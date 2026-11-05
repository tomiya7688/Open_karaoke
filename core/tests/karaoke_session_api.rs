//! Device-free HTTP tests for Issue #107 slice 107.2 (authenticated transport).
//! Each test names the acceptance item (T#) of docs/design/issue-107-karaoke-session.md.
//! The router is the in-process `app()`; the bearer boundary is `CoreSession` in the executable
//! and is covered by the session tests, so these tests check the routes behind it.

#![allow(clippy::doc_markdown)] // JSON-style declaration comments name snake_case fields

use std::time::Duration;

use axum::{
    Router,
    body::{Body, to_bytes},
    http::{Request, StatusCode},
};
use open_karaoke_core::{
    AppState, app,
    karaoke_session::{EventOrigin, ScoringMode},
    karaoke_session_api::{KaraokeSessionHost, MAX_KARAOKE_SSE_CONNECTIONS},
};
use serde_json::{Value, json};
use tokio::time::timeout;
use tokio_stream::StreamExt;
use tower::ServiceExt;
use uuid::Uuid;

const DURATION: u64 = 48_000 * 10;

/// {
///   責務: [`Fixture`: テスト用のrouter・host・世代ID・準備完了済みセッションをまとめる]
///   フィールド: [router, host, instance: 世代ID, session: session_id]
/// }
struct Fixture {
    router: Router,
    host: KaraokeSessionHost,
    instance: Uuid,
    session: Uuid,
}

/// {
///   責務: [`fixture`: ready状態のセッションを持つ環境を作る]
///   処理: [1: 世代IDを指定したAppStateを作る, 2: Controller相当としてセッションを作りreadyにする]
///   戻り値: [`Fixture`]
/// }
fn fixture() -> Fixture {
    let instance = Uuid::new_v4();
    let state = AppState::new().with_core_instance_id(instance);
    let host = state.karaoke_host();
    let snapshot = host
        .create(Uuid::new_v4(), DURATION, ScoringMode::WholeSong)
        .unwrap();
    assert!(host.finish_preparing(snapshot.session_id, Ok(())).unwrap());
    Fixture {
        router: app(state),
        host,
        instance,
        session: snapshot.session_id,
    }
}

