//! Authenticated transport for the Core-owned karaoke session (Issue #107, slice 107.2).
//!
//! Device-free: this layer only exposes the pure [`crate::karaoke_session`] state machine over
//! HTTP (snapshot, play/pause/seek/stop commands and a bounded SSE subscription). Sessions are
//! created and fed (`finish_preparing`, presented timestamps, interrupts) by the future
//! controller through [`KaraokeSessionHost`]; clients can neither create a session nor report
//! playback progress. Authentication is the caller's layer, exactly as for the other routes.

#![allow(clippy::doc_markdown)] // JSON-style declaration comments name snake_case fields

use std::{
    collections::VecDeque,
    convert::Infallible,
    pin::Pin,
    sync::{Arc, Mutex, MutexGuard, PoisonError},
    task::{Context, Poll},
};

use axum::{
    Json, Router,
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode},
    response::{
        IntoResponse, Response,
        sse::{Event, KeepAlive, Sse},
    },
    routing::{get, post},
};
use serde::Deserialize;
use serde_json::json;
use tokio::sync::{OwnedSemaphorePermit, Semaphore, broadcast};
use tokio_stream::{Stream, wrappers::BroadcastStream};
use uuid::Uuid;

use crate::{
    AppState,
    audio_clock::{ClockEpoch, ClockSession},
    karaoke_session::{
        CommandEnvelope, CommandOutcome, EventOrigin, EventsAfter, KaraokeCommand, KaraokeError,
        KaraokeSession, KaraokeSessionRegistry, PresentedStreamSample, ScoringMode, SessionEvent,
        SessionSnapshot,
    },
};

/// Concurrent karaoke SSE subscriptions across all sessions.
pub const MAX_KARAOKE_SSE_CONNECTIONS: usize = 8;
/// Slots of the live broadcast; a subscriber slower than this is closed and must resync.
const LIVE_EVENT_CAPACITY: usize = crate::karaoke_session::EVENT_LOG_LIMIT;

/// {
///   責務: [`KaraokeSessionHost`: Core1世代の歌唱セッションregistryと状態イベント配信の唯一の入口]
///   フィールド: [
///     registry: 非終端1件のみ許すregistry(短いlockで直列化する。lock中にawaitしない),
///     live: 遷移ごとのSessionEventを購読者へ流すbroadcast,
///     sse_budget: 同時SSE購読の上限(`MAX_KARAOKE_SSE_CONNECTIONS`)
///   ]
///   補足: [Controllerはこの型経由でだけ状態を変える。直接registryを持たないのでeventの配信漏れが起きない]
/// }
#[derive(Clone)]
pub struct KaraokeSessionHost {
    registry: Arc<Mutex<KaraokeSessionRegistry>>,
    live: broadcast::Sender<SessionEvent>,
    sse_budget: Arc<Semaphore>,
}

impl KaraokeSessionHost {
    /// {
    ///   責務: [`new`: Core世代IDを持つ空のhostを作る]
    ///   処理: [1: 空のregistryを作る, 2: broadcastとSSE予算を初期化する]
    ///   引数: [core_instance_id: この世代のID(`GET /session`のinstance_idと同じ値を渡す)]
    ///   戻り値: [`Self`]
    /// }
    #[must_use]
    pub fn new(core_instance_id: Uuid) -> Self {
        Self {
            registry: Arc::new(Mutex::new(KaraokeSessionRegistry::new(core_instance_id))),
            live: broadcast::channel(LIVE_EVENT_CAPACITY).0,
            sse_budget: Arc::new(Semaphore::new(MAX_KARAOKE_SSE_CONNECTIONS)),
        }
    }

    /// {
    ///   責務: [`lock`: registryのlockを得る(内部)。panic後の毒状態でも状態機械は常に整合しているため続行する]
    ///   引数: []
    ///   戻り値: [`MutexGuard`]
    /// }
    fn lock(&self) -> MutexGuard<'_, KaraokeSessionRegistry> {
        self.registry.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// {
    ///   責務: [`publish_after`: 指定seqより後に記録されたイベントを購読者へ配る(内部)]
    ///   処理: [1: 取りこぼし無く連続して読めたものだけ送る, 2: 購読者が0でも失敗にしない]
    ///   引数: [session: 対象セッション, before: 操作前の最後のevent_seq]
    ///   戻り値: []
    /// }
    fn publish_after(&self, session: &KaraokeSession, before: u64) {
        if let EventsAfter::Events(events) = session.events_after(before) {
            for event in events {
                let _ = self.live.send(event);
            }
        }
    }

