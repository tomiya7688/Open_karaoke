//! Contract tests with an injected normalizer; real `FFmpeg` output is covered by
//! `audio_import` probe/argument tests and is Unverified here.

use std::{
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

use axum::{
    Router,
    body::{Body, to_bytes},
    http::{Request, StatusCode},
};
use serde_json::{Value, json};
use tower::ServiceExt;

use super::{AudioImportConfig, Normalizer};
use crate::{
    AppState, Job, JobStatus, app,
    audio_import::{AudioImportError, NormalizedAudioMetadata},
};

/// {
///   責務: [`call`: テスト用に`oneshot`で`Router`へ`JSON`リクエストを送る]
///   処理: [1: API版ヘッダ付きリクエストを作る, 2: 送信して状態を得る, 3: 本文をJSONに読み替える(失敗時はNull)]
///   引数: [`router`: 対象Router, `method`: HTTPメソッド, `path`: URI, `body`: JSON本文]
///   戻り値: [状態コードとJSON本文]
/// }
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
    let bytes = to_bytes(response.into_body(), 1024 * 1024).await.unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

/// {
///   責務: [`Harness`: 音声取り込みテストの一時ディレクトリ・入力ファイル・Routerの束]
///   フィールド: [`root`: 一時ルート, `source`: 偽の入力mp3, `router`: 取り込み設定済みRouter]
/// }
struct Harness {
    root: tempfile::TempDir,
    source: std::path::PathBuf,
    router: Router,
}

/// {
///   責務: [`harness`: 差し替えた正規化関数でテスト用`Harness`を作る]
///   処理: [1: 一時ディレクトリと偽の入力を作る, 2: 取り込み設定を持つ`AppState`でRouterを作る]
///   引数: [`normalizer`: テスト用の正規化関数]
///   戻り値: [`Harness`]
/// }
fn harness(normalizer: Normalizer) -> Harness {
    let root = tempfile::tempdir().unwrap();
    let source = root.path().join("source.mp3");
    std::fs::write(&source, b"not decoded by the fake normalizer").unwrap();
    let config = AudioImportConfig::with_normalizer(root.path().join("artifacts"), normalizer);
    let router = app(AppState::new().with_audio_import(config));
    Harness {
        root,
        source,
        router,
    }
}

impl Harness {
    /// {
    ///   責務: [`Harness::submit`: `audio.import` Jobを投入し受理(202)を確認する]
    ///   処理: [1: `POST /jobs`を送る, 2: 202を検証する, 3: `Job`に変換する]
    ///   引数: [`&self`]
    ///   戻り値: [作成された`Job`]
    /// }
    async fn submit(&self) -> Job {
        let (status, value) = call(
            &self.router,
            "POST",
            "/jobs",
            json!({"kind":"audio.import","audio_import":{"source_path":self.source}}),
        )
        .await;
        assert_eq!(status, StatusCode::ACCEPTED, "{value}");
        serde_json::from_value(value).unwrap()
    }

    /// {
    ///   責務: [`Harness::terminal`: Jobが終端状態になるまで10秒を上限に問い合わせる]
    ///   処理: [1: 5ms間隔で`GET /jobs/{id}`する, 2: 終端状態で返す, 3: 10秒で時間切れ]
    ///   引数: [`&self`, `job`: 対象Job]
    ///   戻り値: [終端状態の`Job`]
    /// }
    async fn terminal(&self, job: &Job) -> Job {
        let wait = async {
            loop {
                let (_, value) = call(
                    &self.router,
                    "GET",
                    &format!("/jobs/{}", job.id),
                    Value::Null,
                )
                .await;
                let job: Job = serde_json::from_value(value).unwrap();
                if job.status.terminal() {
                    break job;
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        };
        tokio::time::timeout(Duration::from_secs(10), wait)
            .await
            .unwrap()
    }

    /// {
    ///   責務: [`Harness::import_entries`: importsディレクトリ直下のエントリ名を列挙する]
    ///   処理: [1: ディレクトリを読む(無ければ空), 2: ファイル名を文字列化する]
    ///   引数: [`&self`]
    ///   戻り値: [エントリ名の一覧]
    /// }
    fn import_entries(&self) -> Vec<String> {
        let imports = self.root.path().join("artifacts/imports");
        let Ok(entries) = std::fs::read_dir(imports) else {
            return Vec::new();
        };
        entries
            .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
            .collect()
    }
}

/// {
///   責務: [`metadata`: 48kHz・2ch・指定長のテスト用メタデータを作る]
///   処理: [1: 固定値で構造体を組み立てる]
///   引数: [`duration_samples`: サンプル数]
///   戻り値: [`NormalizedAudioMetadata`]
/// }
fn metadata(duration_samples: u64) -> NormalizedAudioMetadata {
    NormalizedAudioMetadata {
        sample_rate: 48_000,
        channels: 2,
        duration_samples,
    }
}

/// {
///   責務: [`normalized_input_is_published_as_analysis_ready_artifacts`: 正規化結果が解析可能な成果物として公開されることを検証する]
///   処理: [1: 偽の正規化でWAVを書く, 2: Jobを完了させる, 3: 成果物とサマリ内容・import一覧を確認する]
///   引数: [[]]
///   戻り値: [[]]
/// }
#[tokio::test]
async fn normalized_input_is_published_as_analysis_ready_artifacts() {
    let test = harness(Arc::new(|input, output, _| {
        assert!(input.ends_with("source.mp3"));
        std::fs::write(output, b"RIFF fake wav").unwrap();
        Ok(metadata(9600))
    }));
    let job = test.submit().await;
    assert_eq!(job.status, JobStatus::Queued);
    let done = test.terminal(&job).await;
    assert_eq!(done.status, JobStatus::Completed, "{:?}", done.error);
    let audio = format!("imports/{}/original_48k.wav", job.id);
    assert_eq!(
        done.artifacts,
        [audio.clone(), format!("imports/{}/normalized.json", job.id)]
    );
    for artifact in &done.artifacts {
        crate::artifact_ref::validate_ref(artifact).unwrap();
    }
    let artifacts = test.root.path().join("artifacts");
    assert_eq!(
        std::fs::read(artifacts.join(&audio)).unwrap(),
        b"RIFF fake wav"
    );
    let summary: Value =
        serde_json::from_slice(&std::fs::read(artifacts.join(&done.artifacts[1])).unwrap())
            .unwrap();
    assert_eq!(summary["duration_samples"], 9600);
    assert_eq!(summary["sample_rate"], 48_000);
    assert_eq!(summary["audio_artifact"], audio);
    assert_eq!(test.import_entries(), [job.id.to_string()]);
}

/// {
///   責務: [`failures_have_codes_and_never_leave_partial_input`: 失敗がコード付きで報告され、部分入力が残らないことを検証する]
///   処理: [1: 各種失敗を返す正規化関数を用意する, 2: failedとコードを確認する, 3: importsに何も残らないことを確認する]
///   引数: [[]]
///   戻り値: [[]]
/// }
#[tokio::test]
async fn failures_have_codes_and_never_leave_partial_input() {
    let cases: [(Normalizer, &str); 3] = [
        (
            Arc::new(|_, output, _| {
                std::fs::write(output, b"partial").unwrap();
                Err(AudioImportError::ToolUnavailable {
                    tool: "ffmpeg".to_owned(),
                    error: std::io::ErrorKind::NotFound.into(),
                })
            }),
            "audio_tool_unavailable",
        ),
        (
            Arc::new(|_, _, _| Err(AudioImportError::LicensePolicy("gpl".to_owned()))),
            "audio_ffmpeg_license_policy",
        ),
        (
            Arc::new(|_, output, _| {
                std::fs::write(output, b"").unwrap();
                Ok(metadata(0))
            }),
            "audio_invalid_probe",
        ),
    ];
    for (normalizer, code) in cases {
        let test = harness(normalizer);
        let done = test.terminal(&test.submit().await).await;
        assert_eq!(done.status, JobStatus::Failed);
        assert!(done.error.as_deref().unwrap().starts_with(code), "{done:?}");
        assert_eq!(done.artifacts.len(), 0);
        assert!(
            test.import_entries().is_empty(),
            "{:?}",
            test.import_entries()
        );
    }
}

/// {
///   責務: [`cancellation_reaches_the_running_tool_and_publishes_nothing`: 中断が実行中ツールへ届き、何も公開されないことを検証する]
///   処理: [1: 中断判定を待つ正規化関数で開始を確認する, 2: Jobを中断する, 3: cancelledと未公開を確認する]
///   引数: [[]]
///   戻り値: [[]]
/// }
#[tokio::test]
async fn cancellation_reaches_the_running_tool_and_publishes_nothing() {
    let started = Arc::new(AtomicBool::new(false));
    let seen = Arc::clone(&started);
    let test = harness(Arc::new(move |_, output, cancelled| {
        std::fs::write(output, b"partial").unwrap();
        seen.store(true, Ordering::Release);
        while !cancelled() {
            std::thread::sleep(Duration::from_millis(5));
        }
        Err(AudioImportError::Cancelled)
    }));
    let job = test.submit().await;
    while !started.load(Ordering::Acquire) {
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    let path = format!("/jobs/{}/cancel", job.id);
    assert_eq!(
        call(&test.router, "POST", &path, Value::Null).await.0,
        StatusCode::OK
    );
    let done = test.terminal(&job).await;
    assert_eq!(done.status, JobStatus::Cancelled);
    assert!(done.artifacts.is_empty() && done.error.is_none());
    assert_eq!(test.import_entries().len(), 0);
}

/// {
///   責務: [`invalid_requests_are_rejected_before_a_job_exists`: 不正な要求がJob作成前に拒否されることを検証する]
///   処理: [1: ディレクトリ・未存在・非対応拡張子・相対パスを送る, 2: 400/404とJob未作成を確認する]
///   引数: [[]]
///   戻り値: [[]]
/// }
#[tokio::test]
async fn invalid_requests_are_rejected_before_a_job_exists() {
    let test = harness(Arc::new(|_, _, _| unreachable!()));
    let directory = test.root.path().join("folder.wav");
    std::fs::create_dir(&directory).unwrap();
    let missing = test.root.path().join("missing.wav");
    let unsupported = test.root.path().join("song.flac");
    std::fs::write(&unsupported, b"x").unwrap();
    let cases = [
        (json!({"kind":"audio.import"}), StatusCode::BAD_REQUEST),
        (
            json!({"kind":"audio.import","audio_import":{"source_path":"relative.wav"}}),
            StatusCode::BAD_REQUEST,
        ),
        (
            json!({"kind":"audio.import","audio_import":{"source_path":unsupported}}),
            StatusCode::BAD_REQUEST,
        ),
        (
            json!({"kind":"audio.import","audio_import":{"source_path":directory}}),
            StatusCode::BAD_REQUEST,
        ),
        (
            json!({"kind":"audio.import","audio_import":{"source_path":missing}}),
            StatusCode::NOT_FOUND,
        ),
        (
            json!({"kind":"mock","audio_import":{"source_path":test.source}}),
            StatusCode::BAD_REQUEST,
        ),
    ];
    for (body, expected) in cases {
        let (status, value) = call(&test.router, "POST", "/jobs", body.clone()).await;
        assert_eq!(status, expected, "{body} -> {value}");
    }
    let (status, _) = call(
        &test.router,
        "POST",
        "/jobs",
        json!({"kind":"audio.import","audio_import":{"source_path":test.source,"extra":1}}),
    )
    .await;
    assert!(status.is_client_error());
    let unconfigured = app(AppState::new());
    let body = json!({"kind":"audio.import","audio_import":{"source_path":test.source}});
    assert_eq!(
        call(&unconfigured, "POST", "/jobs", body).await.0,
        StatusCode::SERVICE_UNAVAILABLE
    );
    assert_eq!(test.import_entries().len(), 0);
}