/// {
///   責務: [`request`: JSON要求を送り、状態コードとJSON本文を返す]
///   引数: [router, method, path, body]
///   戻り値: [(状態コード, JSON。JSON以外はNull)]
/// }
async fn request(router: &Router, method: &str, path: &str, body: Value) -> (StatusCode, Value) {
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
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

/// {
///   責務: [`send`: commandを送る]
///   引数: [f, command: `{"type":...}`, command_id, expected_revision]
///   戻り値: [(状態コード, JSON)]
/// }
async fn send(
    f: &Fixture,
    command: Value,
    command_id: Uuid,
    expected_revision: u64,
) -> (StatusCode, Value) {
    request(
        &f.router,
        "POST",
        &format!("/karaoke-sessions/{}/commands", f.session),
        json!({
            "core_instance_id": f.instance,
            "command_id": command_id,
            "expected_revision": expected_revision,
            "command": command,
        }),
    )
    .await
}

/// {
///   責務: [`revision`: 現在のstate_revisionをGETで読む]
/// }
async fn revision(f: &Fixture) -> u64 {
    let (status, snapshot) = request(
        &f.router,
        "GET",
        &format!("/karaoke-sessions/{}", f.session),
        Value::Null,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    snapshot["state_revision"].as_u64().unwrap()
}

/// {
///   責務: [`open_events`: SSEを開く]
///   引数: [router, id, query: `?after=N`など]
///   戻り値: [HTTP応答。本文を保持する間は購読枠を使う]
/// }
async fn open_events(router: &Router, id: Uuid, query: &str) -> axum::response::Response {
    router
        .clone()
        .oneshot(
            Request::builder()
                .method("GET")
                .uri(format!("/karaoke-sessions/{id}/events{query}"))
                .header("x-open-karaoke-api-version", "1")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap()
}

/// {
///   責務: [`read_events`: SSE本文から`count`件のeventを(名前, JSON)で読む]
///   処理: [1: 2秒を上限にchunkを蓄積する, 2: 空行区切りでevent名とdataを取り出す]
/// }
async fn read_events<S>(stream: &mut S, count: usize) -> Vec<(String, Value)>
where
    S: tokio_stream::Stream<Item = Result<axum::body::Bytes, axum::Error>> + Unpin,
{
    let mut text = String::new();
    loop {
        let events = parse_events(&text);
        if events.len() >= count {
            return events;
        }
        let chunk = timeout(Duration::from_secs(2), stream.next())
            .await
            .expect("sse chunk timed out")
            .expect("sse ended early")
            .unwrap();
        text.push_str(&String::from_utf8_lossy(&chunk));
    }
}

/// {
///   責務: [`parse_events`: SSE文字列から完結したeventを取り出す]
/// }
fn parse_events(text: &str) -> Vec<(String, Value)> {
    text.split("\n\n")
        .filter(|block| block.contains("event:"))
        .map(|block| {
            let mut name = String::new();
            let mut data = String::new();
            for line in block.lines() {
                if let Some(value) = line.strip_prefix("event: ") {
                    value.clone_into(&mut name);
                } else if let Some(value) = line.strip_prefix("data: ") {
                    data.push_str(value);
                }
            }
            (name, serde_json::from_str(&data).unwrap())
        })
        .collect()
}

/// T1: 状態取得。未作成は404、作成後は現行セッションを`current`とidで取れる。
#[tokio::test]
async fn t1_snapshot_and_current_are_readable() {
    let state = AppState::new();
    let host = state.karaoke_host();
    let router = app(state);
    let (status, body) = request(&router, "GET", "/karaoke-sessions/current", Value::Null).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body["code"], "session_not_found");

    let created = host
        .create(Uuid::new_v4(), DURATION, ScoringMode::Practice)
        .unwrap();
    let (status, current) = request(&router, "GET", "/karaoke-sessions/current", Value::Null).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(current["state"], "preparing");
    assert_eq!(current["session_id"], created.session_id.to_string());
    let (status, by_id) = request(
        &router,
        "GET",
        &format!("/karaoke-sessions/{}", created.session_id),
        Value::Null,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(by_id, current);
    let (status, _) = request(
        &router,
        "GET",
        &format!("/karaoke-sessions/{}", Uuid::new_v4()),
        Value::Null,
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    // preparing中のplayは409 not_ready
    let (status, body) = request(
        &router,
        "POST",
        &format!("/karaoke-sessions/{}/commands", created.session_id),
        json!({
            "core_instance_id": host_instance(&current),
            "command_id": Uuid::new_v4(),
            "expected_revision": 0,
            "command": {"type": "play"},
        }),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["code"], "not_ready");
}

/// {
///   責務: [`host_instance`: snapshotからcore_instance_idを取り出す]
/// }
fn host_instance(snapshot: &Value) -> Value {
    snapshot["core_instance_id"].clone()
}

/// T2: play/pause/seek/stopが状態機械に届き、dispositionとsnapshotを返す。
#[tokio::test]
async fn t2_play_pause_seek_stop_reach_the_state_machine() {
    let f = fixture();
    let (status, body) = send(
        &f,
        json!({"type": "play"}),
        Uuid::new_v4(),
        revision(&f).await,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["disposition"], "applied");
    assert_eq!(body["snapshot"]["state"], "playing");

    let (_, body) = send(
        &f,
        json!({"type": "pause"}),
        Uuid::new_v4(),
        revision(&f).await,
    )
    .await;
    assert_eq!(body["snapshot"]["state"], "paused");

    let (status, body) = send(
        &f,
        json!({"type": "seek", "song_sample": 96_000}),
        Uuid::new_v4(),
        revision(&f).await,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["snapshot"]["position_song_sample"], 96_000);
    assert_eq!(body["snapshot"]["position_source"], "committed");

    // 同じ状態へのpauseはno_opでrevisionを進めない
    let before = revision(&f).await;
    let (_, body) = send(&f, json!({"type": "pause"}), Uuid::new_v4(), before).await;
    assert_eq!(body["disposition"], "no_op");
    assert_eq!(revision(&f).await, before);

    let (_, body) = send(&f, json!({"type": "stop"}), Uuid::new_v4(), 0).await;
    assert_eq!(body["snapshot"]["state"], "stopped");
    // 終端後のplayは409 terminal
    let (status, body) = send(
        &f,
        json!({"type": "play"}),
        Uuid::new_v4(),
        revision(&f).await,
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["code"], "terminal");
}

/// T3: 冪等性と競合。再送はreplayed、別内容の同IDはconflict、古いrevisionはstale、Stopは免除。
#[tokio::test]
async fn t3_idempotency_and_revision_conflicts_are_rejected() {
    let f = fixture();
    let id = Uuid::new_v4();
    let rev = revision(&f).await;
    let (_, first) = send(&f, json!({"type": "play"}), id, rev).await;
    let (status, again) = send(&f, json!({"type": "play"}), id, rev).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(again["disposition"], "replayed");
    assert_eq!(again["snapshot"], first["snapshot"]);

    let (status, body) = send(&f, json!({"type": "pause"}), id, rev).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["code"], "command_id_conflict");

    let (status, body) = send(&f, json!({"type": "pause"}), Uuid::new_v4(), rev).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["code"], "stale_revision");
    assert!(body["error"].as_str().unwrap().contains("stale_revision"));
    assert_eq!(
        revision(&f).await,
        rev + 1,
        "rejection must not change state"
    );

    // 古いrevisionのStopは受理される
    let (status, body) = send(&f, json!({"type": "stop"}), Uuid::new_v4(), rev).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["snapshot"]["state"], "stopped");
}

/// T4: 範囲外seek(422)・旧世代(409 stale_instance)・未知セッション(404)・不正本文を拒否する。
#[tokio::test]
async fn t4_invalid_requests_are_rejected_without_changing_state() {
    let f = fixture();
    let rev = revision(&f).await;
    let (status, body) = send(
        &f,
        json!({"type": "seek", "song_sample": DURATION}),
        Uuid::new_v4(),
        rev,
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["code"], "out_of_range");

    let (status, body) = request(
        &f.router,
        "POST",
        &format!("/karaoke-sessions/{}/commands", f.session),
        json!({
            "core_instance_id": Uuid::new_v4(),
            "command_id": Uuid::new_v4(),
            "expected_revision": rev,
            "command": {"type": "play"},
        }),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["code"], "stale_instance");

    let (status, body) = request(
        &f.router,
        "POST",
        &format!("/karaoke-sessions/{}/commands", Uuid::new_v4()),
        json!({
            "core_instance_id": f.instance,
            "command_id": Uuid::new_v4(),
            "expected_revision": rev,
            "command": {"type": "play"},
        }),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body["code"], "session_not_found");

    for bad in [
        json!({"core_instance_id": f.instance, "command_id": Uuid::new_v4(), "command": {"type": "rewind"}}),
        json!({"core_instance_id": f.instance, "command_id": Uuid::new_v4(), "command": {"type": "seek"}}),
        json!({"core_instance_id": f.instance, "command_id": Uuid::new_v4(), "command": {"type": "play"}, "position": 5}),
    ] {
        let (status, _) = request(
            &f.router,
            "POST",
            &format!("/karaoke-sessions/{}/commands", f.session),
            bad,
        )
        .await;
        assert!(status.is_client_error(), "{status}");
    }
    assert_eq!(revision(&f).await, rev);
}

/// T5: 位置は提示済みtimestampだけ。clientにはpresented報告のrouteが無く、host経由のみ反映される。
#[tokio::test]
async fn t5_position_comes_only_from_the_presented_report() {
    let f = fixture();
    let (_, body) = send(
        &f,
        json!({"type": "play"}),
        Uuid::new_v4(),
        revision(&f).await,
    )
    .await;
    let epoch = body["snapshot"]["transport_epoch"].as_u64().unwrap();
    let (status, _) = request(
        &f.router,
        "POST",
        &format!("/karaoke-sessions/{}/presented", f.session),
        json!({"song_sample": 48_000}),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::NOT_FOUND,
        "no client route may set position"
    );

    let origin = EventOrigin {
        core_instance_id: f.instance,
        session_id: f.session,
        epoch,
    };
    f.host.report_presented(origin, 48_000).unwrap();
    let (_, snapshot) = request(
        &f.router,
        "GET",
        &format!("/karaoke-sessions/{}", f.session),
        Value::Null,
    )
    .await;
    assert_eq!(snapshot["position_song_sample"], 48_000);
    assert_eq!(snapshot["position_source"], "presented");
}

/// T6: SSE。接続直後にsnapshot、以降の遷移をstateイベントで受け取り、終端で閉じる。
#[tokio::test]
async fn t6_sse_streams_snapshot_then_transitions_and_closes_on_terminal() {
    let f = fixture();
    let response = open_events(&f.router, f.session, "").await;
    assert_eq!(response.status(), StatusCode::OK);
    let mut stream = response.into_body().into_data_stream();
    let first = read_events(&mut stream, 1).await;
    assert_eq!(first[0].0, "snapshot");
    assert_eq!(first[0].1["snapshot"]["state"], "ready");

    send(
        &f,
        json!({"type": "play"}),
        Uuid::new_v4(),
        revision(&f).await,
    )
    .await;
    send(&f, json!({"type": "stop"}), Uuid::new_v4(), 0).await;
    let events = read_events(&mut stream, 2).await;
    assert_eq!(events[0].0, "state");
    assert_eq!(events[0].1["snapshot"]["state"], "playing");
    assert_eq!(events[1].1["snapshot"]["state"], "stopped");
    let seqs: Vec<u64> = events
        .iter()
        .map(|e| e.1["event_seq"].as_u64().unwrap())
        .collect();
    assert_eq!(seqs[1], seqs[0] + 1);
    assert!(
        timeout(Duration::from_secs(2), stream.next())
            .await
            .unwrap()
            .is_none(),
        "stream must end after the terminal event"
    );
}

/// T7: 再接続。`after`/`Last-Event-ID`は続きから送り、ログから落ちた位置はgapとsnapshotで復旧させる。
#[tokio::test]
async fn t7_reconnect_resumes_or_reports_a_gap() {
    let f = fixture();
    // preparing(1) ready(2) playing(3)
    send(
        &f,
        json!({"type": "play"}),
        Uuid::new_v4(),
        revision(&f).await,
    )
    .await;
    let response = open_events(&f.router, f.session, "?after=2").await;
    let mut stream = response.into_body().into_data_stream();
    let replay = read_events(&mut stream, 1).await;
    assert_eq!(replay[0].0, "state");
    assert_eq!(replay[0].1["event_seq"], 3);
    drop(stream);

    let header = f
        .router
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!("/karaoke-sessions/{}/events", f.session))
                .header("last-event-id", "2")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let mut stream = header.into_body().into_data_stream();
    assert_eq!(read_events(&mut stream, 1).await[0].1["event_seq"], 3);
    drop(stream);

    let bad = f
        .router
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!("/karaoke-sessions/{}/events", f.session))
                .header("last-event-id", "not-a-number")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(bad.status(), StatusCode::BAD_REQUEST);

    // ログ上限(64)を超えて遷移させ、先頭付近の位置から再接続するとgap
    for _ in 0..40 {
        send(
            &f,
            json!({"type": "pause"}),
            Uuid::new_v4(),
            revision(&f).await,
        )
        .await;
        send(
            &f,
            json!({"type": "play"}),
            Uuid::new_v4(),
            revision(&f).await,
        )
        .await;
    }
    let response = open_events(&f.router, f.session, "?after=1").await;
    let mut stream = response.into_body().into_data_stream();
    let gap = read_events(&mut stream, 1).await;
    assert_eq!(gap[0].0, "gap");
    assert!(gap[0].1["oldest_event_seq"].as_u64().unwrap() > 2);
    assert_eq!(gap[0].1["snapshot"]["state"], "playing");
    assert_eq!(
        gap[0].1["event_seq"],
        gap[0].1["snapshot"]["state_revision"].as_u64().unwrap() + 1
    );
    drop(stream);

    // 最新位置まで受信済みなら過去分は空で、以降のliveだけが届く
    let latest = gap_latest(&f).await;
    let response = open_events(&f.router, f.session, &format!("?after={latest}")).await;
    let mut stream = response.into_body().into_data_stream();
    send(
        &f,
        json!({"type": "pause"}),
        Uuid::new_v4(),
        revision(&f).await,
    )
    .await;
    let live = read_events(&mut stream, 1).await;
    assert_eq!(live[0].1["event_seq"].as_u64().unwrap(), latest + 1);
}