    /// {
    ///   責務: [`mutate`: 1セッションへの変更を実行し、生じたイベントを漏れなく配信する]
    ///   処理: [1: lockを得る, 2: 操作前のevent_seqを控える, 3: 操作を実行する, 4: 増えたイベントをlock中に配信して順序を保つ]
    ///   引数: [session_id, operation: registryへ行う操作]
    ///   戻り値: [`Result<T, KaraokeError>`: 操作の結果。失敗時は状態もイベントも変わらない]
    /// }
    fn mutate<T>(
        &self,
        session_id: Uuid,
        operation: impl FnOnce(&mut KaraokeSessionRegistry) -> Result<T, KaraokeError>,
    ) -> Result<T, KaraokeError> {
        let mut registry = self.lock();
        let before = registry
            .session_mut(session_id)
            .map(|session| session.last_event_seq())
            .ok();
        let result = operation(&mut registry);
        if let (Some(before), Ok(session)) = (before, registry.session_mut(session_id)) {
            self.publish_after(session, before);
        }
        result
    }

    /// {
    ///   責務: [`create`: 新しいセッションをpreparingで作る(Controller用。クライアントは作れない)]
    ///   処理: [1: registryへ作成を依頼する, 2: 最初のイベントを配信する]
    ///   引数: [song_id, duration_samples: 1以上, scoring_mode]
    ///   戻り値: [`Result<SessionSnapshot, KaraokeError>`]
    ///   エラー: [`SessionBusy` `InvalidDuration`]
    /// }
    ///
    /// # Errors
    /// Returns `SessionBusy` while another session is not terminal.
    pub fn create(
        &self,
        song_id: Uuid,
        duration_samples: u64,
        scoring_mode: ScoringMode,
    ) -> Result<SessionSnapshot, KaraokeError> {
        let mut registry = self.lock();
        let snapshot = registry.create(song_id, duration_samples, scoring_mode)?;
        if let Ok(session) = registry.session_mut(snapshot.session_id) {
            self.publish_after(session, 0);
        }
        Ok(snapshot)
    }

    /// {
    ///   責務: [`finish_preparing`: 準備結果でready/failedへ進める(Controller用)]
    ///   引数: [session_id, result: Ok=準備完了, Err(code)=失敗コード]
    ///   戻り値: [`Result<bool, KaraokeError>`: 遷移したか]
    ///   エラー: [`SessionNotFound`]
    /// }
    ///
    /// # Errors
    /// Returns `SessionNotFound` when the id is not the current session.
    pub fn finish_preparing(
        &self,
        session_id: Uuid,
        result: Result<(), &str>,
    ) -> Result<bool, KaraokeError> {
        self.mutate(session_id, |registry| {
            Ok(registry.session_mut(session_id)?.finish_preparing(result))
        })
    }

    /// {
    ///   責務: [`report_presented_from_clock`: clock変換済みの提示位置を品質付きで報告する(Controller用、107.4)]
    ///   引数: [origin, clock, clock_epoch, presented: 提示済みoutput stream sample]
    ///   戻り値: [`Result<SessionSnapshot, KaraokeError>`]
    /// }
    ///
    /// # Errors
    /// Propagates the session or clock rejection.
    pub fn report_presented_from_clock(
        &self,
        origin: EventOrigin,
        clock: &ClockSession,
        clock_epoch: ClockEpoch,
        presented: PresentedStreamSample,
    ) -> Result<SessionSnapshot, KaraokeError> {
        self.mutate(origin.session_id, |registry| {
            registry.report_presented_from_clock(origin, clock, clock_epoch, presented)
        })
    }

