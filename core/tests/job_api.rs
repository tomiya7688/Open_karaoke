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
    audio_engine::AudioIo,
    karaoke_session::{CommandEnvelope, KaraokeCommand, ScoringMode},
    karaoke_session_api::KaraokeSessionHost,
    model_plan::ModelSource,
};
use serde_json::{Value, json};
use tokio::time::{Instant, sleep, timeout};
use tokio_stream::StreamExt;
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
                .header("x-open-karaoke-api-version", "1")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 1_048_576).await.unwrap();
    (status, serde_json::from_slice(&bytes).unwrap())
}

/// Framework body rejections are plain text, so only the status is compared.
async fn put_status(router: &Router, path: &str, body: &Value) -> StatusCode {
    router
        .clone()
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri(path)
                .header("content-type", "application/json")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap()
        .status()
}

#[tokio::test]
async fn mixer_settings_are_read_clamped_written_and_shared_with_the_engine() {
    let state = AppState::new();
    let control = state.mixer_control();
    let router = app(state);
    let (status, value) = call(&router, "GET", "/mixer", Value::Null).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(value["mic_monitor"], false, "monitoring is opt-in");
    assert_eq!(value["echo_delay_ms"], 200);
    let wild = json!({
        "music_gain": 9.0, "mic_gain": 0.5, "echo_level": 0.4, "echo_delay_ms": 5000,
        "echo_feedback": 0.5, "reverb_level": -1.0, "effects_bypass": true, "mic_monitor": true
    });
    let (status, value) = call(&router, "PUT", "/mixer", wild).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(value["music_gain"], 2.0);
    assert_eq!(value["echo_delay_ms"], 1000);
    assert_eq!(value["reverb_level"], 0.0);
    assert_eq!(value["effects_bypass"], true);
    let (_, again) = call(&router, "GET", "/mixer", Value::Null).await;
    assert_eq!(again, value, "GET returns what PUT applied");
    let applied = control.get();
    assert!(applied.mic_monitor && applied.mixer.effects_bypass);
    assert_eq!(applied.mixer.echo_delay_frames, 48_000);
    let missing = json!({"music_gain": 1.0});
    assert_eq!(
        put_status(&router, "/mixer", &missing).await,
        StatusCode::UNPROCESSABLE_ENTITY
    );
    let mut unknown = value.clone();
    unknown["surprise"] = json!(1);
    assert_eq!(
        put_status(&router, "/mixer", &unknown).await,
        StatusCode::UNPROCESSABLE_ENTITY
    );
    assert_eq!(control.get(), applied, "refused writes change nothing");
}

#[tokio::test]
async fn model_selection_needs_the_analysis_service_and_a_valid_body() {
    let router = app(AppState::new());
    let (status, _) = call(&router, "GET", "/model-selection", Value::Null).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    let body = json!({"role":"mock", "model_id":"mock-v1"});
    let (status, _) = call(&router, "POST", "/model-selection", body).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
}

