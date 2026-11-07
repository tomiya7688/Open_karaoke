//! Product Core APIs. Audio/DSP never depend on the offline analysis process.

pub mod analysis_service;
pub mod artifact_ref;
pub mod audio_clock;
pub mod audio_clock_calibration;
pub mod audio_device;
pub mod audio_engine;
pub mod audio_import;
pub mod audio_wav;
pub mod ffmpeg_bundle;
pub mod inference_policy;
pub mod karaoke_controller;
pub mod karaoke_session;
pub mod karaoke_session_api;
pub mod mixer;
pub mod mixer_api;
pub mod mixer_control;
pub mod model_plan;
pub mod runtime_layout;
pub mod song_api;
pub mod song_data;
pub mod song_edit;
pub mod song_library;

use std::{
    collections::HashMap,
    convert::Infallible,
    pin::Pin,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    task::{Context, Poll},
    time::Duration,
};

use analysis_service::{AnalysisClient, AnalysisRequest, RemoteJob};
use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Path, Request, State},
    http::{StatusCode, header},
    middleware::{self, Next},
    response::{
        IntoResponse, Response,
        sse::{Event, KeepAlive, Sse},
    },
    routing::{get, post},
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use tokio::sync::{OwnedSemaphorePermit, RwLock, Semaphore, broadcast};
use tokio_stream::{Stream, wrappers::BroadcastStream};
use uuid::Uuid;

/// Protocol version for the native GUI and test client contract.
pub const CORE_API_VERSION: u32 = 1;
pub const CORE_API_VERSION_HEADER: &str = "x-open-karaoke-api-version";
const MAX_CORE_REQUEST_BYTES: usize = 64 * 1024;
const MAX_ACTIVE_JOBS: usize = 16;
const MAX_RETAINED_JOBS: usize = 512;
const MAX_SSE_CONNECTIONS: usize = 32;
/// 再生中に重いJobを待機させる既定の上限時間。超えたらfailedにする(無期限にしない)。
const DEFAULT_DEFER_MAX_WAIT: Duration = Duration::from_mins(30);
/// 再生状態を確認し直す間隔。
const DEFER_POLL_INTERVAL: Duration = Duration::from_millis(50);
/// 待機中Jobの `deferred_reason` に入れる値(UIが「再生中のため待機」と表示する)。
pub const DEFERRED_REASON_PLAYBACK_ACTIVE: &str = "playback_active";
/// 重いJobの同時数上限で待機中の `deferred_reason`(Issue #43 43.3)。
pub const DEFERRED_REASON_HEAVY_JOB_LIMIT: &str = "heavy_job_limit";

#[derive(Clone)]
pub struct AppState {
    jobs: Arc<RwLock<HashMap<Uuid, JobRecord>>>,
    analysis: AnalysisClient,
    song_library: Option<song_library::SongLibrary>,
    stopping: Arc<AtomicBool>,
    sse_budget: Arc<Semaphore>,
    mixer: mixer_control::MixerControl,
    media_tools: Option<ffmpeg_bundle::MediaTools>,
    karaoke: karaoke_session_api::KaraokeSessionHost,
    inference: InferencePolicy,
    /// 重いJobの同時実行枠。`None`は無制限(既定、現行挙動)。
    heavy_slots: Option<Arc<Semaphore>>,
}

/// {
///   責務: [`InferencePolicy`: 再生中の重い推論Jobの扱い(Issue #43)。永続化せず起動時に保持する]
///   フィールド: [`defer_while_playing`: 再生中は重いJobの開始を待たせる, `defer_max_wait`: 待機の上限(超過でfailed)]
/// }
#[derive(Clone, Copy, Debug)]
struct InferencePolicy {
    defer_while_playing: bool,
    defer_max_wait: Duration,
}

impl Default for AppState {
    /// {
    ///   責務: [`default`: 既定のCore状態を作る]
    ///   処理: [1: 空のJob表を作る, 2: SSE接続予算を `MAX_SSE_CONNECTIONS` で初期化する]
    ///   引数: []
    ///   戻り値: [Job・解析client・SSE予算を持つAppState]
    /// }
    fn default() -> Self {
        Self {
            jobs: Arc::new(RwLock::new(HashMap::new())),
            analysis: AnalysisClient::default(),
            song_library: None,
            stopping: Arc::new(AtomicBool::new(false)),
            sse_budget: Arc::new(Semaphore::new(MAX_SSE_CONNECTIONS)),
            mixer: mixer_control::MixerControl::default(),
            media_tools: None,
            karaoke: karaoke_session_api::KaraokeSessionHost::new(Uuid::new_v4()),
            inference: InferencePolicy {
                defer_while_playing: true,
                defer_max_wait: DEFAULT_DEFER_MAX_WAIT,
            },
            heavy_slots: None,
        }
    }
}

struct JobRecord {
    snapshot: Job,
    cancelled: bool,
    import_control: Option<Arc<song_library::ImportControl>>,
    events: broadcast::Sender<JobEvent>,
}