    /// {
    ///   責務: [`report_presented`: 提示済み位置を報告する(Controller用。クライアントAPIには無い)]
    ///   引数: [origin, presented_song_sample]
    ///   戻り値: [`Result<SessionSnapshot, KaraokeError>`]
    /// }
    ///
    /// # Errors
    /// Propagates the session rejection.
    pub fn report_presented(
        &self,
        origin: EventOrigin,
        presented_song_sample: u64,
    ) -> Result<SessionSnapshot, KaraokeError> {
        self.mutate(origin.session_id, |registry| {
            registry.report_presented(origin, presented_song_sample)
        })
    }

    /// {
    ///   責務: [`interrupt`: 機器抜去・stream errorを明示的な中断として報告する(Controller用)]
    ///   引数: [origin, code: 診断コード]
    ///   戻り値: [`Result<SessionSnapshot, KaraokeError>`]
    /// }
    ///
    /// # Errors
    /// Propagates the session rejection.
    pub fn interrupt(
        &self,
        origin: EventOrigin,
        code: &str,
    ) -> Result<SessionSnapshot, KaraokeError> {
        self.mutate(origin.session_id, |registry| {
            registry.interrupt(origin, code)
        })
    }

    /// {
    ///   責務: [`recover`: 中断(interrupted)したセッションを自動再生せずに復旧する(Controller用)]
    ///   処理: [1: 状態機械のrecoverを呼ぶ, 2: 生じたイベントを配信する]
    ///   引数: [session_id]
    ///   戻り値: [`Result<SessionSnapshot, KaraokeError>`: ready/pausedへ戻った状態]
    ///   エラー: [`SessionNotFound` `Terminal` `NotReady`(中断中でない)]
    /// }
    ///
    /// # Errors
    /// Returns `Terminal` or `NotReady` unless the session is interrupted.
    pub fn recover(&self, session_id: Uuid) -> Result<SessionSnapshot, KaraokeError> {
        self.mutate(session_id, |registry| {
            registry.session_mut(session_id)?.recover()
        })
    }

    /// {
    ///   責務: [`execute`: クライアントのコマンドを適用してイベントを配信する]
    ///   引数: [envelope]
    ///   戻り値: [`Result<CommandOutcome, KaraokeError>`]
    /// }
    ///
    /// # Errors
    /// Propagates the session rejection; state is unchanged on error.
    pub fn execute(&self, envelope: CommandEnvelope) -> Result<CommandOutcome, KaraokeError> {
        self.mutate(envelope.session_id, |registry| registry.execute(envelope))
    }

    /// {
    ///   責務: [`snapshot`: 指定セッションの現在状態を返す]
    ///   引数: [session_id]
    ///   戻り値: [`Result<SessionSnapshot, KaraokeError>`]
    ///   エラー: [`SessionNotFound`]
    /// }
    ///
    /// # Errors
    /// Returns `SessionNotFound` when the id is not the current session.
    pub fn snapshot(&self, session_id: Uuid) -> Result<SessionSnapshot, KaraokeError> {
        Ok(self.lock().session_mut(session_id)?.snapshot())
    }

    /// {
    ///   責務: [`current_snapshot`: 現行セッションがあればその状態を返す]
    ///   戻り値: [`Option<SessionSnapshot>`]
    /// }
    #[must_use]
    pub fn current_snapshot(&self) -> Option<SessionSnapshot> {
        self.lock().current_snapshot()
    }