async fn verify_model_selection(router: &Router) {
    let (status, value) = call(router, "GET", "/model-selection", Value::Null).await;
    assert_eq!((status, &value["selection"]), (StatusCode::OK, &json!({})));
    let choose = |id: Value| json!({"role":"mock", "model_id": id});
    let (status, value) = call(router, "POST", "/model-selection", choose(json!("mock-v1"))).await;
    assert_eq!(
        (status, &value["selection"]["mock"]),
        (StatusCode::OK, &json!("mock-v1"))
    );
    let (status, _) = call(router, "POST", "/model-selection", choose(json!("ghost"))).await;
    assert_eq!(status, StatusCode::CONFLICT);
    let (status, _) = call(router, "POST", "/model-selection", json!({"role":"nope"})).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    let (status, value) = call(router, "POST", "/model-selection", choose(Value::Null)).await;
    assert_eq!((status, &value["selection"]), (StatusCode::OK, &json!({})));
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
    assert_eq!(value["api_version"], 1);
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
async fn input_artifact_references_are_validated_before_admission() {
    // No worker is configured, so a syntactically valid reference reaches the
    // worker lease (503) while invalid ones are rejected first (400).
    let router = app(AppState::new());
    let too_long = "a".repeat(1025);
    for (reference, code) in [
        ("../outside.wav", "invalid_artifact_ref"),
        ("C:/Users/x.wav", "invalid_artifact_ref"),
        ("https://example.invalid/a.wav", "invalid_artifact_ref"),
        ("inputs\\a.wav", "invalid_artifact_ref"),
        ("/abs.wav", "invalid_artifact_ref"),
        ("", "invalid_artifact_ref"),
        (too_long.as_str(), "artifact_ref_too_long"),
    ] {
        let (status, body) = call(
            &router,
            "POST",
            "/jobs",
            json!({"kind":"analysis.stems","analysis":{"input_artifact":reference}}),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{reference:?}");
        let error = body["error"].as_str().unwrap();
        assert!(error.starts_with(code), "{error}");
        assert!(
            !error.contains(reference) || reference.is_empty(),
            "{error}"
        );
    }
    let (status, _) = call(
        &router,
        "POST",
        "/jobs",
        json!({"kind":"analysis.stems","analysis":{"input_artifact":"inputs/song.wav"}}),
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
}

#[tokio::test]
async fn api_version_and_browser_origin_rejections() {
    let router = app(AppState::new());
    for (name, value, expected) in [
        (
            "x-open-karaoke-api-version",
            "2",
            StatusCode::UPGRADE_REQUIRED,
        ),
        ("origin", "https://example.invalid", StatusCode::FORBIDDEN),
    ] {
        let response = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/jobs")
                    .header("content-type", "application/json")
                    .header(name, value)
                    .body(Body::from(r#"{"kind":"test"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), expected);
    }

    // Existing native callers without the version header remain on v1.
    let response = router
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/jobs")
                .header("content-type", "application/json")
                .body(Body::from(r#"{"kind":"test","steps":1}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::ACCEPTED);
}

#[tokio::test]
async fn oversized_job_request_is_rejected() {
    let router = app(AppState::new());
    let response = router
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/jobs")
                .header("content-type", "application/json")
                .body(Body::from("x".repeat(65_537)))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::PAYLOAD_TOO_LARGE);
}

#[tokio::test]
async fn active_job_budget_limits_concurrency_and_recovers() {
    let router = app(AppState::new());
    let mut jobs = Vec::new();
    for _ in 0..16 {
        jobs.push(create(&router, json!({"kind": "test", "steps": 100})).await);
    }
    let (status, response) = call(
        &router,
        "POST",
        "/jobs",
        json!({"kind": "test", "steps": 100}),
    )
    .await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS, "{response}");

    for id in &jobs {
        let (status, _) = call(&router, "POST", &format!("/jobs/{id}/cancel"), Value::Null).await;
        assert_eq!(status, StatusCode::OK);
    }
    assert_eq!(
        terminal(&router, jobs[0]).await.status,
        JobStatus::Cancelled,
    );
    let next = create(&router, json!({"kind": "test", "steps": 1})).await;
    assert_eq!(terminal(&router, next).await.status, JobStatus::Completed);
}

#[tokio::test]
async fn sse_reconnect_starts_with_terminal_snapshot() {
    let router = app(AppState::new());
    let id = create(&router, json!({"kind": "test", "steps": 1})).await;
    assert_eq!(terminal(&router, id).await.status, JobStatus::Completed);

    let response = router
        .clone()
        .oneshot(
            Request::builder()
                .method("GET")
                .uri(format!("/jobs/{id}/events"))
                .header("x-open-karaoke-api-version", "1")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let mut stream = response.into_body().into_data_stream();
    let first = timeout(Duration::from_secs(2), stream.next())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    let event = String::from_utf8_lossy(&first);
    assert!(event.contains(&id.to_string()), "{event}");
    assert!(event.contains("completed"), "{event}");
}

/// {
///   責務: [`open_events`: Jobのevents SSEへ接続する]
///   処理: [1: api versionヘッダー付きGETを送る]
///   引数: [router: 接続先, id: 対象Job]
///   戻り値: [SSEのHTTP応答。本文を保持する間は接続枠を使う]
/// }
async fn open_events(router: &Router, id: Uuid) -> axum::response::Response {
    router
        .clone()
        .oneshot(
            Request::builder()
                .method("GET")
                .uri(format!("/jobs/{id}/events"))
                .header("x-open-karaoke-api-version", "1")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap()
}

/// {
///   責務: [`sse_connection_budget_limits_and_recovers`: SSE同時接続の上限と枠の返却を確認する]
///   処理: [
///     1: 実行中Jobへ32接続して全て受理されることを確認する,
///     2: 33件目が429になり、本文がJSONのエラーであることを確認する,
///     3: 別のJobへの接続も429になり、上限が全Job合計であることを確認する,
///     4: 1接続を切断すると受理され、再び満杯になると429に戻ることを確認する
///   ]
///   引数: []
///   戻り値: []
/// }
#[tokio::test]
async fn sse_connection_budget_limits_and_recovers() {
    let router = app(AppState::new());
    let id = create(&router, json!({"kind": "test", "steps": 100})).await;
    let other = create(&router, json!({"kind": "test", "steps": 100})).await;

    let mut open = Vec::new();
    for _ in 0..32 {
        let response = open_events(&router, id).await;
        assert_eq!(response.status(), StatusCode::OK);
        open.push(response);
    }
    let rejected = open_events(&router, id).await;
    assert_eq!(rejected.status(), StatusCode::TOO_MANY_REQUESTS);
    let body = axum::body::to_bytes(rejected.into_body(), usize::MAX)
        .await
        .unwrap();
    let error: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(error["error"], "Too many SSE connections");
    assert_eq!(
        open_events(&router, other).await.status(),
        StatusCode::TOO_MANY_REQUESTS,
        "the budget is shared by every job"
    );

    // 本文を破棄すると接続枠が返り、次の接続を受け付ける。返した1枠だけが再利用される。
    drop(open.pop());
    let reconnected = open_events(&router, other).await;
    assert_eq!(reconnected.status(), StatusCode::OK);
    open.push(reconnected);
    assert_eq!(
        open_events(&router, id).await.status(),
        StatusCode::TOO_MANY_REQUESTS
    );

    for job in [id, other] {
        let (status, _) = call(&router, "POST", &format!("/jobs/{job}/cancel"), Value::Null).await;
        assert_eq!(status, StatusCode::OK);
    }
}

/// {
///   責務: [`sse_closes_after_terminal_event`: 終端Jobのイベント送信後に接続が閉じることを確認する]
///   処理: [
///     1: 完了済みJobへ接続して終端snapshotを受け取る,
///     2: 次の読み取りでストリームが終了することを確認する,
///     3: 閉じた接続の枠が返り、上限まで再接続できることを確認する
///   ]
///   引数: []
///   戻り値: []
/// }
#[tokio::test]
async fn sse_closes_after_terminal_event() {
    let router = app(AppState::new());
    let id = create(&router, json!({"kind": "test", "steps": 1})).await;
    assert_eq!(terminal(&router, id).await.status, JobStatus::Completed);

    let mut stream = open_events(&router, id)
        .await
        .into_body()
        .into_data_stream();
    let first = timeout(Duration::from_secs(2), stream.next())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert!(String::from_utf8_lossy(&first).contains("completed"));
    assert!(
        timeout(Duration::from_secs(2), stream.next())
            .await
            .unwrap()
            .is_none(),
        "stream must end after the terminal event"
    );

    // 上限(32)を超える40回、完了済みJobへ接続しては読み終えて閉じる。毎回200で終端snapshotを受け取り、
    // 閉じた接続の枠が返るので上限に達しない。
    for _ in 0..40 {
        let response = open_events(&router, id).await;
        assert_eq!(response.status(), StatusCode::OK);
        let mut stream = response.into_body().into_data_stream();
        let mut body = Vec::new();
        while let Some(chunk) = timeout(Duration::from_secs(2), stream.next())
            .await
            .unwrap()
        {
            body.extend_from_slice(&chunk.unwrap());
        }
        // 文字列の部分一致ではなく、job eventを1件だけ解析して終端snapshotであることを確かめる。
        let text = String::from_utf8(body).unwrap();
        let events: Vec<&str> = text.split("\n\n").filter(|e| !e.is_empty()).collect();
        assert_eq!(events.len(), 1, "{text}");
        let mut lines = events[0].lines();
        assert_eq!(lines.next(), Some("event: job"), "{text}");
        let data = lines.next().and_then(|line| line.strip_prefix("data: "));
        let event: Value = serde_json::from_str(data.unwrap()).unwrap();
        assert!(lines.next().is_none(), "{text}");
        assert_eq!(event["job"]["id"], id.to_string());
        assert_eq!(event["job"]["status"], "completed");
    }
    // 枠が本当に返っていれば、ループ後も上限ちょうどまで同時に保持でき、33本目だけが拒否される。
    // 冒頭で読み終えた接続はここで手放す(テスト内のoneshotでは本文を破棄するまで枠が残るため)。
    drop(stream);
    let mut held = Vec::new();
    for _ in 0..32 {
        let response = open_events(&router, id).await;
        assert_eq!(response.status(), StatusCode::OK);
        held.push(response);
    }
    assert_eq!(
        open_events(&router, id).await.status(),
        StatusCode::TOO_MANY_REQUESTS
    );
}

#[tokio::test]
async fn missing_python_has_actionable_error() {
    let root = tempfile::tempdir().unwrap();
    let result = AnalysisService::start(AnalysisConfig {
        python: root.path().join("no-python-here"),
        artifact_root: root.path().join("artifacts"),
        startup_timeout: Duration::from_secs(1),
        worker_priority: open_karaoke_core::inference_policy::WorkerPriority::Inherit,
    })
    .await;
    assert!(result.err().unwrap().contains("analysis_spawn"));
}

async fn verify_song_input_errors(router: &Router) {
    let invalid_song = create(router, json!({"kind":"analysis.song"})).await;
    assert!(
        terminal(router, invalid_song)
            .await
            .error
            .unwrap()
            .contains("invalid_options")
    );
    let missing_source = create(
        router,
        json!({"kind":"analysis.song", "analysis":{"options":{"title":"missing input"}}}),
    )
    .await;
    assert!(
        terminal(router, missing_source)
            .await
            .error
            .unwrap()
            .contains("input_required")
    );
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
        worker_priority: open_karaoke_core::inference_policy::WorkerPriority::Inherit,
    })
    .await
    .unwrap();
    let client = service.client();
    let router = app(AppState::with_analysis(client.clone()));
    let health = client.health().await.unwrap();
    assert_eq!(client.models().await.unwrap()["models"][0]["id"], "mock-v1");
    verify_model_selection(&router).await;
    let a = create(&router, json!({"kind":"analysis.mock"})).await;
    let result = terminal(&router, a).await;
    assert_eq!(result.status, JobStatus::Completed);
    assert_eq!(result.model_id.as_deref(), Some("mock-v1"));
    assert_eq!(result.model_source, Some(ModelSource::Default));
    assert_eq!(result.require_exact_model_plan(), Ok(()));
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
    verify_song_input_errors(&router).await;
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

#[cfg(windows)]
#[tokio::test]
#[ignore = "Requires installed Python analysis package; explicitly executed in CI"]
async fn windows_venv_redirector_python_completes_a_job() {
    // #82: a venv `Scripts\python.exe` re-spawns the base interpreter, so its PID differs.
    let root = tempfile::tempdir().unwrap();
    let python = std::env::var_os("OPEN_KARAOKE_PYTHON")
        .map_or_else(|| PathBuf::from("python"), PathBuf::from);
    let venv = root.path().join("venv");
    let created = std::process::Command::new(&python)
        .args(["-m", "venv", "--without-pip"])
        .arg(&venv)
        .status()
        .unwrap();
    assert!(created.success());
    // Reuse the configured interpreter's installed dependencies inside the new venv.
    let purelib = std::process::Command::new(&python)
        .args([
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ])
        .output()
        .unwrap();
    let purelib = String::from_utf8(purelib.stdout).unwrap();
    let source = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../python/src");
    // Venv site-packages are processed only when the worker really runs as this venv, so the
    // recorded prefix proves `__PYVENV_LAUNCHER__` took effect. Rust's `{:?}` string escaping is a
    // valid Python literal, including paths with quotes or apostrophes.
    let prefix_file = root.path().join("worker-prefix.txt");
    std::fs::write(
        venv.join("Lib/site-packages/open_karaoke_dev.pth"),
        format!(
            "import site; site.addsitedir({:?})\n{}\nimport sys, pathlib; pathlib.Path({:?}).write_text(sys.prefix)\n",
            purelib.trim(),
            source.display(),
            prefix_file.to_string_lossy()
        ),
    )
    .unwrap();
    let service = AnalysisService::start(AnalysisConfig {
        python: venv.join("Scripts/python.exe"),
        artifact_root: root.path().join("artifacts"),
        startup_timeout: Duration::from_secs(30),
        worker_priority: open_karaoke_core::inference_policy::WorkerPriority::Inherit,
    })
    .await
    .unwrap();
    let router = app(AppState::with_analysis(service.client()));
    let job = create(&router, json!({"kind":"analysis.mock"})).await;
    assert_eq!(terminal(&router, job).await.status, JobStatus::Completed);
    service.shutdown().await;
    let prefix = std::fs::read_to_string(&prefix_file).unwrap();
    assert_eq!(
        std::fs::canonicalize(prefix.trim()).unwrap(),
        std::fs::canonicalize(&venv).unwrap()
    );
}

/// {
///   責務: [`PlayingFixture`: 再生中(playing)のセッションを持つAppStateと、同時に回る模擬音声経路]
///   フィールド: [state, router, host, session, instance, audio: mock出力を回す`AudioIo`]
/// }
struct PlayingFixture {
    router: Router,
    host: KaraokeSessionHost,
    session: Uuid,
    instance: Uuid,
    audio: AudioIo,
}

/// {
///   責務: [`playing_fixture`: 指定のAppStateでセッションをplayingにし、出力を有効にした`AudioIo`を作る]
///   引数: [state: 設定済みの`AppState`, instance: 世代ID]
///   戻り値: [`PlayingFixture`]
/// }
fn playing_fixture(state: AppState, instance: Uuid) -> PlayingFixture {
    let host = state.karaoke_host();
    let snapshot = host
        .create(Uuid::new_v4(), 48_000 * 10, ScoringMode::WholeSong)
        .unwrap();
    assert!(host.finish_preparing(snapshot.session_id, Ok(())).unwrap());
    let ready = host.snapshot(snapshot.session_id).unwrap();
    host.execute(CommandEnvelope {
        core_instance_id: instance,
        session_id: snapshot.session_id,
        command_id: Uuid::new_v4(),
        expected_revision: ready.state_revision,
        command: KaraokeCommand::Play,
    })
    .unwrap();
    let audio = AudioIo::new(48_000).unwrap();
    audio.set_playing(true);
    PlayingFixture {
        router: app(state),
        host,
        session: snapshot.session_id,
        instance,
        audio,
    }
}

impl PlayingFixture {
    /// {
    ///   責務: [`pump`: mock出力callbackを1回回して出力位置が進むことを確認する]
    ///   戻り値: [回した後の出力位置(frame)]
    /// }
    fn pump(&self) -> u64 {
        let before = self.audio.counters().playback_sample_position;
        let mut buffer = vec![0.0_f32; 480 * 2];
        self.audio.render_mock(&mut buffer, 2);
        let after = self.audio.counters().playback_sample_position;
        assert!(after > before, "audio output must keep advancing");
        after
    }
}

#[tokio::test]
async fn job_failure_and_cancel_do_not_disturb_playback() {
    // #43 slice 43.4 (contract only; real device underrun behavior stays Unverified).
    let instance = Uuid::new_v4();
    let f = playing_fixture(AppState::new().with_core_instance_id(instance), instance);
    let before = f.host.snapshot(f.session).unwrap();
    f.pump();
    let failing = create(&f.router, json!({"kind":"test","steps":3,"fail_at_step":2})).await;
    let cancelled = create(&f.router, json!({"kind":"test","steps":100})).await;
    call(
        &f.router,
        "POST",
        &format!("/jobs/{cancelled}/cancel"),
        Value::Null,
    )
    .await;
    f.pump();
    assert_eq!(terminal(&f.router, failing).await.status, JobStatus::Failed);
    assert_eq!(
        terminal(&f.router, cancelled).await.status,
        JobStatus::Cancelled
    );
    f.pump();
    assert_eq!(
        f.host.snapshot(f.session).unwrap(),
        before,
        "Job outcomes never touch the playback session"
    );
    assert_eq!(f.instance, before.core_instance_id);
}

#[tokio::test]
#[ignore = "Requires installed Python analysis package; explicitly executed in CI"]
async fn python_heavy_job_defers_while_playing_and_worker_crash_keeps_playback() {
    let root = tempfile::tempdir().unwrap();
    let python = std::env::var_os("OPEN_KARAOKE_PYTHON")
        .map_or_else(|| PathBuf::from("python"), PathBuf::from);
    let service = AnalysisService::start(AnalysisConfig {
        python: python.clone(),
        artifact_root: root.path().join("artifacts"),
        startup_timeout: Duration::from_secs(30),
        worker_priority: open_karaoke_core::inference_policy::WorkerPriority::Inherit,
    })
    .await
    .unwrap();
    let client = service.client();
    let health = client.health().await.unwrap();
    let instance = Uuid::new_v4();
    let f = playing_fixture(
        AppState::with_analysis(client.clone()).with_core_instance_id(instance),
        instance,
    );
    let before = f.host.snapshot(f.session).unwrap();
    // 43.2: a heavy job stays queued while playing, then can be cancelled.
    let heavy = create(&f.router, json!({"kind":"analysis.stems"})).await;
    sleep(Duration::from_millis(300)).await;
    let (_, value) = call(&f.router, "GET", &format!("/jobs/{heavy}"), Value::Null).await;
    assert_eq!(value["status"], "queued", "{value}");
    assert_eq!(value["deferred_reason"], "playback_active");
    call(
        &f.router,
        "POST",
        &format!("/jobs/{heavy}/cancel"),
        Value::Null,
    )
    .await;
    assert_eq!(
        terminal(&f.router, heavy).await.status,
        JobStatus::Cancelled
    );
    // 43.4: a worker crash fails only the job; output and session continue.
    let running = create(
        &f.router,
        json!({"kind":"analysis.mock", "analysis":{"options":{"steps":100,"delay_ms":100}}}),
    )
    .await;
    sleep(Duration::from_millis(200)).await;
    f.pump();
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
    assert_eq!(terminal(&f.router, running).await.status, JobStatus::Failed);
    f.pump();
    assert_eq!(f.host.snapshot(f.session).unwrap(), before);
    service.shutdown().await;
}