/// {
///   責務: [`gap_latest`: 現在の最新event_seqを`snapshot`イベントから得る]
/// }
async fn gap_latest(f: &Fixture) -> u64 {
    let response = open_events(&f.router, f.session, "").await;
    let mut stream = response.into_body().into_data_stream();
    read_events(&mut stream, 1).await[0].1["event_seq"]
        .as_u64()
        .unwrap()
}

/// T8: SSE購読は上限付き。超過は429、切断で枠が返る。未知のセッションは404。
#[tokio::test]
async fn t8_sse_subscriptions_are_bounded_and_recover() {
    let f = fixture();
    let mut open = Vec::new();
    for _ in 0..MAX_KARAOKE_SSE_CONNECTIONS {
        let response = open_events(&f.router, f.session, "").await;
        assert_eq!(response.status(), StatusCode::OK);
        open.push(response);
    }
    let rejected = open_events(&f.router, f.session, "").await;
    assert_eq!(rejected.status(), StatusCode::TOO_MANY_REQUESTS);
    let body = to_bytes(rejected.into_body(), usize::MAX).await.unwrap();
    let error: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(error["code"], "too_many_subscriptions");

    drop(open.pop());
    let reconnected = open_events(&f.router, f.session, "").await;
    assert_eq!(reconnected.status(), StatusCode::OK);
    open.push(reconnected);
    assert_eq!(
        open_events(&f.router, f.session, "").await.status(),
        StatusCode::TOO_MANY_REQUESTS
    );
    drop(open);
    assert_eq!(
        open_events(&f.router, Uuid::new_v4(), "").await.status(),
        StatusCode::NOT_FOUND
    );
}

