use std::{path::PathBuf, time::Duration};

use axum::{
    Router,
    body::{Body, to_bytes},
    http::{Request, StatusCode},
};
use open_karaoke_core::{
    AppState, Job, JobStatus,
    analysis_service::{AnalysisConfig, AnalysisService},
    app,
};
use serde_json::{Value, json};
use tokio::time::{Instant, sleep};
use tower::ServiceExt;
use uuid::Uuid;

async fn call(router: &Router, method: &str, path: &str, body: Value) -> (StatusCode, Value) {
    let response = router
        .clone()
        .oneshot(
            Request::builder()
                .method(method)
                .uri(path)
                .header("content-type", "application/json")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 1_048_576).await.unwrap();
    (status, serde_json::from_slice(&bytes).unwrap())
}

async fn create(router: &Router, body: Value) -> Uuid {
    let (status, value) = call(router, "POST", "/jobs", body).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{value}");
    assert_eq!(value["status"], "queued");
    serde_json::from_value::<Job>(value).unwrap().id
}

async fn terminal(router: &Router, id: Uuid) -> Job {
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        let (status, value) = call(router, "GET", &format!("/jobs/{id}"), Value::Null).await;
        assert_eq!(status, StatusCode::OK);
        let job: Job = serde_json::from_value(value).unwrap();
        if matches!(
            job.status,
            JobStatus::Completed | JobStatus::Failed | JobStatus::Cancelled
        ) {
            return job;
        }
        assert!(Instant::now() < deadline, "Job did not finish");
        sleep(Duration::from_millis(10)).await;
    }
}

#[tokio::test]
async fn health_contract() {
    let router = app(AppState::new());
    let (status, value) = call(&router, "GET", "/health", Value::Null).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(value["service"], "open-karaoke-core");
    assert_eq!(
        call(&router, "GET", "/models", Value::Null).await.0,
        StatusCode::SERVICE_UNAVAILABLE
    );
}

#[tokio::test]
async fn lifecycle_completes() {
    let router = app(AppState::new());
    let id = create(&router, json!({"kind":"test", "steps":2})).await;
    let job = terminal(&router, id).await;
    assert_eq!(job.status, JobStatus::Completed);
    assert!((job.progress - 1.0).abs() < f32::EPSILON);
    assert!(job.started_at.is_some() && job.finished_at.is_some());
}

#[tokio::test]
async fn failure_reason_is_exposed() {
    let router = app(AppState::new());
    let id = create(&router, json!({"kind":"test", "steps":2, "fail_at_step":1})).await;
    let job = terminal(&router, id).await;
    assert_eq!(job.status, JobStatus::Failed);
    assert!(job.error.unwrap().contains("step 1"));
}

#[tokio::test]
async fn cancellation_reaches_cancelled() {
    let router = app(AppState::new());
    let id = create(&router, json!({"kind":"test", "steps":50})).await;
    call(&router, "POST", &format!("/jobs/{id}/cancel"), Value::Null).await;
    assert_eq!(terminal(&router, id).await.status, JobStatus::Cancelled);
}

#[tokio::test]
async fn concurrent_jobs_complete_independently() {
    let router = app(AppState::new());
    let a = create(&router, json!({"kind":"test", "steps":2})).await;
    let b = create(&router, json!({"kind":"test", "steps":3})).await;
    let (first, second) = tokio::join!(terminal(&router, a), terminal(&router, b));
    assert_eq!(first.status, JobStatus::Completed);
    assert_eq!(second.status, JobStatus::Completed);
    assert_ne!(a, b);
}

