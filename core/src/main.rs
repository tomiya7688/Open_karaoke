mod session;

use std::{
    io::{self, IsTerminal, Read, Write},
    net::SocketAddr,
    path::PathBuf,
    time::Duration,
};

use open_karaoke_core::{
    AppState, CORE_API_VERSION,
    analysis_service::{AnalysisConfig, AnalysisService},
    ffmpeg_bundle::{MediaTools, verify_bundle},
    inference_policy::{WorkerPriority, parse_max_heavy_jobs},
    runtime_layout::{RUNTIME_MANIFEST_FILE, RuntimeLayout, RuntimeManifest},
    song_library::SongLibrary,
};
use session::CoreSession;
use tokio::{net::TcpListener, sync::oneshot};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Bootstrap before opening a socket or starting Python. Refuse an interactive
    // terminal so a secret is never requested or echoed at a shell prompt.
    if io::stdin().is_terminal() {
        return Err("Launch Core through a desktop/CLI launcher with redirected stdin".into());
    }
    let session = CoreSession::read_from(&mut io::stdin().lock())?;
    let instance_id = session.instance_id();
    let (parent_closed, owner_lifetime) = oneshot::channel();
    std::thread::spawn(move || {
        // The first line is the entire protocol. EOF or unexpected further input
        // revokes the process lifetime; the launcher keeps the pipe open to own it.
        let _ = io::stdin().read(&mut [0_u8; 1]);
        let _ = parent_closed.send(());
    });

    let bind: SocketAddr = std::env::var("OPEN_KARAOKE_BIND")
        .unwrap_or_else(|_| "127.0.0.1:32145".to_owned())
        .parse()?;
    if !bind.ip().is_loopback() {
        return Err("Core must bind to loopback".into());
    }
    let listener = TcpListener::bind(bind).await?;
    let port = listener.local_addr()?.port();
    let executable = std::env::current_exe()?;
    let directory = executable
        .parent()
        .ok_or("Executable directory is missing")?;
    let python = RuntimeLayout::new(directory)
        .resolve_python(std::env::var_os("OPEN_KARAOKE_PYTHON").map(PathBuf::from));
    let layout = RuntimeLayout::new(directory);
    let media_tools = MediaTools::resolve(
        &layout,
        std::env::var_os("OPEN_KARAOKE_FFMPEG").map(PathBuf::from),
        std::env::var_os("OPEN_KARAOKE_FFPROBE").map(PathBuf::from),
    );
    report_bundled_ffmpeg(&layout);
    let data = std::env::var_os("OPEN_KARAOKE_DATA_DIR")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("LOCALAPPDATA").map(|p| PathBuf::from(p).join("OpenKaraoke")))
        .or_else(|| {
            std::env::var_os("HOME").map(|p| PathBuf::from(p).join(".local/share/OpenKaraoke"))
        })
        .ok_or("Set OPEN_KARAOKE_DATA_DIR to a writable application data directory")?;
    // 範囲外の値は暗黙に丸めず起動時に拒否する(未設定なら現行挙動)。
    // UTF-8でない値も未設定扱いにせず拒否する。
    let worker_priority = match std::env::var("OPEN_KARAOKE_WORKER_PRIORITY") {
        Ok(text) => WorkerPriority::parse(&text)?,
        Err(std::env::VarError::NotPresent) => WorkerPriority::Inherit,
        Err(error) => return Err(format!("OPEN_KARAOKE_WORKER_PRIORITY: {error}").into()),
    };
    let (worker_priority, warning) = worker_priority.effective();
    if let Some(warning) = warning {
        eprintln!("warning: {warning}");
    }
    let max_heavy_jobs = match std::env::var("OPEN_KARAOKE_MAX_HEAVY_JOBS") {
        Ok(text) => parse_max_heavy_jobs(&text)?,
        Err(std::env::VarError::NotPresent) => None,
        Err(error) => return Err(format!("OPEN_KARAOKE_MAX_HEAVY_JOBS: {error}").into()),
    };
    let library = SongLibrary::open(data.join("artifacts"), data.join("songs"))?;
    let service = AnalysisService::start(AnalysisConfig {
        python,
        artifact_root: data.join("artifacts"),
        startup_timeout: Duration::from_secs(30),
        worker_priority,
    })
    .await
    .map_err(std::io::Error::other)?;
    let state = AppState::with_analysis(service.client())
        .with_core_instance_id(instance_id)
        .with_song_library(library)
        .with_media_tools(media_tools)
        .with_max_concurrent_heavy_jobs(max_heavy_jobs);
    let router = session.router(state.clone());
    if let Err(error) = publish_ready(port, instance_id) {
        // A launcher that exited during startup must not leave its worker behind.
        state.begin_shutdown().await;
        service.shutdown().await;
        return Err(error.into());
    }
    let result = tokio::select! {
        result = axum::serve(listener, router).into_future() => result,
        result = tokio::signal::ctrl_c() => result,
        _ = owner_lifetime => Ok(()),
    };
    state.begin_shutdown().await;
    service.shutdown().await;
    result?;
    Ok(())
}

/// { 処理: 同梱配置（manifestあり）の場合だけ同梱FFmpegを検査し、失敗はstderrへ警告する。
///   開発環境（manifestなし）では何もしない。起動は止めない（取り込み時に再検査される）。 }
fn report_bundled_ffmpeg(layout: &RuntimeLayout) {
    let path = layout.manifest_path();
    if !path.is_file() {
        return;
    }
    let result = RuntimeManifest::load(&path)
        .map_err(|e| e.to_string())
        .and_then(|m| verify_bundle(layout, &m).map_err(|e| e.to_string()));
    if let Err(error) = result {
        eprintln!("warning: {RUNTIME_MANIFEST_FILE}: {error}");
    }
}

fn publish_ready(port: u16, instance_id: uuid::Uuid) -> io::Result<()> {
    // Readiness is credential-free and uses fallible I/O, not a broken-pipe panic.
    let mut output = io::stdout().lock();
    writeln!(
        output,
        "{}",
        serde_json::json!({
            "event": "core_ready",
            "api_version": CORE_API_VERSION,
            "instance_id": instance_id,
            "port": port,
        })
    )?;
    output.flush()
}