/// {
///   責務: [`JobEventStream`: 1接続分のJob SSEを供給し、終端で閉じて接続枠を返す]
///   フィールド: [
///     initial: 接続時に最初に送るsnapshot,
///     updates: 以降の更新を受けるbroadcast,
///     closed: 終端または取りこぼしで閉じた状態,
///     _permit: 保持中だけ接続枠を占有するpermit
///   ]
/// }
struct JobEventStream {
    initial: Option<JobEvent>,
    updates: BroadcastStream<JobEvent>,
    closed: bool,
    _permit: OwnedSemaphorePermit,
}

impl Stream for JobEventStream {
    type Item = Result<Event, Infallible>;

    /// {
    ///   責務: [`poll_next`: 次のJob eventをSSE形式で返す]
    ///   処理: [
    ///     1: 閉じていれば終了する,
    ///     2: 初回はsnapshot、以降はbroadcastの更新を取り出す,
    ///     3: 取りこぼしまたは送信元終了なら閉じる,
    ///     4: 終端状態のeventを送ったら以降を閉じる
    ///   ]
    ///   引数: [cx: 非同期の起床context]
    ///   戻り値: [SSE event、待機、または終了]
    /// }
    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let stream = self.get_mut();
        if stream.closed {
            return Poll::Ready(None);
        }
        let event = if let Some(initial) = stream.initial.take() {
            initial
        } else {
            match Pin::new(&mut stream.updates).poll_next(cx) {
                Poll::Ready(Some(Ok(update))) => update,
                Poll::Ready(Some(Err(_)) | None) => {
                    stream.closed = true;
                    return Poll::Ready(None);
                }
                Poll::Pending => return Poll::Pending,
            }
        };
        stream.closed = event.job.status.terminal();
        Poll::Ready(Some(Ok(Event::default()
            .event("job")
            .data(serde_json::to_string(&event).unwrap_or_default()))))
    }
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
    /// Library identity is separate from worker-relative artifact paths.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub imported_song: Option<song_library::ImportedSong>,
    /// Adapter that actually ran (worker-validated); absent for non-analysis jobs.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model_id: Option<String>,
    /// How the model was chosen; separates "selected" from "actually ran".
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model_source: Option<model_plan::ModelSource>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub selected_model_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub selection_fallback_reason: Option<String>,
    /// Per-stage plan pinned at acceptance (`analysis.song` only).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model_plan: Option<model_plan::ModelPlan>,
    /// 開始待ちの理由(例: `playback_active`)。待機していない/開始後は無い。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deferred_reason: Option<String>,
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
    pub song_import: Option<song_api::SongImportRequest>,
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

    #[must_use]
    pub fn with_song_library(mut self, library: song_library::SongLibrary) -> Self {
        self.song_library = Some(library);
        self
    }

    /// { 処理: 解決済みの ffmpeg / ffprobe パスを設定する（取り込み処理が使う）。 }
    #[must_use]
    pub fn with_media_tools(mut self, tools: ffmpeg_bundle::MediaTools) -> Self {
        self.media_tools = Some(tools);
        self
    }

    /// { 戻り値: 解決済みの ffmpeg / ffprobe パス。未設定なら None。 }
    #[must_use]
    pub fn media_tools(&self) -> Option<&ffmpeg_bundle::MediaTools> {
        self.media_tools.as_ref()
    }

    /// Mixer settings shared with the audio engine; hand a clone to the device layer.
    #[must_use]
    pub fn mixer_control(&self) -> mixer_control::MixerControl {
        self.mixer.clone()
    }

    /// Replace the shared mixer settings so an engine opened with them starts from `control`.
    #[must_use]
    pub fn with_mixer_control(mut self, control: mixer_control::MixerControl) -> Self {
        self.mixer = control;
        self
    }

    /// { 処理: 歌唱セッションの世代IDを設定する。`GET /session` の `instance_id` と同じ値を渡し、旧世代のコマンドを拒否できるようにする。 }
    #[must_use]
    pub fn with_core_instance_id(mut self, core_instance_id: Uuid) -> Self {
        self.karaoke = karaoke_session_api::KaraokeSessionHost::new(core_instance_id);
        self
    }

    /// { 戻り値: 歌唱セッションhost。Controllerが作成・準備完了・提示位置・中断を報告する入口(クライアントAPIにはこれらが無い)。 }
    #[must_use]
    pub fn karaoke_host(&self) -> karaoke_session_api::KaraokeSessionHost {
        self.karaoke.clone()
    }

    /// {
    ///   責務: [`with_defer_while_playing`: 再生中に重いJobを待たせる方針を切り替える(既定は有効)]
    ///   引数: [enabled]
    ///   戻り値: [方針を反映した`AppState`]
    /// }
    #[must_use]
    pub fn with_defer_while_playing(mut self, enabled: bool) -> Self {
        self.inference.defer_while_playing = enabled;
        self
    }

    /// {
    ///   責務: [`with_defer_max_wait`: 再生中待機の上限時間を設定する(超過したJobはfailed)]
    ///   引数: [`max_wait`]
    ///   戻り値: [方針を反映した`AppState`]
    /// }
    #[must_use]
    pub fn with_defer_max_wait(mut self, max_wait: Duration) -> Self {
        self.inference.defer_max_wait = max_wait;
        self
    }

    /// {
    ///   責務: [`with_max_concurrent_heavy_jobs`: 重い推論Jobの同時実行数を制限する(`None`は無制限で既定)]
    ///   処理: [1: 上限数の`Semaphore`を作って保持する, 2: 待機は`defer_max_wait`で打ち切る]
    ///   引数: [limit: 1以上の上限。`None`なら制限なし]
    ///   戻り値: [方針を反映した`AppState`]
    /// }
    #[must_use]
    pub fn with_max_concurrent_heavy_jobs(mut self, limit: Option<std::num::NonZeroUsize>) -> Self {
        self.heavy_slots = limit.map(|n| Arc::new(Semaphore::new(n.get())));
        self
    }

    /// {
    ///   責務: [`playback_active`: 歌唱セッションが再生中(playing)かを返す。読み取りのみで状態を変えない]
    ///   処理: [1: セッションhostの現行snapshotを読む, 2: stateがplayingか判定する(paused/interrupted/準備中は再生中でない)]
    ///   戻り値: [`bool`: 再生中ならtrue]
    /// }
    #[must_use]
    pub fn playback_active(&self) -> bool {
        self.karaoke
            .current_snapshot()
            .is_some_and(|snapshot| snapshot.state == karaoke_session::KaraokeState::Playing)
    }

    /// Stop admission and request cancellation before runtime teardown.
    /// Active imports that already claimed publication finish without accepting cancellation.
    pub async fn begin_shutdown(&self) {
        self.stopping.store(true, Ordering::Release);
        let mut jobs = self.jobs.write().await;
        for record in jobs.values_mut() {
            if !record.snapshot.status.terminal()
                && record
                    .import_control
                    .as_ref()
                    .is_none_or(|control| control.cancel())
            {
                record.cancelled = true;
            }
        }
    }

    fn ensure_running(&self) -> Result<(), ApiError> {
        if self.stopping.load(Ordering::Acquire) {
            return Err(ApiError::unavailable("Core is shutting down".to_owned()));
        }
        Ok(())
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
            job.deferred_reason = None;
            job.error = error;
            job.finished_at = Some(Utc::now());
        })
        .await;
    }
}

