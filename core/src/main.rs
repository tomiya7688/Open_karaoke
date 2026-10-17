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
    let bundled = directory.join("runtime/python").join(if cfg!(windows) {
        "python.exe"
    } else {
        "bin/python3"
    });
    let python = std::env::var_os("OPEN_KARAOKE_PYTHON").map_or_else(
        || {
            if bundled.is_file() {
                bundled
            } else {
                PathBuf::from("python")
            }
        },
        PathBuf::from,
    );
    let data = std::env::var_os("OPEN_KARAOKE_DATA_DIR")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("LOCALAPPDATA").map(|p| PathBuf::from(p).join("OpenKaraoke")))
        .or_else(|| {
            std::env::var_os("HOME").map(|p| PathBuf::from(p).join(".local/share/OpenKaraoke"))
        })
        .ok_or("Set OPEN_KARAOKE_DATA_DIR to a writable application data directory")?;
    let service = AnalysisService::start(AnalysisConfig {
        python,
        artifact_root: data.join("artifacts"),
        startup_timeout: Duration::from_secs(30),
    })
    .await
    .map_err(std::io::Error::other)?;
    let router = session.router(AppState::with_analysis(service.client()));
    if let Err(error) = publish_ready(port, instance_id) {
        // A launcher that exited during startup must not leave its worker behind.
        service.shutdown().await;
        return Err(error.into());
    }
    let result = tokio::select! {
        result = axum::serve(listener, router).into_future() => result,
        result = tokio::signal::ctrl_c() => result,
        _ = owner_lifetime => Ok(()),
    };
    service.shutdown().await;
    result?;
    Ok(())
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