    /// {
    ///   責務: [`subscribe`: 取りこぼしなく(購読開始と過去分の取得を同じlockで行い)購読を始める(内部)]
    ///   処理: [1: SSE予算を確保する(無ければ`None`), 2: lock中に購読を開始する, 3: `after`が有れば過去分またはgapを、無ければ現在のsnapshotを作る]
    ///   引数: [session_id, after: 受信済みの最後のevent_seq]
    ///   戻り値: [`Result<Option<KaraokeEventStream>, KaraokeError>`: 予算超過ならNone]
    /// }
    fn subscribe(
        &self,
        session_id: Uuid,
        after: Option<u64>,
    ) -> Result<Option<KaraokeEventStream>, KaraokeError> {
        let Ok(permit) = Arc::clone(&self.sse_budget).try_acquire_owned() else {
            return Ok(None);
        };
        let mut registry = self.lock();
        let session = registry.session_mut(session_id)?;
        let updates = BroadcastStream::new(self.live.subscribe());
        let snapshot = session.snapshot();
        let last_seq = session.last_event_seq();
        let mut backlog = VecDeque::new();
        let mut seen = last_seq;
        match after {
            None => backlog.push_back(StreamItem::Snapshot {
                event_seq: last_seq,
                snapshot: snapshot.clone(),
            }),
            Some(after) => match session.events_after(after) {
                EventsAfter::Events(events) => {
                    for event in events {
                        seen = seen.max(event.event_seq);
                        backlog.push_back(StreamItem::State(event));
                    }
                }
                EventsAfter::Gap { oldest_event_seq } => {
                    backlog.push_back(StreamItem::Gap {
                        oldest_event_seq,
                        event_seq: last_seq,
                        snapshot: snapshot.clone(),
                    });
                }
            },
        }
        let closed = backlog.is_empty() && snapshot.state.is_terminal();
        Ok(Some(KaraokeEventStream {
            session_id,
            backlog,
            updates,
            seen,
            closed,
            _permit: permit,
        }))
    }
}

/// {
///   責務: [`StreamItem`: SSEで送る1件。通常の状態イベント、現在状態の初回通知、取りこぼし通知]
///   フィールド: [
///     State: 遷移イベント(event名`state`),
///     Snapshot: `after`なしの接続で最初に送る現在状態(event名`snapshot`),
///     Gap: 要求seqが上限付きログから落ちた通知。snapshotで復旧する(event名`gap`)
///   ]
/// }
enum StreamItem {
    State(SessionEvent),
    Snapshot {
        event_seq: u64,
        snapshot: SessionSnapshot,
    },
    Gap {
        oldest_event_seq: u64,
        event_seq: u64,
        snapshot: SessionSnapshot,
    },
}

/// {
///   責務: [`KaraokeEventStream`: 1購読分のSSE。終端で閉じ、遅すぎる購読者も閉じて枠を返す]
///   フィールド: [
///     session_id: 対象セッション(他セッションのeventは捨てる),
///     backlog: 接続時に先に送る分,
///     updates: 以降のlive event,
///     seen: 送信済みの最大event_seq(重複を送らない),
///     closed: 閉じた状態,
///     _permit: 保持中だけSSE枠を占有する
///   ]
/// }
struct KaraokeEventStream {
    session_id: Uuid,
    backlog: VecDeque<StreamItem>,
    updates: BroadcastStream<SessionEvent>,
    seen: u64,
    closed: bool,
    _permit: OwnedSemaphorePermit,
}

impl Stream for KaraokeEventStream {
    type Item = Result<Event, Infallible>;

    /// {
    ///   責務: [`poll_next`: 次のSSE eventを返す]
    ///   処理: [
    ///     1: 閉じていれば終了する,
    ///     2: backlogを先に送る,
    ///     3: 次にlive eventを取り、別セッション・送信済みseqは捨てる,
    ///     4: 取りこぼし(lagged)または送信元終了なら閉じる(クライアントは再接続してsnapshotで復旧),
    ///     5: 終端状態のeventを送ったら閉じる
    ///   ]
    ///   引数: [cx]
    ///   戻り値: [SSE event、待機、または終了]
    /// }
    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let stream = self.get_mut();
        loop {
            if stream.closed {
                return Poll::Ready(None);
            }
            let item = if let Some(item) = stream.backlog.pop_front() {
                item
            } else {
                match Pin::new(&mut stream.updates).poll_next(cx) {
                    Poll::Ready(Some(Ok(event))) => {
                        if event.session_id != stream.session_id || event.event_seq <= stream.seen {
                            continue;
                        }
                        stream.seen = event.event_seq;
                        StreamItem::State(event)
                    }
                    Poll::Ready(Some(Err(_)) | None) => {
                        stream.closed = true;
                        return Poll::Ready(None);
                    }
                    Poll::Pending => return Poll::Pending,
                }
            };
            let (name, seq, payload, terminal) = match item {
                StreamItem::State(event) => (
                    "state",
                    event.event_seq,
                    serde_json::to_string(&event),
                    event.snapshot.state.is_terminal(),
                ),
                StreamItem::Snapshot {
                    event_seq,
                    snapshot,
                } => (
                    "snapshot",
                    event_seq,
                    serde_json::to_string(&json!({"event_seq": event_seq, "snapshot": snapshot})),
                    snapshot.state.is_terminal(),
                ),
                StreamItem::Gap {
                    oldest_event_seq,
                    event_seq,
                    snapshot,
                } => (
                    "gap",
                    event_seq,
                    serde_json::to_string(&json!({
                        "oldest_event_seq": oldest_event_seq,
                        "event_seq": event_seq,
                        "snapshot": snapshot,
                    })),
                    snapshot.state.is_terminal(),
                ),
            };
            stream.closed = terminal;
            return Poll::Ready(Some(Ok(Event::default()
                .event(name)
                .id(seq.to_string())
                .data(payload.unwrap_or_default()))));
        }
    }
}