pub fn app(state: AppState) -> Router {
    Router::new()
        .route("/devices", get(devices))
        .route("/models", get(models))
        .route("/model-packages", get(model_packages))
        .route(
            "/model-selection",
            get(model_selection).post(set_model_selection),
        )
        .route("/analysis/health", get(analysis_health))
        .route("/jobs", post(create_job))
        .route("/jobs/{id}", get(get_job))
        .route("/jobs/{id}/cancel", post(cancel_job))
        .route("/jobs/{id}/events", get(job_events))
        .merge(song_api::routes())
        .merge(mixer_api::routes())
        .merge(karaoke_session_api::routes())
        .route_layer(middleware::from_fn(api_guard))
        .route("/health", get(health))
        .layer(DefaultBodyLimit::max(MAX_CORE_REQUEST_BYTES))
        .with_state(state)
}

/// In-process version/Origin guard. The executable additionally wraps every
/// route with its pipe-bootstrapped `CoreSession` authentication boundary.
async fn api_guard(request: Request, next: Next) -> Result<Response, ApiError> {
    if request.headers().contains_key(header::ORIGIN) {
        return Err(ApiError(
            StatusCode::FORBIDDEN,
            "Browser-origin requests are not permitted".to_owned(),
        ));
    }
    if let Some(version) = request.headers().get(CORE_API_VERSION_HEADER)
        && version.as_bytes() != b"1"
    {
        return Err(ApiError(
            StatusCode::UPGRADE_REQUIRED,
            "Unsupported Core API version; expected 1".to_owned(),
        ));
    }
    Ok(next.run(request).await)
}

async fn devices() -> Result<Json<Vec<audio_device::AudioDeviceInfo>>, ApiError> {
    audio_device::enumerate_devices()
        .map(Json)
        .map_err(ApiError::unavailable)
}

async fn health() -> Json<serde_json::Value> {
    Json(
        serde_json::json!({"status":"ok", "service":"open-karaoke-core", "api_version": CORE_API_VERSION}),
    )
}

async fn models(State(state): State<AppState>) -> Result<Json<serde_json::Value>, ApiError> {
    state
        .analysis
        .models()
        .await
        .map(Json)
        .map_err(ApiError::unavailable)
}

async fn model_packages(
    State(state): State<AppState>,
) -> Result<Json<serde_json::Value>, ApiError> {
    state
        .analysis
        .model_packages()
        .await
        .map(Json)
        .map_err(ApiError::unavailable)
}