#[tokio::test]
async fn invalid_requests_and_unknown_jobs() {
    let router = app(AppState::new());
    for body in [
        json!({"kind":"test", "steps":0}),
        json!({"kind":"analysis.unknown"}),
    ] {
        assert_eq!(
            call(&router, "POST", "/jobs", body).await.0,
            StatusCode::BAD_REQUEST
        );
    }
    assert_eq!(
        call(
            &router,
            "GET",
            &format!("/jobs/{}", Uuid::new_v4()),
            Value::Null
        )
        .await
        .0,
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn missing_python_has_actionable_error() {
    let root = tempfile::tempdir().unwrap();
    let result = AnalysisService::start(AnalysisConfig {
        python: root.path().join("no-python-here"),
        artifact_root: root.path().join("artifacts"),
        startup_timeout: Duration::from_secs(1),
    })
    .await;
    assert!(result.err().unwrap().contains("analysis_spawn"));
}

#[tokio::test]
#[ignore = "Requires installed Python analysis package; explicitly executed in CI"]
async fn python_startup_mock_cancel_crash_recovery_shutdown() {
    let root = tempfile::tempdir().unwrap();
    let python = std::env::var_os("OPEN_KARAOKE_PYTHON")
        .map_or_else(|| PathBuf::from("python"), PathBuf::from);
    let service = AnalysisService::start(AnalysisConfig {
        python: python.clone(),
        artifact_root: root.path().join("artifacts"),
        startup_timeout: Duration::from_secs(30),
    })
    .await
    .unwrap();
    let client = service.client();
    let router = app(AppState::with_analysis(client.clone()));
    let health = client.health().await.unwrap();
    assert_eq!(client.models().await.unwrap()["models"][0]["id"], "mock-v1");
    let a = create(&router, json!({"kind":"analysis.mock"})).await;
    let result = terminal(&router, a).await;
    assert_eq!(result.status, JobStatus::Completed);
    assert!(
        root.path()
            .join("artifacts")
            .join(&result.artifacts[0])
            .is_file()
    );
    let b = create(
        &router,
        json!({"kind":"analysis.mock", "analysis":{"options":{"steps":100,"delay_ms":100}}}),
    )
    .await;
    call(&router, "POST", &format!("/jobs/{b}/cancel"), Value::Null).await;
    assert_eq!(terminal(&router, b).await.status, JobStatus::Cancelled);
    let failed = create(
        &router,
        json!({"kind":"analysis.mock", "analysis":{"options":{"fail":true}}}),
    )
    .await;
    assert!(
        terminal(&router, failed)
            .await
            .error
            .unwrap()
            .contains("mock_failure")
    );
    let unsupported = create(&router, json!({"kind":"analysis.notes"})).await;
    assert!(
        terminal(&router, unsupported)
            .await
            .error
            .unwrap()
            .contains("not_implemented")
    );
    let crashed = create(
        &router,
        json!({"kind":"analysis.mock", "analysis":{"options":{"steps":100,"delay_ms":100}}}),
    )
    .await;
    sleep(Duration::from_millis(200)).await;
    let pid = health["pid"].as_u64().unwrap();
    let killed = tokio::process::Command::new(python)
        .args([
            "-c",
            "import os,signal,sys; os.kill(int(sys.argv[1]), signal.SIGTERM)",
        ])
        .arg(pid.to_string())
        .status()
        .await
        .unwrap();
    assert!(killed.success());
    assert_eq!(terminal(&router, crashed).await.status, JobStatus::Failed);
    let deadline = Instant::now() + Duration::from_secs(40);
    loop {
        if let Ok(next) = client.health().await
            && next["instance_id"] != health["instance_id"]
        {
            break;
        }
        assert!(Instant::now() < deadline, "Service did not recover");
        sleep(Duration::from_millis(50)).await;
    }
    let next = create(&router, json!({"kind":"analysis.mock"})).await;
    assert_eq!(terminal(&router, next).await.status, JobStatus::Completed);
    assert_eq!(terminal(&router, crashed).await.status, JobStatus::Failed);
    service.shutdown().await;
    assert!(client.health().await.is_err());
    assert_eq!(
        std::fs::read_dir(root.path().join("artifacts/.runtime"))
            .unwrap()
            .count(),
        0
    );
}