/// T9: 遅い購読者は黙って落とさず、ストリームを閉じて再接続(snapshot)で復旧させる。
#[tokio::test]
async fn t9_slow_subscriber_is_closed_and_can_resync() {
    let f = fixture();
    let response = open_events(&f.router, f.session, "").await;
    let mut slow = response.into_body().into_data_stream();
    // 読まないまま容量(64)を超える遷移を起こす
    for _ in 0..50 {
        send(
            &f,
            json!({"type": "play"}),
            Uuid::new_v4(),
            revision(&f).await,
        )
        .await;
        send(
            &f,
            json!({"type": "pause"}),
            Uuid::new_v4(),
            revision(&f).await,
        )
        .await;
    }
    let mut seen = 0_usize;
    loop {
        match timeout(Duration::from_secs(2), slow.next()).await {
            Ok(Some(chunk)) => {
                chunk.unwrap();
                seen += 1;
                assert!(seen < 1000, "stream must end");
            }
            Ok(None) => break,
            Err(elapsed) => panic!("lagged subscriber must be closed: {elapsed}"),
        }
    }
    let resync = open_events(&f.router, f.session, "").await;
    let mut stream = resync.into_body().into_data_stream();
    let first = read_events(&mut stream, 1).await;
    assert_eq!(first[0].0, "snapshot");
    assert_eq!(first[0].1["snapshot"]["state"], "paused");
}