async fn model_selection(
    State(state): State<AppState>,
) -> Result<Json<serde_json::Value>, ApiError> {
    state
        .analysis
        .model_selection()
        .await
        .map(Json)
        .map_err(ApiError::selection)
}

async fn set_model_selection(
    State(state): State<AppState>,
    Json(request): Json<ModelSelectionRequest>,
) -> Result<Json<serde_json::Value>, ApiError> {
    state
        .analysis
        .set_model_selection(&request.role, request.model_id.as_deref())
        .await
        .map(Json)
        .map_err(ApiError::selection)
}

/// Body of `POST /model-selection`; the Analysis Service validates role and id.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ModelSelectionRequest {
    role: String,
    model_id: Option<String>,
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
    if let Some(reference) = request.analysis.input_artifact.as_deref() {
        artifact_ref::validate_ref(reference)
            .map_err(|error| ApiError(StatusCode::BAD_REQUEST, error.to_string()))?;
    }
    state.ensure_running()?;
    let import_work = prepare_import(&state, &request).await?;
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
        imported_song: None,
        model_id: None,
        model_source: None,
        selected_model_id: None,
        selection_fallback_reason: None,
        model_plan: None,
        deferred_reason: None,
    };
    let (events, _) = broadcast::channel(64);
    let mut jobs = state.jobs.write().await;
    state.ensure_running()?;
    admit_job(&mut jobs)?;
    jobs.insert(
        id,
        JobRecord {
            snapshot: job.clone(),
            cancelled: false,
            import_control: import_work.as_ref().map(|work| Arc::clone(&work.control)),
            events,
        },
    );
    drop(jobs);
    tokio::spawn(run_job(state, id, request, import_work));
    Ok((StatusCode::ACCEPTED, Json(job)))
}

fn admit_job(jobs: &mut HashMap<Uuid, JobRecord>) -> Result<(), ApiError> {
    // Admission is atomic with insertion, so concurrent requests cannot
    // bypass the active-job budget.
    if jobs
        .values()
        .filter(|record| !record.snapshot.status.terminal())
        .count()
        >= MAX_ACTIVE_JOBS
    {
        return Err(ApiError(
            StatusCode::TOO_MANY_REQUESTS,
            "Too many active jobs".to_owned(),
        ));
    }
    if jobs.len() >= MAX_RETAINED_JOBS {
        // Keep active jobs and discard only the oldest completed/failed/cancelled
        // snapshot. Clients must treat an evicted job as expired (404).
        if let Some(oldest_id) = jobs
            .iter()
            .filter(|(_, record)| record.snapshot.status.terminal())
            .min_by(|(_, a), (_, b)| a.snapshot.created_at.cmp(&b.snapshot.created_at))
            .map(|(id, _)| *id)
        {
            jobs.remove(&oldest_id);
        } else {
            return Err(ApiError(
                StatusCode::TOO_MANY_REQUESTS,
                "Job history capacity reached".to_owned(),
            ));
        }
    }
    Ok(())
}

async fn prepare_import(
    state: &AppState,
    request: &CreateJobRequest,
) -> Result<Option<song_api::ImportWork>, ApiError> {
    if request.kind == "song.import" {
        let input = request.song_import.as_ref().ok_or_else(|| {
            ApiError(
                StatusCode::BAD_REQUEST,
                "song_import.analysis_job_id is required".to_owned(),
            )
        })?;
        return song_api::prepare(state, input).await.map(Some);
    }
    if request.song_import.is_some() {
        return Err(ApiError(
            StatusCode::BAD_REQUEST,
            "song_import requires kind song.import".to_owned(),
        ));
    }
    Ok(None)
}