/// {
///   責務: [`CommandKind`: HTTPで受け取る4種のコマンド]
///   フィールド: [`type`が play/pause/seek/stop。seekは`song_sample`(48kHz整数sample)必須]
/// }
#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
enum CommandKind {
    Play,
    Pause,
    Seek { song_sample: u64 },
    Stop,
}

/// {
///   責務: [`CommandBody`: `POST /karaoke-sessions/{id}/commands`の本文]
///   フィールド: [
///     core_instance_id: 宛先Core世代(`GET /session`のinstance_id),
///     command_id: 再送キー(同IDの再送は前回結果を返す),
///     expected_revision: 期待するstate_revision(Stopは省略可で、古くても受理),
///     command: 実行内容
///   ]
/// }
#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CommandBody {
    core_instance_id: Uuid,
    command_id: Uuid,
    #[serde(default)]
    expected_revision: u64,
    command: CommandKind,
}

/// {
///   責務: [`EventsQuery`: SSE再接続位置。`Last-Event-ID`ヘッダーでも指定できる]
///   フィールド: [after: 受信済みの最後のevent_seq]
/// }
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct EventsQuery {
    after: Option<u64>,
}

/// {
///   責務: [`routes`: 歌唱セッションtransportのRouterを返す]
///   戻り値: [`AppState`を使うRouter。認証・Origin/版検査は呼び出し側の層が担う]
///   補足: [`/karaoke-sessions/current`は静的segmentとして`{id}`より優先される]
/// }
pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/karaoke-sessions/current", get(current))
        .route("/karaoke-sessions/{id}", get(read))
        .route("/karaoke-sessions/{id}/commands", post(command))
        .route("/karaoke-sessions/{id}/events", get(events))
}

/// {
///   責務: [`KaraokeApiError`: 失敗を`{"error","code"}`のJSONで返す]
///   フィールド: [status, code: 安定コード, message]
/// }
struct KaraokeApiError {
    status: StatusCode,
    code: &'static str,
    message: String,
}

impl KaraokeApiError {
    /// {
    ///   責務: [`from_session`: 状態機械の拒否をHTTP状態へ対応づける]
    ///   処理: [1: 未知session=404, 2: 範囲・値の不正=422, 3: それ以外の状態競合=409]
    ///   引数: [error]
    ///   戻り値: [`Self`]
    /// }
    fn from_session(error: &KaraokeError) -> Self {
        let status = match error {
            KaraokeError::SessionNotFound => StatusCode::NOT_FOUND,
            KaraokeError::OutOfRange
            | KaraokeError::InvalidRange
            | KaraokeError::InvalidDuration => StatusCode::UNPROCESSABLE_ENTITY,
            _ => StatusCode::CONFLICT,
        };
        let message = match error {
            KaraokeError::StaleRevision { current, .. } => {
                format!("stale_revision: current state_revision is {current}")
            }
            other => other.code().to_owned(),
        };
        Self {
            status,
            code: error.code(),
            message,
        }
    }
}

impl IntoResponse for KaraokeApiError {
    fn into_response(self) -> Response {
        (
            self.status,
            Json(json!({"error": self.message, "code": self.code})),
        )
            .into_response()
    }
}

