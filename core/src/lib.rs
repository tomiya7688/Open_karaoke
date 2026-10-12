//! Product Core APIs. Audio/DSP never depend on the offline analysis process.

pub mod analysis_service;
pub mod audio_import;
pub mod song_data;

use std::{collections::HashMap, convert::Infallible, sync::Arc, time::Duration};

use analysis_service::{AnalysisClient, AnalysisRequest, RemoteJob};
use axum::{
    Json, Router,
    extract::{Path, State},
    http::StatusCode,
    response::{
        IntoResponse,
        sse::{Event, KeepAlive, Sse},
    },
    routing::{get, post},
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use tokio::sync::{RwLock, broadcast};
use tokio_stream::{Stream, StreamExt, wrappers::BroadcastStream};
use uuid::Uuid;

#[derive(Clone, Default)]
pub struct AppState {
    jobs: Arc<RwLock<HashMap<Uuid, JobRecord>>>,
    analysis: AnalysisClient,
}

struct JobRecord {
    snapshot: Job,
    cancelled: bool,
    events: broadcast::Sender<JobEvent>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum JobStatus {
    Queued,
    Running,
    Completed,
    Failed,
    Cancelled,
}

impl JobStatus {
    fn terminal(&self) -> bool {
        matches!(self, Self::Completed | Self::Failed | Self::Cancelled)
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Job {
    pub id: Uuid,
    pub kind: String,
    pub status: JobStatus,
    pub progress: f32,
    pub stage: String,
    pub created_at: DateTime<Utc>,
    pub started_at: Option<DateTime<Utc>>,
    pub finished_at: Option<DateTime<Utc>>,
    pub error: Option<String>,
    pub artifacts: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct JobEvent {
    pub job: Job,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CreateJobRequest {
    pub kind: String,
    #[serde(default = "default_steps")]
    pub steps: u16,
    pub fail_at_step: Option<u16>,
    #[serde(default)]
    pub analysis: AnalysisRequest,
}

fn default_steps() -> u16 {
    5
}

impl AppState {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    #[must_use]
    pub fn with_analysis(analysis: AnalysisClient) -> Self {
        Self {
            analysis,
            ..Self::default()
        }
    }

    async fn snapshot(&self, id: Uuid) -> Option<Job> {
        self.jobs
            .read()
            .await
            .get(&id)
            .map(|record| record.snapshot.clone())
    }

    async fn update(&self, id: Uuid, update: impl FnOnce(&mut Job)) {
        let mut jobs = self.jobs.write().await;
        if let Some(record) = jobs.get_mut(&id) {
            if record.snapshot.status.terminal() {
                return;
            }
            update(&mut record.snapshot);
            let _ = record.events.send(JobEvent {
                job: record.snapshot.clone(),
            });
        }
    }

    async fn cancelled(&self, id: Uuid) -> bool {
        self.jobs
            .read()
            .await
            .get(&id)
            .is_some_and(|record| record.cancelled)
    }

    async fn finish(&self, id: Uuid, status: JobStatus, error: Option<String>) {
        self.update(id, |job| {
            match status {
                JobStatus::Completed => "completed",
                JobStatus::Cancelled => "cancelled",
                _ => "failed",
            }
            .clone_into(&mut job.stage);
            job.status = status;
            job.error = error;
            job.finished_at = Some(Utc::now());
        })
        .await;
    }
}

pub fn app(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/models", get(models))
        .route("/analysis/health", get(analysis_health))
        .route("/jobs", post(create_job))
        .route("/jobs/{id}", get(get_job))
        .route("/jobs/{id}/cancel", post(cancel_job))
        .route("/jobs/{id}/events", get(job_events))
        .with_state(state)
}

async fn health() -> Json<serde_json::Value> {
    Json(serde_json::json!({"status":"ok", "service":"open-karaoke-core"}))
}

async fn models(State(state): State<AppState>) -> Result<Json<serde_json::Value>, ApiError> {
    state
        .analysis
        .models()
        .await
        .map(Json)
        .map_err(ApiError::unavailable)
}

async fn analysis_health(
    State(state): State<AppState>,
) -> Result<Json<serde_json::Value>, ApiError> {
    state
        .analysis
        .health()
        .await
        .map(Json)
        .map_err(ApiError::unavailable)
}

async fn create_job(
    State(state): State<AppState>,
    Json(request): Json<CreateJobRequest>,
) -> Result<(StatusCode, Json<Job>), ApiError> {
    if request.kind.trim().is_empty()
        || request.steps == 0
        || request.steps > 100
        || request
            .fail_at_step
            .is_some_and(|step| step == 0 || step > request.steps)
    {
        return Err(ApiError(
            StatusCode::BAD_REQUEST,
            "Invalid job kind or step range".to_owned(),
        ));
    }
    if let Some(role) = request.kind.strip_prefix("analysis.") {
        if !matches!(
            role,
            "mock" | "stems" | "lyrics" | "alignment" | "pitch" | "events" | "notes" | "song"
        ) {
            return Err(ApiError(
                StatusCode::BAD_REQUEST,
                "Unknown analysis operation".to_owned(),
            ));
        }
        state
            .analysis
            .lease()
            .await
            .map_err(ApiError::unavailable)?;
    }
    let id = Uuid::new_v4();
    let job = Job {
        id,
        kind: request.kind.clone(),
        status: JobStatus::Queued,
        progress: 0.0,
        stage: "queued".to_owned(),
        created_at: Utc::now(),
        started_at: None,
        finished_at: None,
        error: None,
        artifacts: Vec::new(),
    };
    let (events, _) = broadcast::channel(64);
    state.jobs.write().await.insert(
        id,
        JobRecord {
            snapshot: job.clone(),
            cancelled: false,
            events,
        },
    );
    tokio::spawn(run_job(state, id, request));
    Ok((StatusCode::ACCEPTED, Json(job)))
}

async fn run_job(state: AppState, id: Uuid, request: CreateJobRequest) {
    if state.cancelled(id).await {
        state.finish(id, JobStatus::Cancelled, None).await;
        return;
    }
    state
        .update(id, |job| {
            job.status = JobStatus::Running;
            "running".clone_into(&mut job.stage);
            job.started_at = Some(Utc::now());
        })
        .await;
    if let Some(role) = request.kind.strip_prefix("analysis.") {
        if let Err(error) = run_analysis(&state, id, role, &request.analysis).await {
            state.finish(id, JobStatus::Failed, Some(error)).await;
        }
        return;
    }
    for step in 1..=request.steps {
        tokio::time::sleep(Duration::from_millis(25)).await;
        if state.cancelled(id).await {
            state.finish(id, JobStatus::Cancelled, None).await;
            return;
        }
        if request.fail_at_step == Some(step) {
            state
                .finish(
                    id,
                    JobStatus::Failed,
                    Some(format!("simulated failure at step {step}")),
                )
                .await;
            return;
        }
        state
            .update(id, |job| {
                job.progress = f32::from(step) / f32::from(request.steps);
                job.stage = format!("step_{step}");
            })
            .await;
    }
    state.finish(id, JobStatus::Completed, None).await;
}

async fn run_analysis(
    state: &AppState,
    id: Uuid,
    role: &str,
    request: &AnalysisRequest,
) -> Result<(), String> {
    // Pin the instance. A crash fails this job; only NEW jobs use a restarted process.
    let session = state.analysis.lease().await?;
    let mut remote = session.submit(role, request).await?;
    let deadline = tokio::time::Instant::now() + Duration::from_secs(3600);
    let mut cancellation_sent = false;
    loop {
        if state.cancelled(id).await && !cancellation_sent {
            session.cancel(remote.id).await?;
            cancellation_sent = true;
        }
        if apply_remote(state, id, &remote).await? {
            return Ok(());
        }
        if tokio::time::Instant::now() >= deadline {
            let _ = session.cancel(remote.id).await;
            return Err("analysis_timeout: job exceeded its execution deadline".to_owned());
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
        remote = session.job(remote.id).await?;
    }
}

async fn apply_remote(state: &AppState, id: Uuid, remote: &RemoteJob) -> Result<bool, String> {
    state
        .update(id, |job| {
            job.progress = remote.progress;
            job.stage.clone_from(&remote.stage);
        })
        .await;
    match remote.status.as_str() {
        "queued" | "running" => Ok(false),
        "completed" => {
            state
                .update(id, |job| {
                    job.artifacts.clone_from(&remote.artifacts);
                })
                .await;
            state.finish(id, JobStatus::Completed, None).await;
            Ok(true)
        }
        "cancelled" => {
            state.finish(id, JobStatus::Cancelled, None).await;
            Ok(true)
        }
        "failed" => Err(remote.error.as_ref().map_or_else(
            || "analysis_failed: worker failed".to_owned(),
            |e| format!("{}: {}", e.code, e.message),
        )),
        _ => Err("analysis_protocol: unknown job status".to_owned()),
    }
}

async fn get_job(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> Result<Json<Job>, ApiError> {
    state
        .snapshot(id)
        .await
        .map(Json)
        .ok_or_else(ApiError::not_found)
}

async fn cancel_job(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> Result<Json<Job>, ApiError> {
    let mut jobs = state.jobs.write().await;
    let record = jobs.get_mut(&id).ok_or_else(ApiError::not_found)?;
    if !record.snapshot.status.terminal() {
        record.cancelled = true;
    }
    Ok(Json(record.snapshot.clone()))
}

async fn job_events(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, ApiError> {
    // Subscribe and snapshot under one lock so even an already completed job has an event.
    let (snapshot, receiver) = {
        let jobs = state.jobs.read().await;
        let record = jobs.get(&id).ok_or_else(ApiError::not_found)?;
        (record.snapshot.clone(), record.events.subscribe())
    };
    let initial = tokio_stream::once(JobEvent { job: snapshot });
    let updates = BroadcastStream::new(receiver).filter_map(Result::ok);
    let stream = initial.chain(updates).map(|event| {
        // Job contains only finite progress and ordinary strings/timestamps.
        Ok(Event::default()
            .event("job")
            .data(serde_json::to_string(&event).unwrap_or_default()))
    });
    Ok(Sse::new(stream).keep_alive(KeepAlive::default()))
}

#[derive(Debug)]
struct ApiError(StatusCode, String);
impl ApiError {
    fn not_found() -> Self {
        Self(StatusCode::NOT_FOUND, "job not found".to_owned())
    }
    fn unavailable(message: String) -> Self {
        Self(StatusCode::SERVICE_UNAVAILABLE, message)
    }
}
impl IntoResponse for ApiError {
    fn into_response(self) -> axum::response::Response {
        (self.0, Json(serde_json::json!({"error": self.1}))).into_response()
    }
}
