//! Seed completed analysis Jobs in-process; the product never exposes this fixture hook.

#[path = "../../tests/support/song_export.rs"]
mod fixture;

use std::{sync::Arc, time::Duration};

use axum::{Router, body::{Body, to_bytes}, http::{Request, StatusCode}};
use serde_json::{Value, json};
use tokio::sync::broadcast;
use tower::ServiceExt;
use uuid::Uuid;

use crate::{AppState, Job, JobRecord, JobStatus, app, song_library::{ImportControl, SongLibrary}};

async fn call(router: &Router, method: &str, path: &str, body: Value) -> (StatusCode, Value) {
    let response = router.clone().oneshot(
        Request::builder().method(method).uri(path)
            .header("content-type", "application/json")
            .header("x-open-karaoke-api-version", "1")
            .body(Body::from(body.to_string())).unwrap()
    ).await.unwrap();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 16 * 1024 * 1024).await.unwrap();
    (status, serde_json::from_slice(&bytes).unwrap_or(Value::Null))
}

async fn setup() -> (tempfile::TempDir, AppState, Job) {
    let root = tempfile::tempdir().unwrap();
    let artifacts = root.path().join("artifacts");
    let library = SongLibrary::open(&artifacts, root.path().join("songs")).unwrap();
    let (source, _) = fixture::build(&artifacts);
    let source: Job = serde_json::from_value(source).unwrap();
    let state = AppState::new().with_song_library(library);
    state.jobs.write().await.insert(source.id, JobRecord {
        snapshot: source.clone(), cancelled: false, import_control: None,
        events: broadcast::channel(64).0,
    });
    (root, state, source)
}

async fn import(router: &Router, source: Uuid) -> Job {
    let (status, value) = call(router, "POST", "/jobs", json!({
        "kind":"song.import", "song_import":{"analysis_job_id":source},
    })).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{value}");
    let created: Job = serde_json::from_value(value).unwrap();
    assert_eq!(created.status, JobStatus::Queued);
    let wait = async {
        loop {
            let (status, value) = call(router, "GET", &format!("/jobs/{}", created.id), Value::Null).await;
            assert_eq!(status, StatusCode::OK);
            let job: Job = serde_json::from_value(value).unwrap();
            if job.status.terminal() { break job; }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    };
    tokio::time::timeout(Duration::from_secs(10), wait).await.unwrap()
}

#[tokio::test]
async fn import_job_publishes_audio_and_library_identity_then_reuses_it() {
    let (root, state, source) = setup().await;
    let router = app(state.clone());
    let done = import(&router, source.id).await;
    assert_eq!(done.status, JobStatus::Completed, "{:?}", done.error);
    let registered = done.imported_song.unwrap();
    assert!(!registered.reused && done.artifacts.is_empty());
    let (status, page) = call(&router, "GET", "/songs?limit=1", Value::Null).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(page["songs"][0]["song_id"], registered.song_id.to_string());
    let (status, bundle) = call(&router, "GET", &format!("/songs/{}", registered.song_id), Value::Null).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(bundle["song"]["title"], "合成・登録テスト");
    let directory = root.path().join("songs").join(registered.song_id.to_string());
    for name in ["original_48k.wav", "vocals.wav", "accompaniment.wav"] {
        assert!(directory.join("audio").join(name).is_file());
    }
    let retry = import(&router, source.id).await;
    assert_eq!(retry.imported_song.unwrap().song_id, registered.song_id);
    assert!(retry.imported_song.unwrap().reused);
    assert_eq!(state.snapshot(source.id).await.unwrap().artifacts, source.artifacts);
    assert!(state.snapshot(source.id).await.unwrap().imported_song.is_none());
}

#[tokio::test]
async fn invalid_source_job_and_import_requests_have_explicit_errors() {
    let (_root, state, source) = setup().await;
    let router = app(state.clone());
    for (body, expected) in [
        (json!({"kind":"song.import"}), StatusCode::BAD_REQUEST),
        (json!({"kind":"test","song_import":{"analysis_job_id":source.id}}), StatusCode::BAD_REQUEST),
        (json!({"kind":"song.import","song_import":{"analysis_job_id":Uuid::new_v4()}}), StatusCode::NOT_FOUND),
        (json!({"kind":"song.import","song_import":{"analysis_job_id":source.id,"path":"../escape"}}), StatusCode::UNPROCESSABLE_ENTITY),
    ] {
        assert_eq!(call(&router, "POST", "/jobs", body).await.0, expected);
    }
    state.jobs.write().await.get_mut(&source.id).unwrap().snapshot.status = JobStatus::Running;
    assert_eq!(call(&router, "POST", "/jobs", json!({"kind":"song.import","song_import":{"analysis_job_id":source.id}})).await.0, StatusCode::CONFLICT);
    assert_eq!(call(&app(AppState::new()), "GET", "/songs", Value::Null).await.0, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(call(&router, "GET", "/songs?limit=0", Value::Null).await.0, StatusCode::BAD_REQUEST);
    assert_eq!(call(&router, "GET", &format!("/songs/{}", Uuid::new_v4()), Value::Null).await.0, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn copy_failure_does_not_advertise_imported_song_or_mutate_source_job() {
    let (root, state, source) = setup().await;
    std::fs::remove_file(root.path().join("artifacts").join(&source.artifacts[0])).unwrap();
    let router = app(state.clone());
    let failed = import(&router, source.id).await;
    assert_eq!(failed.status, JobStatus::Failed);
    assert!(failed.imported_song.is_none() && failed.artifacts.is_empty());
    assert!(failed.error.is_some());
    assert_eq!(call(&router, "GET", "/songs", Value::Null).await.1["songs"], json!([]));
    assert_eq!(state.snapshot(source.id).await.unwrap().status, JobStatus::Completed);
}

#[tokio::test]
async fn bounded_io_and_shutdown_stop_new_work_and_revoke_pending_imports() {
    let (_root, state, source) = setup().await;
    let router = app(state.clone());
    let library = state.song_library.as_ref().unwrap();
    let permits = Arc::clone(&library.budget).acquire_many_owned(4).await.unwrap();
    assert_eq!(call(&router, "GET", "/songs", Value::Null).await.0, StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(call(&router, "POST", "/jobs", json!({"kind":"song.import","song_import":{"analysis_job_id":source.id}})).await.0, StatusCode::TOO_MANY_REQUESTS);
    drop(permits);
    let control = Arc::new(ImportControl::default());
    {
        let mut jobs = state.jobs.write().await;
        let record = jobs.get_mut(&source.id).unwrap();
        record.snapshot.status = JobStatus::Running;
        record.import_control = Some(Arc::clone(&control));
    }
    state.begin_shutdown().await;
    assert!(control.checkpoint().is_err());
    assert!(state.cancelled(source.id).await);
    assert_eq!(call(&router, "GET", "/songs", Value::Null).await.0, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(call(&router, "POST", "/jobs", json!({"kind":"test"})).await.0, StatusCode::SERVICE_UNAVAILABLE);
}

#[tokio::test]
async fn song_routes_keep_origin_and_version_guards() {
    let (_root, state, _) = setup().await;
    let router = app(state);
    for (name, value, expected) in [
        ("origin", "https://example.invalid", StatusCode::FORBIDDEN),
        ("x-open-karaoke-api-version", "2", StatusCode::UPGRADE_REQUIRED),
    ] {
        let request = Request::builder().uri("/songs").header(name, value).body(Body::empty()).unwrap();
        assert_eq!(router.clone().oneshot(request).await.unwrap().status(), expected);
    }
}
