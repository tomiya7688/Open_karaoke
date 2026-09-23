//! Supervised loopback Python process. A job pins one service instance and is never replayed.

use std::{
    path::{Path, PathBuf},
    process::Stdio,
    sync::Arc,
    time::Duration,
};

use reqwest::{Client, Method, redirect::Policy};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::{
    fs,
    process::{Child, Command},
    sync::{RwLock, oneshot},
    task::JoinHandle,
    time::{Instant, sleep},
};
use uuid::Uuid;

#[derive(Clone)]
pub struct AnalysisConfig {
    pub python: PathBuf,
    pub artifact_root: PathBuf,
    pub startup_timeout: Duration,
}

#[derive(Clone, Deserialize)]
struct Ready {
    port: u16,
    pid: u32,
    instance_id: Uuid,
    protocol_version: u32,
}

#[derive(Clone)]
pub(crate) struct Session {
    ready: Ready,
    token: String,
    http: Client,
}

#[derive(Clone, Default)]
pub struct AnalysisClient {
    session: Arc<RwLock<Option<Session>>>,
}

pub struct AnalysisService {
    client: AnalysisClient,
    stop: Option<oneshot::Sender<()>>,
    task: Option<JoinHandle<()>>,
}

struct Running {
    child: Child,
    ready_file: PathBuf,
    session: Session,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AnalysisRequest {
    pub model_id: Option<String>,
    pub input_artifact: Option<String>,
    #[serde(default)]
    pub options: serde_json::Map<String, Value>,
}

#[derive(Deserialize)]
pub(crate) struct RemoteError {
    pub code: String,
    pub message: String,
}

#[derive(Deserialize)]
pub(crate) struct RemoteJob {
    pub id: Uuid,
    pub instance_id: Uuid,
    pub status: String,
    pub progress: f32,
    pub stage: String,
    pub error: Option<RemoteError>,
    pub artifacts: Vec<String>,
}

impl Session {
    async fn request(
        &self,
        method: Method,
        path: &str,
        body: Option<Value>,
    ) -> Result<Value, String> {
        let mut request = self
            .http
            .request(
                method,
                format!("http://127.0.0.1:{}/{path}", self.ready.port),
            )
            .bearer_auth(&self.token);
        if let Some(body) = body {
            request = request.json(&body);
        }
        let response = request
            .send()
            .await
            .map_err(|e| format!("analysis_unavailable: {e}"))?;
        let status = response.status();
        let value: Value = response
            .json()
            .await
            .map_err(|e| format!("analysis_protocol: {e}"))?;
        if !status.is_success() {
            let code = value["error"]["code"].as_str().unwrap_or("analysis_error");
            let message = value["error"]["message"]
                .as_str()
                .unwrap_or("Analysis request failed");
            return Err(format!("{code}: {message}"));
        }
        Ok(value)
    }

    async fn health(&self) -> Result<Value, String> {
        let value = self.request(Method::GET, "health", None).await?;
        if value["service"] != "open-karaoke-analysis"
            || value["status"] != "ok"
            || value["protocol_version"] != 1
            || value["pid"] != self.ready.pid
            || value["instance_id"] != self.ready.instance_id.to_string()
        {
            return Err("analysis_protocol: service identity mismatch".to_owned());
        }
        Ok(value)
    }

    pub(crate) async fn submit(
        &self,
        role: &str,
        request: &AnalysisRequest,
    ) -> Result<RemoteJob, String> {
        let body = serde_json::to_value(request).map_err(|e| e.to_string())?;
        let value = self
            .request(Method::POST, &format!("analysis/{role}"), Some(body))
            .await?;
        self.decode_job(value)
    }

    pub(crate) async fn job(&self, id: Uuid) -> Result<RemoteJob, String> {
        let value = self
            .request(Method::GET, &format!("jobs/{id}"), None)
            .await?;
        let job = self.decode_job(value)?;
        if job.id != id {
            return Err("analysis_protocol: job identity mismatch".to_owned());
        }
        Ok(job)
    }

    pub(crate) async fn cancel(&self, id: Uuid) -> Result<(), String> {
        self.request(Method::POST, &format!("jobs/{id}/cancel"), None)
            .await?;
        Ok(())
    }

    fn decode_job(&self, value: Value) -> Result<RemoteJob, String> {
        let job: RemoteJob =
            serde_json::from_value(value).map_err(|e| format!("analysis_protocol: {e}"))?;
        if job.instance_id != self.ready.instance_id || !(0.0..=1.0).contains(&job.progress) {
            return Err("analysis_protocol: invalid instance or progress".to_owned());
        }
        Ok(job)
    }
}

impl AnalysisClient {
    pub(crate) async fn lease(&self) -> Result<Session, String> {
        self.session
            .read()
            .await
            .clone()
            .ok_or_else(|| "analysis_unavailable: service is stopped or recovering".to_owned())
    }

    /// Read the authenticated health endpoint of the current child.
    ///
    /// # Errors
    /// Returns an error during recovery, after shutdown, or on a protocol mismatch.
    pub async fn health(&self) -> Result<Value, String> {
        self.lease().await?.health().await
    }