/// {
///   責務: [`ensure_running`: Core停止中なら503にする]
///   引数: [state]
///   戻り値: [稼働中ならOk]
/// }
fn ensure_running(state: &AppState) -> Result<(), KaraokeApiError> {
    state.ensure_running().map_err(|error| KaraokeApiError {
        status: error.0,
        code: "shutting_down",
        message: error.1,
    })
}

/// {
///   責務: [`read`: 指定セッションの現在状態を返す]
///   引数: [state, id: session_id]
///   戻り値: [snapshot。未知のidは404]
/// }
async fn read(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> Result<Json<SessionSnapshot>, KaraokeApiError> {
    state
        .karaoke_host()
        .snapshot(id)
        .map(Json)
        .map_err(|error| KaraokeApiError::from_session(&error))
}

/// {
///   責務: [`current`: 現行セッションの状態を返す。クライアントがsession_idを知る入口]
///   引数: [state]
///   戻り値: [snapshot。セッションが無ければ404]
/// }
async fn current(State(state): State<AppState>) -> Result<Json<SessionSnapshot>, KaraokeApiError> {
    state
        .karaoke_host()
        .current_snapshot()
        .map(Json)
        .ok_or_else(|| KaraokeApiError::from_session(&KaraokeError::SessionNotFound))
}

/// {
///   責務: [`command`: play/pause/seek/stopを適用する]
///   処理: [1: 停止中なら503, 2: 本文を封筒へ変換する, 3: 状態機械へ渡す(冪等・revision・epochの規則はそちら), 4: dispositionとsnapshotを返す]
///   引数: [state, id: session_id, body]
///   戻り値: [`{"disposition","snapshot"}`。拒否は409/422/404でstateを変えない]
/// }
async fn command(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Json(body): Json<CommandBody>,
) -> Result<Json<serde_json::Value>, KaraokeApiError> {
    ensure_running(&state)?;
    let command = match body.command {
        CommandKind::Play => KaraokeCommand::Play,
        CommandKind::Pause => KaraokeCommand::Pause,
        CommandKind::Seek { song_sample } => KaraokeCommand::Seek { song_sample },
        CommandKind::Stop => KaraokeCommand::Stop,
    };
    let outcome = state
        .karaoke_host()
        .execute(CommandEnvelope {
            core_instance_id: body.core_instance_id,
            session_id: id,
            command_id: body.command_id,
            expected_revision: body.expected_revision,
            command,
        })
        .map_err(|error| KaraokeApiError::from_session(&error))?;
    Ok(Json(
        json!({"disposition": outcome.disposition, "snapshot": outcome.snapshot}),
    ))
}

/// {
///   責務: [`events`: 状態イベントの上限付きSSE購読]
///   処理: [
///     1: 停止中なら503,
///     2: `after`(query)または`Last-Event-ID`を読む(不正値は400),
///     3: 購読を開始する(同時8本まで、超過は429),
///     4: `after`なしは現在のsnapshot、ありは続きのeventまたはgapを送り、以降をliveで流す
///   ]
///   引数: [state, id, query, headers]
///   戻り値: [SSE。未知のidは404]
/// }
async fn events(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Query(query): Query<EventsQuery>,
    headers: HeaderMap,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, KaraokeApiError> {
    ensure_running(&state)?;
    let after = match (query.after, headers.get("last-event-id")) {
        (Some(after), _) => Some(after),
        (None, Some(value)) => Some(
            value
                .to_str()
                .ok()
                .and_then(|text| text.parse::<u64>().ok())
                .ok_or_else(|| KaraokeApiError {
                    status: StatusCode::BAD_REQUEST,
                    code: "invalid_last_event_id",
                    message: "invalid_request: Last-Event-ID must be an event_seq".to_owned(),
                })?,
        ),
        (None, None) => None,
    };
    let stream = state
        .karaoke_host()
        .subscribe(id, after)
        .map_err(|error| KaraokeApiError::from_session(&error))?
        .ok_or_else(|| KaraokeApiError {
            status: StatusCode::TOO_MANY_REQUESTS,
            code: "too_many_subscriptions",
            message: "Too many karaoke SSE connections".to_owned(),
        })?;
    Ok(Sse::new(stream).keep_alive(KeepAlive::default()))
}
