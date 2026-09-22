use std::{
    collections::HashMap,
    convert::Infallible,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::{
        sse::{Event, KeepAlive, Sse},
        IntoResponse,
    },
    routing::{get, post},
    Json, Router,
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use tokio::sync::{broadcast, RwLock};
use tokio_stream::{wrappers::BroadcastStream, Stream, StreamExt};
use uuid::Uuid;

#[derive(Clone)]
pub struct AppState {
    jobs: Arc<RwLock<HashMap<Uuid, JobRecord>>>,
    events: broadcast::Sender<JobEvent>,
}

#[derive(Clone)]
struct JobRecord {
    snapshot: Job,
    cancel: Arc<AtomicBool>,
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
pub struct CreateJobRequest {
    pub kind: String,
    #[serde(default = "default_steps")]
    pub steps: u32,
    pub fail_at_step: Option<u32>,
}

#[derive(Debug, Serialize)]
struct Health {
    status: &'static str,
    service: &'static str,
}

fn default_steps() -> u32 {
    5
}

impl AppState {
    #[must_use]
    pub fn new() -> Self {
        let (events, _) = broadcast::channel(256);
        Self {
            jobs: Arc::new(RwLock::new(HashMap::new())),
            events,
        }
    }

    async fn snapshot(&self, id: Uuid) -> Option<Job> {
        self.jobs.read().await.get(&id).map(|record| record.snapshot.clone())
    }

    async fn update<F>(&self, id: Uuid, update: F) -> Option<Job>
    where
        F: FnOnce(&mut Job),
    {
        let snapshot = {
            let mut jobs = self.jobs.write().await;
            let record = jobs.get_mut(&id)?;
            update(&mut record.snapshot);
            record.snapshot.clone()
        };
        let _ = self.events.send(JobEvent {
            job: snapshot.clone(),
        });
        Some(snapshot)
    }

    async fn is_cancelled(&self, id: Uuid) -> bool {
        self.jobs
            .read()
            .await
            .get(&id)
            .is_some_and(|record| record.cancel.load(Ordering::Acquire))
    }
}

impl Default for AppState {
    fn default() -> Self {
        Self::new()
    }
}

#[must_use]
pub fn app(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/jobs", post(create_job))
        .route("/jobs/{id}", get(get_job))
        .route("/jobs/{id}/cancel", post(cancel_job))
        .route("/jobs/{id}/events", get(job_events))
        .with_state(state)
}

async fn health() -> Json<Health> {
    Json(Health {
        status: "ok",
        service: "open-karaoke-core",
    })
}

async fn create_job(
    State(state): State<AppState>,
    Json(request): Json<CreateJobRequest>,
) -> Result<(StatusCode, Json<Job>), ApiError> {
    if request.kind.trim().is_empty() {
        return Err(ApiError::bad_request("kind must not be empty"));
    }
    if request.steps == 0 || request.steps > 100 {
        return Err(ApiError::bad_request("steps must be between 1 and 100"));
    }
    if request
        .fail_at_step
        .is_some_and(|step| step == 0 || step > request.steps)
    {
        return Err(ApiError::bad_request(
            "fail_at_step must be within the job step range",
        ));
    }

    let id = Uuid::new_v4();
    let now = Utc::now();
    let job = Job {
        id,
        kind: request.kind,
        status: JobStatus::Queued,
        progress: 0.0,
        stage: "queued".to_owned(),
        created_at: now,
        started_at: None,
        finished_at: None,
        error: None,
        artifacts: Vec::new(),
    };
    state.jobs.write().await.insert(
        id,
        JobRecord {
            snapshot: job.clone(),
            cancel: Arc::new(AtomicBool::new(false)),
        },
    );
    let _ = state.events.send(JobEvent { job: job.clone() });

    tokio::spawn(run_job(
        state.clone(),
        id,
        request.steps,
        request.fail_at_step,
    ));

    Ok((StatusCode::ACCEPTED, Json(job)))
}

async fn run_job(state: AppState, id: Uuid, steps: u32, fail_at_step: Option<u32>) {
    let _ = state
        .update(id, |job| {
            job.status = JobStatus::Running;
            job.stage = "running".to_owned();
            job.started_at = Some(Utc::now());
        })
        .await;

    for step in 1..=steps {
        if state.is_cancelled(id).await {
            let _ = state
                .update(id, |job| {
                    job.status = JobStatus::Cancelled;
                    job.stage = "cancelled".to_owned();
                    job.finished_at = Some(Utc::now());
                })
                .await;
            return;
        }

        tokio::time::sleep(Duration::from_millis(25)).await;

        if fail_at_step == Some(step) {
            let _ = state
                .update(id, |job| {
                    job.status = JobStatus::Failed;
                    job.stage = "failed".to_owned();
                    job.error = Some(format!("simulated failure at step {step}"));
                    job.finished_at = Some(Utc::now());
                })
                .await;
            return;
        }

        let _ = state
            .update(id, |job| {
                job.progress = step as f32 / steps as f32;
                job.stage = format!("step_{step}");
            })
            .await;
    }

    let _ = state
        .update(id, |job| {
            job.status = JobStatus::Completed;
            job.progress = 1.0;
            job.stage = "completed".to_owned();
            job.finished_at = Some(Utc::now());
        })
        .await;
}

async fn get_job(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> Result<Json<Job>, ApiError> {
    state.snapshot(id).await.map(Json).ok_or_else(ApiError::not_found)
}

async fn cancel_job(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> Result<Json<Job>, ApiError> {
    {
        let jobs = state.jobs.read().await;
        let record = jobs.get(&id).ok_or_else(ApiError::not_found)?;
        if matches!(
            record.snapshot.status,
            JobStatus::Completed | JobStatus::Failed | JobStatus::Cancelled
        ) {
            return Ok(Json(record.snapshot.clone()));
        }
        record.cancel.store(true, Ordering::Release);
    }

    state
        .snapshot(id)
        .await
        .map(Json)
        .ok_or_else(ApiError::not_found)
}

async fn job_events(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, ApiError> {
    if state.snapshot(id).await.is_none() {
        return Err(ApiError::not_found());
    }

    let stream = BroadcastStream::new(state.events.subscribe()).filter_map(move |message| {
        let event = match message {
            Ok(event) if event.job.id == id => {
                Some(Ok(Event::default().json_data(event).expect("serializable job event")))
            }
            _ => None,
        };
        event
    });

    Ok(Sse::new(stream).keep_alive(KeepAlive::default()))
}

#[derive(Debug)]
struct ApiError {
    status: StatusCode,
    message: String,
}

impl ApiError {
    fn bad_request(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            message: message.into(),
        }
    }

    fn not_found() -> Self {
        Self {
            status: StatusCode::NOT_FOUND,
            message: "job not found".to_owned(),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> axum::response::Response {
        (
            self.status,
            Json(serde_json::json!({
                "error": self.message,
            })),
        )
            .into_response()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        body::{to_bytes, Body},
        http::{Request, StatusCode},
    };
    use tower::ServiceExt;

    async fn create_test_job(
        app: &Router,
        steps: u32,
        fail_at_step: Option<u32>,
    ) -> Job {
        let request = Request::builder()
            .method("POST")
            .uri("/jobs")
            .header("content-type", "application/json")
            .body(Body::from(
                serde_json::json!({
                    "kind": "test",
                    "steps": steps,
                    "fail_at_step": fail_at_step,
                })
                .to_string(),
            ))
            .unwrap();
        let response = app.clone().oneshot(request).await.unwrap();
        assert_eq!(response.status(), StatusCode::ACCEPTED);
        let bytes = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        serde_json::from_slice(&bytes).unwrap()
    }

    async fn wait_terminal(app: &Router, id: Uuid) -> Job {
        for _ in 0..100 {
            let response = app
                .clone()
                .oneshot(
                    Request::builder()
                        .uri(format!("/jobs/{id}"))
                        .body(Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            let bytes = to_bytes(response.into_body(), usize::MAX).await.unwrap();
            let job: Job = serde_json::from_slice(&bytes).unwrap();
            if matches!(
                job.status,
                JobStatus::Completed | JobStatus::Failed | JobStatus::Cancelled
            ) {
                return job;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        panic!("job did not reach a terminal state");
    }

    #[tokio::test]
    async fn health_contract() {
        let response = app(AppState::new())
            .oneshot(
                Request::builder()
                    .uri("/health")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn lifecycle_completes() {
        let router = app(AppState::new());
        let job = create_test_job(&router, 2, None).await;
        let finished = wait_terminal(&router, job.id).await;
        assert_eq!(finished.status, JobStatus::Completed);
        assert_eq!(finished.progress, 1.0);
        assert!(finished.finished_at.is_some());
    }

    #[tokio::test]
    async fn failure_reason_is_exposed() {
        let router = app(AppState::new());
        let job = create_test_job(&router, 2, Some(1)).await;
        let finished = wait_terminal(&router, job.id).await;
        assert_eq!(finished.status, JobStatus::Failed);
        assert!(finished.error.as_deref().is_some_and(|e| e.contains("step 1")));
    }

    #[tokio::test]
    async fn cancellation_reaches_cancelled() {
        let router = app(AppState::new());
        let job = create_test_job(&router, 50, None).await;
        let response = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri(format!("/jobs/{}/cancel", job.id))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let finished = wait_terminal(&router, job.id).await;
        assert_eq!(finished.status, JobStatus::Cancelled);
    }

    #[tokio::test]
    async fn concurrent_jobs_complete_independently() {
        let router = app(AppState::new());
        let first = create_test_job(&router, 2, None).await;
        let second = create_test_job(&router, 3, None).await;
        let (a, b) = tokio::join!(
            wait_terminal(&router, first.id),
            wait_terminal(&router, second.id)
        );
        assert_eq!(a.status, JobStatus::Completed);
        assert_eq!(b.status, JobStatus::Completed);
        assert_ne!(a.id, b.id);
    }
}
