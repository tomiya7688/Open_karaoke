use std::{net::SocketAddr, path::PathBuf, time::Duration};

use open_karaoke_core::{
    AppState,
    analysis_service::{AnalysisConfig, AnalysisService},
    app,
};
use tokio::net::TcpListener;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let bind: SocketAddr = std::env::var("OPEN_KARAOKE_BIND")
        .unwrap_or_else(|_| "127.0.0.1:32145".to_owned())
        .parse()?;
    if !bind.ip().is_loopback() {
        return Err("Core must bind to loopback".into());
    }
    let listener = TcpListener::bind(bind).await?;
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
    let state = AppState::with_analysis(service.client());
    println!(
        "Open Karaoke Core listening on http://{}",
        listener.local_addr()?
    );
    let result = tokio::select! {
        result = axum::serve(listener, app(state)).into_future() => result,
        result = tokio::signal::ctrl_c() => result,
    };
    service.shutdown().await;
    result?;
    Ok(())
}