/// T10: 旧世代のSessionを復活させない。世代IDを変えた新しいAppStateは旧セッションを知らない。
#[tokio::test]
async fn t10_a_new_core_generation_does_not_know_the_old_session() {
    let old = fixture();
    let fresh = app(AppState::new().with_core_instance_id(Uuid::new_v4()));
    let (status, _) = request(
        &fresh,
        "GET",
        &format!("/karaoke-sessions/{}", old.session),
        Value::Null,
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    let (status, body) = request(
        &fresh,
        "POST",
        &format!("/karaoke-sessions/{}/commands", old.session),
        json!({
            "core_instance_id": old.instance,
            "command_id": Uuid::new_v4(),
            "expected_revision": 0,
            "command": {"type": "play"},
        }),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["code"], "stale_instance");
}

/// T11: Core停止中はコマンドとSSEを503で受けない。
#[tokio::test]
async fn t11_commands_and_subscriptions_are_refused_while_shutting_down() {
    let state = AppState::new();
    let host = state.karaoke_host();
    let snapshot = host
        .create(Uuid::new_v4(), DURATION, ScoringMode::Practice)
        .unwrap();
    host.finish_preparing(snapshot.session_id, Ok(())).unwrap();
    state.begin_shutdown().await;
    let router = app(state);
    let (status, _) = request(
        &router,
        "POST",
        &format!("/karaoke-sessions/{}/commands", snapshot.session_id),
        json!({
            "core_instance_id": snapshot.core_instance_id,
            "command_id": Uuid::new_v4(),
            "expected_revision": 1,
            "command": {"type": "play"},
        }),
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(
        open_events(&router, snapshot.session_id, "").await.status(),
        StatusCode::SERVICE_UNAVAILABLE
    );
}