async fn run_job(
    state: AppState,
    id: Uuid,
    request: CreateJobRequest,
    import: Option<song_api::ImportWork>,
) {
    if state.cancelled(id).await {
        state.finish(id, JobStatus::Cancelled, None).await;
        return;
    }
    if is_heavy_kind(&request.kind) && !wait_while_playing(&state, id).await {
        return;
    }
    // 同時数枠はJobの終了(この関数の戻り)まで保持する。
    let _heavy_slot = if is_heavy_kind(&request.kind) {
        match acquire_heavy_slot(&state, id).await {
            Ok(permit) => permit,
            Err(()) => return,
        }
    } else {
        None
    };
    state
        .update(id, |job| {
            job.status = JobStatus::Running;
            job.deferred_reason = None;
            "running".clone_into(&mut job.stage);
            job.started_at = Some(Utc::now());
        })
        .await;
    if let Some(work) = import {
        song_api::run(&state, id, work).await;
        return;
    }
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

/// {
///   責務: [`is_heavy_kind`: 再生中は開始を待たせる重い推論Jobかを判定する]
///   処理: [1: `analysis.`役割のうち mock 以外(stems/lyrics/alignment/pitch/events/notes/song)を重いとみなす]
///   引数: [kind: Job種別]
///   戻り値: [`bool`: 重い推論ならtrue。`song.import`や試験用Jobはfalse]
/// }
fn is_heavy_kind(kind: &str) -> bool {
    kind.strip_prefix("analysis.")
        .is_some_and(|role| role != "mock")
}

/// {
///   責務: [`wait_while_playing`: 再生中の間、重いJobをqueuedのまま開始待ちにする]
///   処理: [1: 方針が無効か再生中でなければ即座に開始可とする, 2: 待機中は`deferred_reason`とstage`deferred`を公開する, 3: 一定間隔で再生状態とcancelを確認する, 4: 上限時間を超えたらfailedにする]
///   引数: [state, id: Job ID]
///   戻り値: [`bool`: 開始してよければtrue。cancelled/failedで終端済みならfalse]
/// }
async fn wait_while_playing(state: &AppState, id: Uuid) -> bool {
    if !state.inference.defer_while_playing {
        return true;
    }
    let deadline = tokio::time::Instant::now() + state.inference.defer_max_wait;
    let mut announced = false;
    while state.playback_active() {
        if !announced {
            announced = true;
            state
                .update(id, |job| {
                    "deferred".clone_into(&mut job.stage);
                    job.deferred_reason = Some(DEFERRED_REASON_PLAYBACK_ACTIVE.to_owned());
                })
                .await;
        }
        if state.cancelled(id).await {
            state.finish(id, JobStatus::Cancelled, None).await;
            return false;
        }
        if tokio::time::Instant::now() >= deadline {
            state
                .finish(
                    id,
                    JobStatus::Failed,
                    Some(
                        "deferred_timeout: playback kept the job waiting past its limit".to_owned(),
                    ),
                )
                .await;
            return false;
        }
        tokio::time::sleep(DEFER_POLL_INTERVAL).await;
    }
    if state.cancelled(id).await {
        state.finish(id, JobStatus::Cancelled, None).await;
        return false;
    }
    true
}

/// {
///   責務: [`acquire_heavy_slot`: 重いJobの同時実行枠を取得するまでqueuedのまま待つ]
///   処理: [1: 枠設定が無ければ即開始, 2: 空きが無ければ`deferred_reason`とstage`deferred`を公開, 3: 一定間隔で枠・cancel・上限時間を確認, 4: 上限超過はfailed]
///   引数: [state, id: Job ID]
///   戻り値: [`Result<Option<OwnedSemaphorePermit>, ()>`: Okは開始可(無制限ならNone)。Errはcancelled/failedで終端済み]
///   副作用: [待機中のJob表を更新する]
/// }
async fn acquire_heavy_slot(
    state: &AppState,
    id: Uuid,
) -> Result<Option<OwnedSemaphorePermit>, ()> {
    let Some(slots) = state.heavy_slots.clone() else {
        return Ok(None);
    };
    let deadline = tokio::time::Instant::now() + state.inference.defer_max_wait;
    let mut announced = false;
    loop {
        if let Ok(permit) = Arc::clone(&slots).try_acquire_owned() {
            if state.cancelled(id).await {
                state.finish(id, JobStatus::Cancelled, None).await;
                return Err(());
            }
            return Ok(Some(permit));
        }
        if !announced {
            announced = true;
            state
                .update(id, |job| {
                    "deferred".clone_into(&mut job.stage);
                    job.deferred_reason = Some(DEFERRED_REASON_HEAVY_JOB_LIMIT.to_owned());
                })
                .await;
        }
        if state.cancelled(id).await {
            state.finish(id, JobStatus::Cancelled, None).await;
            return Err(());
        }
        if tokio::time::Instant::now() >= deadline {
            state
                .finish(
                    id,
                    JobStatus::Failed,
                    Some(
                        "deferred_timeout: heavy job limit kept the job waiting past its limit"
                            .to_owned(),
                    ),
                )
                .await;
            return Err(());
        }
        tokio::time::sleep(DEFER_POLL_INTERVAL).await;
    }
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
    // Provenance is untrusted too: refuse to publish an oversized or malformed one.
    remote
        .provenance
        .validate()
        .map_err(|reason| format!("analysis_protocol: {reason}"))?;
    let provenance = &remote.provenance;
    state
        .update(id, |job| {
            job.progress = remote.progress;
            job.stage.clone_from(&remote.stage);
            job.model_id.clone_from(&provenance.model_id);
            job.model_source = provenance.model_source;
            job.selected_model_id
                .clone_from(&provenance.selected_model_id);
            job.selection_fallback_reason
                .clone_from(&provenance.selection_fallback_reason);
            job.model_plan.clone_from(&provenance.model_plan);
        })
        .await;
    match remote.status.as_str() {
        "queued" | "running" => Ok(false),
        "completed" => {
            // A worker result is untrusted input to the public Job snapshot.
            artifact_ref::validate_job_artifacts(&remote.artifacts)
                .map_err(|error| format!("analysis_protocol: {error}"))?;
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
    if !record.snapshot.status.terminal()
        && record
            .import_control
            .as_ref()
            .is_none_or(|control| control.cancel())
    {
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
    let permit = Arc::clone(&state.sse_budget)
        .try_acquire_owned()
        .map_err(|_| {
            ApiError(
                StatusCode::TOO_MANY_REQUESTS,
                "Too many SSE connections".to_owned(),
            )
        })?;
    let stream = JobEventStream {
        initial: Some(JobEvent { job: snapshot }),
        updates: BroadcastStream::new(receiver),
        closed: false,
        _permit: permit,
    };
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
    /// Keep the service's client errors (`code: message`) as 4xx instead of 503.
    fn selection(message: String) -> Self {
        let status = if message.starts_with("model_not_selectable:") {
            StatusCode::CONFLICT
        } else if message.starts_with("invalid_request:") {
            StatusCode::UNPROCESSABLE_ENTITY
        } else {
            StatusCode::SERVICE_UNAVAILABLE
        };
        Self(status, message)
    }
}
impl IntoResponse for ApiError {
    fn into_response(self) -> axum::response::Response {
        (self.0, Json(serde_json::json!({"error": self.1}))).into_response()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    async fn running_job(state: &AppState) -> Uuid {
        let id = Uuid::new_v4();
        let (events, _) = broadcast::channel(4);
        let snapshot = Job {
            id,
            kind: "analysis.stems".to_owned(),
            status: JobStatus::Running,
            progress: 0.0,
            stage: "running".to_owned(),
            created_at: Utc::now(),
            started_at: Some(Utc::now()),
            finished_at: None,
            error: None,
            artifacts: Vec::new(),
            imported_song: None,
            model_id: None,
            model_source: None,
            selected_model_id: None,
            selection_fallback_reason: None,
            model_plan: None,
            deferred_reason: None,
        };
        state.jobs.write().await.insert(
            id,
            JobRecord {
                snapshot,
                cancelled: false,
                import_control: None,
                events,
            },
        );
        id
    }

    fn completed_remote(artifacts: Vec<String>) -> RemoteJob {
        RemoteJob {
            id: Uuid::new_v4(),
            instance_id: Uuid::new_v4(),
            status: "completed".to_owned(),
            progress: 1.0,
            stage: "completed".to_owned(),
            error: None,
            artifacts,
            provenance: model_plan::ModelProvenance::default(),
        }
    }

    #[tokio::test]
    async fn worker_artifacts_within_limits_are_published() {
        let state = AppState::new();
        let id = running_job(&state).await;
        let remote = completed_remote(vec!["jobs/x/stems/vocals.wav".to_owned()]);
        assert_eq!(apply_remote(&state, id, &remote).await, Ok(true));
        let job = state.snapshot(id).await.unwrap();
        assert_eq!(job.status, JobStatus::Completed);
        assert_eq!(job.artifacts, remote.artifacts);
    }

    #[tokio::test]
    async fn worker_model_provenance_is_published_and_bad_provenance_is_refused() {
        let state = AppState::new();
        let id = running_job(&state).await;
        let mut remote = completed_remote(Vec::new());
        remote.provenance = serde_json::from_value(serde_json::json!({
            "model_id": "mock-v1", "model_source": "default_fallback",
            "selected_model_id": "ghost", "selection_fallback_reason": "package missing",
            "model_plan": {"resolved_at": "t", "stems": {"source": "default"}},
        }))
        .unwrap();
        assert_eq!(apply_remote(&state, id, &remote).await, Ok(true));
        let job = state.snapshot(id).await.unwrap();
        assert_eq!(job.model_id.as_deref(), Some("mock-v1"));
        assert_eq!(job.selected_model_id.as_deref(), Some("ghost"));
        assert!(job.require_exact_model_plan().is_err());
        let value = serde_json::to_value(&job).unwrap();
        assert_eq!(value["model_source"], "default_fallback");
        assert_eq!(value["model_plan"]["stems"]["source"], "default");

        let state = AppState::new();
        let id = running_job(&state).await;
        let mut remote = completed_remote(Vec::new());
        remote.provenance.model_id = Some("x".repeat(model_plan::MAX_TEXT_CHARS + 1));
        let error = apply_remote(&state, id, &remote).await.unwrap_err();
        assert!(error.starts_with("analysis_protocol:"), "{error}");
        let job = state.snapshot(id).await.unwrap();
        assert_eq!((job.status, job.model_id), (JobStatus::Running, None));
    }

    #[tokio::test]
    async fn oversized_or_malformed_worker_artifacts_are_not_published() {
        for (artifacts, code) in [
            (
                vec!["jobs/x/a.json".to_owned(); artifact_ref::MAX_JOB_ARTIFACTS + 1],
                "too_many_artifacts",
            ),
            (vec!["../escape.wav".to_owned()], "invalid_artifact_ref"),
            (vec!["C:/abs.wav".to_owned()], "invalid_artifact_ref"),
            (vec!["a".repeat(1025)], "artifact_ref_too_long"),
        ] {
            let state = AppState::new();
            let id = running_job(&state).await;
            let error = apply_remote(&state, id, &completed_remote(artifacts))
                .await
                .unwrap_err();
            assert!(
                error.starts_with(&format!("analysis_protocol: {code}")),
                "{error}"
            );
            let job = state.snapshot(id).await.unwrap();
            // run_job turns the Err into a failed terminal snapshot.
            assert_eq!(job.status, JobStatus::Running);
            assert_eq!(job.artifacts, Vec::<String>::new());
        }
    }

    /// {
    ///   責務: [`queued_job`: 指定種別のqueued Jobを表に入れる(#43 待機試験用)]
    ///   引数: [state, kind]
    ///   戻り値: [Job ID]
    /// }
    async fn queued_job(state: &AppState, kind: &str) -> Uuid {
        let id = running_job(state).await;
        state
            .update(id, |job| {
                job.kind = kind.to_owned();
                job.status = JobStatus::Queued;
                "queued".clone_into(&mut job.stage);
                job.started_at = None;
            })
            .await;
        id
    }

    /// {
    ///   責務: [`start_playing`: Controller相当でセッションをplaying状態にする]
    ///   引数: [state, instance: 世代ID]
    ///   戻り値: [セッションID(`Uuid`)]
    /// }
    fn start_playing(state: &AppState, instance: Uuid) -> Uuid {
        let host = state.karaoke_host();
        let snapshot = host
            .create(
                Uuid::new_v4(),
                48_000 * 10,
                karaoke_session::ScoringMode::WholeSong,
            )
            .unwrap();
        assert!(host.finish_preparing(snapshot.session_id, Ok(())).unwrap());
        let ready = host.snapshot(snapshot.session_id).unwrap();
        host.execute(karaoke_session::CommandEnvelope {
            core_instance_id: instance,
            session_id: snapshot.session_id,
            command_id: Uuid::new_v4(),
            expected_revision: ready.state_revision,
            command: karaoke_session::KaraokeCommand::Play,
        })
        .unwrap();
        assert!(state.playback_active());
        snapshot.session_id
    }

    /// {
    ///   責務: [`stop_playing`: playing中のセッションをstopする]
    /// }
    fn stop_playing(state: &AppState, instance: Uuid, session: Uuid) {
        let host = state.karaoke_host();
        let current = host.snapshot(session).unwrap();
        host.execute(karaoke_session::CommandEnvelope {
            core_instance_id: instance,
            session_id: session,
            command_id: Uuid::new_v4(),
            expected_revision: current.state_revision,
            command: karaoke_session::KaraokeCommand::Stop,
        })
        .unwrap();
        assert!(!state.playback_active());
    }

    #[test]
    fn only_analysis_roles_other_than_mock_are_heavy() {
        for kind in [
            "analysis.stems",
            "analysis.lyrics",
            "analysis.alignment",
            "analysis.pitch",
            "analysis.events",
            "analysis.notes",
            "analysis.song",
        ] {
            assert!(is_heavy_kind(kind), "{kind}");
        }
        for kind in ["analysis.mock", "song.import", "test", ""] {
            assert!(!is_heavy_kind(kind), "{kind}");
        }
    }

    #[tokio::test]
    async fn heavy_job_waits_while_playing_and_starts_when_playback_ends() {
        let instance = Uuid::new_v4();
        let state = AppState::new().with_core_instance_id(instance);
        let session = start_playing(&state, instance);
        let id = queued_job(&state, "analysis.stems").await;
        let waiter = tokio::spawn({
            let state = state.clone();
            async move { wait_while_playing(&state, id).await }
        });
        tokio::time::sleep(Duration::from_millis(200)).await;
        assert!(!waiter.is_finished(), "must keep waiting during playback");
        let job = state.snapshot(id).await.unwrap();
        assert_eq!(job.status, JobStatus::Queued);
        assert_eq!(job.stage, "deferred");
        assert_eq!(
            job.deferred_reason.as_deref(),
            Some(DEFERRED_REASON_PLAYBACK_ACTIVE)
        );
        stop_playing(&state, instance, session);
        assert!(
            tokio::time::timeout(Duration::from_secs(5), waiter)
                .await
                .unwrap()
                .unwrap()
        );
    }

    #[tokio::test]
    async fn deferred_job_can_be_cancelled_and_does_not_touch_the_session() {
        let instance = Uuid::new_v4();
        let state = AppState::new().with_core_instance_id(instance);
        let session = start_playing(&state, instance);
        let before = state.karaoke_host().snapshot(session).unwrap();
        let id = queued_job(&state, "analysis.song").await;
        let waiter = tokio::spawn({
            let state = state.clone();
            async move { wait_while_playing(&state, id).await }
        });
        tokio::time::sleep(Duration::from_millis(120)).await;
        state.jobs.write().await.get_mut(&id).unwrap().cancelled = true;
        assert!(
            !tokio::time::timeout(Duration::from_secs(5), waiter)
                .await
                .unwrap()
                .unwrap()
        );
        let job = state.snapshot(id).await.unwrap();
        assert_eq!(job.status, JobStatus::Cancelled);
        assert_eq!(job.deferred_reason, None);
        assert_eq!(state.karaoke_host().snapshot(session).unwrap(), before);
    }

    /// {
    ///   責務: [`heavy_slot_unlimited_by_default_and_limit_one_serializes`: 既定は無制限で、上限1なら2件目の重いJobが枠解放まで待つことを検証する]
    ///   処理: [1: 既定で即開始, 2: 上限1で2件目がdeferred(heavy_job_limit), 3: 枠解放で開始]
    ///   引数: []
    ///   戻り値: []
    /// }
    #[tokio::test]
    async fn heavy_slot_unlimited_by_default_and_limit_one_serializes() {
        let state = AppState::new();
        let id = queued_job(&state, "analysis.song").await;
        assert!(matches!(acquire_heavy_slot(&state, id).await, Ok(None)));

        let state = AppState::new().with_max_concurrent_heavy_jobs(std::num::NonZeroUsize::new(1));
        let first = queued_job(&state, "analysis.song").await;
        let second = queued_job(&state, "analysis.lyrics").await;
        let held = acquire_heavy_slot(&state, first).await.unwrap();
        assert!(held.is_some());
        let waiter = tokio::spawn({
            let state = state.clone();
            async move {
                acquire_heavy_slot(&state, second)
                    .await
                    .map(|p| p.is_some())
            }
        });
        tokio::time::sleep(Duration::from_millis(200)).await;
        assert!(!waiter.is_finished(), "second heavy job must wait");
        let job = state.snapshot(second).await.unwrap();
        assert_eq!(job.status, JobStatus::Queued);
        assert_eq!(job.stage, "deferred");
        assert_eq!(
            job.deferred_reason.as_deref(),
            Some(DEFERRED_REASON_HEAVY_JOB_LIMIT)
        );
        drop(held);
        let got = tokio::time::timeout(Duration::from_secs(5), waiter)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(got, Ok(true));
    }

    /// {
    ///   責務: [`heavy_slot_wait_is_cancellable_and_bounded`: 枠待ちがcancelで終わり、上限時間でfailedになることを検証する]
    ///   処理: [1: 枠を占有, 2: 待機中にcancelしてcancelled, 3: 別Jobがdeferred_timeoutでfailed]
    ///   引数: []
    ///   戻り値: []
    /// }
    #[tokio::test]
    async fn heavy_slot_wait_is_cancellable_and_bounded() {
        let state = AppState::new()
            .with_max_concurrent_heavy_jobs(std::num::NonZeroUsize::new(1))
            .with_defer_max_wait(Duration::from_millis(200));
        let holder = queued_job(&state, "analysis.song").await;
        let _held = acquire_heavy_slot(&state, holder).await.unwrap();

        let cancelled = queued_job(&state, "analysis.song").await;
        let waiter = tokio::spawn({
            let state = state.clone();
            async move { acquire_heavy_slot(&state, cancelled).await.map(|_| ()) }
        });
        tokio::time::sleep(Duration::from_millis(80)).await;
        state
            .jobs
            .write()
            .await
            .get_mut(&cancelled)
            .unwrap()
            .cancelled = true;
        assert!(waiter.await.unwrap().is_err());
        assert_eq!(
            state.snapshot(cancelled).await.unwrap().status,
            JobStatus::Cancelled
        );

        let timed_out = queued_job(&state, "analysis.song").await;
        assert!(acquire_heavy_slot(&state, timed_out).await.is_err());
        let job = state.snapshot(timed_out).await.unwrap();
        assert_eq!(job.status, JobStatus::Failed);
        assert!(job.error.unwrap().contains("deferred_timeout"));
    }

    #[tokio::test]
    async fn deferral_is_bounded_and_fails_after_the_limit() {
        let instance = Uuid::new_v4();
        let state = AppState::new()
            .with_core_instance_id(instance)
            .with_defer_max_wait(Duration::from_millis(150));
        start_playing(&state, instance);
        let id = queued_job(&state, "analysis.lyrics").await;
        assert!(!wait_while_playing(&state, id).await);
        let job = state.snapshot(id).await.unwrap();
        assert_eq!(job.status, JobStatus::Failed);
        assert!(job.error.unwrap().contains("deferred_timeout"));
        assert!(
            state.playback_active(),
            "the job failure must not stop playback"
        );
    }

    #[tokio::test]
    async fn disabled_policy_and_idle_playback_do_not_wait() {
        let instance = Uuid::new_v4();
        let idle = AppState::new().with_core_instance_id(instance);
        let id = queued_job(&idle, "analysis.stems").await;
        assert!(wait_while_playing(&idle, id).await, "no session: start now");
        let off = AppState::new()
            .with_core_instance_id(instance)
            .with_defer_while_playing(false);
        start_playing(&off, instance);
        let id = queued_job(&off, "analysis.stems").await;
        assert!(wait_while_playing(&off, id).await, "policy off: start now");
        assert_eq!(off.snapshot(id).await.unwrap().deferred_reason, None);
    }
}