    /// List registered adapters without loading inference models.
    ///
    /// # Errors
    /// Returns an error when the service or its REST response is unavailable.
    pub async fn models(&self) -> Result<Value, String> {
        self.lease()
            .await?
            .request(Method::GET, "models", None)
            .await
    }
}

impl AnalysisService {
    /// Start Python, authenticate its readiness handshake, and supervise its lifetime.
    ///
    /// # Errors
    /// Returns an error for invalid paths, spawn failure, protocol mismatch or startup timeout.
    pub async fn start(mut config: AnalysisConfig) -> Result<Self, String> {
        fs::create_dir_all(&config.artifact_root)
            .await
            .map_err(|e| e.to_string())?;
        config.artifact_root = fs::canonicalize(&config.artifact_root)
            .await
            .map_err(|e| e.to_string())?;
        let http = Client::builder()
            .no_proxy()
            .redirect(Policy::none())
            .timeout(Duration::from_secs(3))
            .build()
            .map_err(|e| e.to_string())?;
        let running = launch(&config, &http).await?;
        let client = AnalysisClient::default();
        *client.session.write().await = Some(running.session.clone());
        let (stop, receiver) = oneshot::channel();
        let task = tokio::spawn(supervise(config, http, running, client.clone(), receiver));
        Ok(Self {
            client,
            stop: Some(stop),
            task: Some(task),
        })
    }

    #[must_use]
    pub fn client(&self) -> AnalysisClient {
        self.client.clone()
    }

    /// Stop accepting new analysis, request cooperative shutdown, then reap the child.
    pub async fn shutdown(mut self) {
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
        if let Some(task) = self.task.take() {
            let _ = task.await;
        }
    }
}

impl Drop for AnalysisService {
    fn drop(&mut self) {
        // Also signals on early-return paths; the supervisor owns and reaps the child.
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
    }
}

async fn launch(config: &AnalysisConfig, http: &Client) -> Result<Running, String> {
    let runtime = config.artifact_root.join(".runtime");
    fs::create_dir_all(&runtime)
        .await
        .map_err(|e| e.to_string())?;
    let ready_file = runtime.join(format!("{}.json", Uuid::new_v4()));
    let token = format!("{}{}", Uuid::new_v4(), Uuid::new_v4());
    let mut child = Command::new(&config.python)
        .args(["-m", "open_karaoke_analysis", "--artifact-root"])
        .arg(&config.artifact_root)
        .arg("--ready-file")
        .arg(&ready_file)
        .env("OPEN_KARAOKE_ANALYSIS_TOKEN", &token)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| format!("analysis_spawn: {e}"))?;
    let result = wait_ready(
        &mut child,
        &ready_file,
        &token,
        http,
        config.startup_timeout,
    )
    .await;
    match result {
        Ok(session) => Ok(Running {
            child,
            ready_file,
            session,
        }),
        Err(error) => {
            let _ = child.kill().await;
            let _ = child.wait().await;
            let _ = fs::remove_file(&ready_file).await;
            Err(error)
        }
    }
}

async fn wait_ready(
    child: &mut Child,
    ready_file: &Path,
    token: &str,
    http: &Client,
    timeout: Duration,
) -> Result<Session, String> {
    let deadline = Instant::now() + timeout;
    loop {
        if let Some(status) = child.try_wait().map_err(|e| e.to_string())? {
            return Err(format!("analysis_startup: child exited with {status}"));
        }
        if let Ok(bytes) = fs::read(ready_file).await {
            let ready: Ready =
                serde_json::from_slice(&bytes).map_err(|e| format!("analysis_handshake: {e}"))?;
            if ready.port == 0 || ready.protocol_version != 1 || Some(ready.pid) != child.id() {
                return Err("analysis_handshake: invalid port, version or child PID".to_owned());
            }
            let session = Session {
                ready,
                token: token.to_owned(),
                http: http.clone(),
            };
            session.health().await?;
            return Ok(session);
        }
        if Instant::now() >= deadline {
            return Err("analysis_startup_timeout: no ready handshake".to_owned());
        }
        sleep(Duration::from_millis(50)).await;
    }
}

async fn stop_child(running: &mut Running) {
    let _ = running
        .session
        .request(Method::POST, "shutdown", None)
        .await;
    if tokio::time::timeout(Duration::from_secs(5), running.child.wait())
        .await
        .is_err()
    {
        let _ = running.child.kill().await;
    }
    let _ = running.child.wait().await;
    let _ = fs::remove_file(&running.ready_file).await;
}

async fn supervise(
    config: AnalysisConfig,
    http: Client,
    mut running: Running,
    client: AnalysisClient,
    mut stop: oneshot::Receiver<()>,
) {
    let mut restarts = 0_u32;
    let mut failures = 0_u32;
    loop {
        tokio::select! {
            _ = &mut stop => break,
            () = sleep(Duration::from_millis(500)) => {}
        }
        let exited = !matches!(running.child.try_wait(), Ok(None));
        if !exited && running.session.health().await.is_ok() {
            failures = 0;
            continue;
        }
        failures += 1;
        if !exited && failures < 3 {
            continue;
        }
        *client.session.write().await = None;
        stop_child(&mut running).await;
        let mut replacement = None;
        while restarts < 3 {
            restarts += 1;
            tokio::select! {
                _ = &mut stop => return,
                () = sleep(Duration::from_millis(200 * u64::from(restarts))) => {}
            }
            match launch(&config, &http).await {
                Ok(next) => {
                    replacement = Some(next);
                    break;
                }
                Err(error) => eprintln!("Analysis restart {restarts} failed: {error}"),
            }
        }
        let Some(next) = replacement else {
            eprintln!("Analysis service exhausted its restart budget");
            return;
        };
        running = next;
        failures = 0;
        *client.session.write().await = Some(running.session.clone());
    }
    *client.session.write().await = None;
    stop_child(&mut running).await;
}
