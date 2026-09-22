use open_karaoke_core::{app, AppState};
use tokio::net::TcpListener;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let state = AppState::new();
    let listener = TcpListener::bind("127.0.0.1:32145").await?;
    println!("Open Karaoke Core listening on http://{}", listener.local_addr()?);
    axum::serve(listener, app(state)).await?;
    Ok(())
}
