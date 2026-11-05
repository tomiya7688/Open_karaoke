# #107 Core所有の KaraokeSession 設計

> 状態: 107.1（純粋な状態機械）と107.2（認証付きtransport API、デバイス無し）を実装。107.3（Controller合成、fakeのみ）を実装。107.4（提示timestamp→`clock_quality`、合成のみ）を実装。107.5以降は**未実装**。数値・実機品質の主張はしない。
> 参照のみ: PR #111 の `docs/karaoke_session_design.md`（草案。マージ前のため本書は内容を再掲せず、差異は下記「草案との差」に記す）。

## 目的と責務の分け方
再生・マイク・採点・クロックを横断する「1つの歌唱セッション」の**状態とコマンド順序の正本**をCoreに置く。
本スライスはデバイス・スレッド・HTTPを持たない純粋型だけを作る。理由: 状態遷移・冪等性・epoch排除は音声デバイス無しで決定的にテストでき、後続スライス（worker組成、transport）が同じ契約に乗れるため。

継承は使わず、後続のworker群は本型を**保持・委譲**する。`AudioIo`/`ClockSession`/`Mixer`/`WavFeeder` には触らない。

## 受入条件（Issue #107 を番号化。実装後に1件ずつ照合）
| # | 条件 | 107.1での扱い |
|---|---|---|
| AC1 | UIなしで 保存曲→再生状態→提示timestamp→停止 の一連が成立 | 実装（状態機械＋presented報告。保存曲の実体検証は107.3） |
| AC2 | preparing→ready→playing↔paused→finished/failed/interrupted の遷移と不正遷移の拒否 | 実装（全状態×全コマンド表をテスト） |
| AC3 | play/pause/seek/stop の冪等性と重複操作拒否 | 実装（no-op、`command_id`再送、同IDで別内容は拒否、履歴128件上限） |
| AC4 | `expected_revision` による競合拒否 | 実装（Stopのみ免除） |
| AC5 | epoch不一致の観測（進行位置・加点・割込み）を除外し件数を残す | 実装 |
| AC6 | 位置は**提示済み**timestampのみ。送信済みframe数から推測しない | 実装（frame数を受け取るAPIを作らない。後退を拒否） |
| AC7 | 機器抜去・stream errorは明示的に`interrupted`。自動復旧・自動再生をしない | 実装（状態機械側）。実デバイス検出は107.3 |
| AC8 | 再起動で旧Sessionを復活させない。旧instanceのコマンド/イベントを拒否 | 実装（`core_instance_id`照合、永続化しない） |
| AC9 | 2セッションの競合を処理（device lease相当は1つだけ） | 実装（registryが非終端セッション1つのみ許可）。実device leaseは107.3 |
| AC10 | practice/whole-songを区別。シーク後の重複加点禁止 | 実装（`ScoreEligibility`、加点区間の重複除去ledger） |
| AC11 | 状態イベントは上限付き。遅い購読者はgapを検知しsnapshotで復旧 | 実装（`SessionEventLog`）。SSE配線は107.2で実装 |
| AC12 | audio callbackはlock/alloc/network/Python無し | 本スライスはcallbackに接続しない（型に音声デバイス依存なし、`unsafe`禁止） |
| AC13 | APIは#41のversioningに従う | 追加のみ（`pub mod karaoke_session`）。既存公開APIは不変。URLは未確定のまま |
| AC14 | GUI/scoreに届くtimestamp/scoreにsource/quality情報 | 107.1: `PositionSource::Presented`と`ScoreEligibility`。107.4: snapshotの`clock_quality`を追加（実装、合成のみ） |

## 実装スライス
| # | 内容 | 依存 | 状態 |
|---|---|---|---|
| 107.1 | 純粋な状態機械・registry・イベントログ・加点ledger（`core/src/karaoke_session.rs`） | - | **実装** |
| 107.2 | 認証付きtransport API（状態取得・play/pause/seek/stop・上限付きSSE）を状態機械へ配線。デバイス無し（`core/src/karaoke_session_api.rs`） | 107.1 | **実装** |
| 107.3 | `KaraokeSessionController`: 曲の読取(`SongSource`)、device lease(`DeviceLeaseProvider`)、準備(preparing→ready)、stream error→`interrupt`、復旧。抽象とfakeでの合成のみ(`core/src/karaoke_controller.rs`)。実SongLibrary配線・実device lease・CPAL/worker組成は**後続**(下記) | #12 #13 | **合成を実装** |
| 107.4 | output presentation timestamp（`ClockSession`のanchor/遅延品質）をpresented報告へ接続、`clock_quality` | #40 | **実装（合成のみ）** |
| 107.5 | C# client契約、SSE上限・購読数の実測 | #41 #36 | 未 |
| 107.6 | リアルタイム音程・採点の接続（credit ledgerを利用） | #14 | 未 |
| 107.7 | GUI接続・hotplug復旧UI・実機ゲート | #16 #40 #19 | 未 |

## 107.1 データ契約
場所: `core/src/karaoke_session.rs`、テスト: `core/tests/karaoke_session.rs`。

- 時間はすべて48kHz整数sample（曲タイムライン）。`duration_samples >= 1`。
- `KaraokeState`: `preparing | ready | playing | paused | interrupted | finished | stopped | failed`。終端は `finished/stopped/failed`。
- `KaraokeSession`（1セッションの状態）。所有者はregistryのみ。生成: `create`、破棄: 終端後に次の`create`で置換。並行アクセスはControllerの単一ループが直列化する前提（内部ロックは持たない）。
- `state_revision`: 状態遷移・シークのたびに+1。進行位置の更新や加点では増やさない。
- `transport_epoch`: 再生に影響する遷移（play/pause/seek/stop/interrupt/recover/finish/fail）ごとに+1。`preparing→ready`では増やさない（初期値1）。
- `CommandEnvelope { core_instance_id, session_id, command_id, expected_revision, command }`。
- 結果は `CommandOutcome { disposition: applied | no_op | replayed, snapshot }`。エラーは `KaraokeError`（`code()`で`not_ready` `terminal` `device_unavailable` `stale_revision` `stale_instance` `stale_epoch` `out_of_range` `command_id_conflict` `session_not_found` `session_busy` `not_playing` `position_regressed` `invalid_range` `ledger_full`）。エラーは状態を変えない。成功のみ`command_id`履歴に記録する。
- `Snapshot`: `session_id, core_instance_id, song_id, state, state_revision, transport_epoch, position_song_sample, position_source(none|committed|presented), duration_samples, scoring_mode, score_eligibility, error_code, stale_rejections`。

### 遷移表（AC2）
| 現状態 | play | pause | seek(s) | stop |
|---|---|---|---|---|
| preparing | `not_ready` | `not_ready` | `not_ready` | stopped（取消） |
| ready | playing | no-op | 位置更新（同位置はno-op） | stopped |
| playing | no-op | paused | 位置更新・新epoch（常にapplied） | stopped |
| paused | playing（新epoch） | no-op | 位置更新（同位置はno-op） | stopped |
| interrupted | `device_unavailable` | `device_unavailable` | `device_unavailable` | stopped |
| finished/stopped/failed | `terminal` | `terminal` | `terminal` | no-op（同じ終端snapshot） |

seekは `s < duration_samples` のみ（`out_of_range`）。内部イベント: `finish_preparing(ok|error)`、`report_presented(epoch, sample)`（playingのみ。`duration`到達で`finished`）、`interrupt(epoch)`（ready/playing/paused→interrupted、preparingはfailed）、`recover()`（interrupted→復旧前がreadyならready、それ以外はpaused。自動再生しない）、`fail(code)`。

### 冪等性と競合（AC3/AC4）
1. `command_id`が履歴にあり内容が同じ→前回の同じsnapshotを`replayed`で返す。内容が違う→`command_id_conflict`。
2. Stop以外は `expected_revision != state_revision` なら`stale_revision`（何も変えない）。Stopは「終端へ向かう安全な意図」のため、古いrevisionでも受理する（既に終端なら同じ終端snapshot）。
3. 履歴は128件の有界。枠外の古いIDの再送は、状態変更を伴う命令なら`expected_revision`が既に進んでいるため`stale_revision`、no-opなら再実行しても無害。したがって履歴の追い出しで二重適用は起きない。

### 加点とモード（AC10）
`ScoreEligibility`: `practice`（練習指定）、`whole_song`（連続）、`downgraded(seek|interrupted)`。whole-song指定は、再生開始後のseekまたは0以外へのseek、あるいは再生中/一時停止中のinterruptで練習扱いに格下げされ、戻らない。`credit(epoch, start, end)` はplayingかつepoch一致のとき、既に加点済みの区間を除いた**新規区間のみ**を返し台帳へ登録する（区間は結合、最大256区間、超過は`ledger_full`）。シークで戻って同じ区間を歌っても二重加点にならない。

### Registry（AC8/AC9）
`KaraokeSessionRegistry::new(core_instance_id)`。非終端セッションが存在する間の`create`は`session_busy`。終端なら置換。他のsession_idは`session_not_found`。永続化・復元はしないので、Core再起動後の新registryは旧セッションを知らない（旧instanceのコマンドは`stale_instance`）。

### イベントログ（AC11）
`SessionEvent { event_seq, core_instance_id, session_id, state_revision, transport_epoch, snapshot }` を遷移ごとに追加。容量64、超過は古い方から捨てる。`events_after(seq)` は連続して取れれば`events`、取りこぼしがあれば`gap{oldest_event_seq}`（クライアントはsnapshotで復旧）。

## 107.2 transport API（実装済み）
場所: `core/src/karaoke_session_api.rs`、テスト: `core/tests/karaoke_session_api.rs`。既存の公開APIは変えず追加のみ（`pub mod karaoke_session_api`、`AppState::karaoke_host()` / `with_core_instance_id()`、`KaraokeSession::last_event_seq()`、`KaraokeSessionRegistry::current_snapshot()`、`SessionEvent`にSerialize）。認証・Origin・版検査は既存の層（実行ファイルでは`CoreSession`）が担い、本ルートは他の保護対象ルートと同じ`route_layer`の内側に置く。

| Method | Path | 内容 |
|---|---|---|
| GET | `/karaoke-sessions/current` | 現行セッションのsnapshot。無ければ404 `session_not_found` |
| GET | `/karaoke-sessions/{id}` | 指定セッションのsnapshot（未知は404） |
| POST | `/karaoke-sessions/{id}/commands` | 本文 `{core_instance_id, command_id, expected_revision, command:{type:play\|pause\|seek\|stop, song_sample?}}`。成功は `{disposition, snapshot}`。`expected_revision`はStopのみ省略可 |
| GET | `/karaoke-sessions/{id}/events[?after=N]` | SSE。`Last-Event-ID`でも再開位置を指定可 |

- 失敗は `{"error","code"}`。`code`は`KaraokeError::code()`。HTTP: 未知session=404、`out_of_range`等の値不正=422、状態競合（stale_revision/stale_instance/command_id_conflict/not_ready/terminal/device_unavailable/session_busy）=409。未知フィールド・未知のtype・seekの`song_sample`欠落は4xx。失敗は状態を変えない。
- クライアントは**セッション作成・準備完了・提示位置・採点区間・中断を報告できない**（routeを作らない）。これらは将来のControllerが`KaraokeSessionHost`経由で行い、位置は提示済みtimestampだけ（AC6）。host経由の変更は同じlock内でイベントを配信し、配信漏れ・順序入替えを防ぐ。
- SSE: `after`なしは最初にevent名`snapshot`（現在状態と最新`event_seq`）、`after`ありは続きの`state`イベント、ログから落ちていれば`gap`（`oldest_event_seq`と現在snapshot）を送る。以降は`state`をliveで送る。各eventの`id`は`event_seq`。終端状態のeventを送ったら閉じる。購読開始と過去分の取得は同じlockで行い取りこぼさない。遅い購読者（liveの容量64を超過）は静かに落とさずストリームを閉じ、クライアントは再接続して`snapshot`/`gap`で復旧する。
- SSEの同時購読は全セッション合計8本（超過は429 `too_many_subscriptions`）。8は暫定で、実測は107.5。Core停止中（`begin_shutdown`後）のコマンド・購読は503。
- 世代: 実行ファイルは`CoreSession`のinstance_idを`with_core_instance_id`で渡す。再起動後の新しいhostは旧セッションを知らず、旧世代宛のコマンドは`stale_instance`（AC8）。

### 107.2 受入条件の照合（テスト名 = `core/tests/karaoke_session_api.rs`）
| # | 条件 | テスト |
|---|---|---|
| T1 | 状態取得（current/id/未知） | `t1_snapshot_and_current_are_readable` |
| T2 | play/pause/seek/stopが状態機械に届く（AC1/AC2） | `t2_play_pause_seek_stop_reach_the_state_machine` |
| T3 | 冪等・競合・Stop免除（AC3/AC4） | `t3_idempotency_and_revision_conflicts_are_rejected` |
| T4 | 不正要求の拒否と状態不変 | `t4_invalid_requests_are_rejected_without_changing_state` |
| T5 | 位置は提示済みのみ（AC6） | `t5_position_comes_only_from_the_presented_report` |
| T6 | SSE snapshot→遷移→終端で閉じる（AC11） | `t6_sse_streams_snapshot_then_transitions_and_closes_on_terminal` |
| T7 | 再接続・gap（AC11） | `t7_reconnect_resumes_or_reports_a_gap` |
| T8 | 購読の上限と回復 | `t8_sse_subscriptions_are_bounded_and_recover` |
| T9 | 遅い購読者はclose→再取得 | `t9_slow_subscriber_is_closed_and_can_resync` |
| T10 | 旧世代を復活させない（AC8） | `t10_a_new_core_generation_does_not_know_the_old_session` |
| T11 | 停止中は503 | `t11_commands_and_subscriptions_are_refused_while_shutting_down` |

## 107.3 Controller（合成のみ・実装済み）
場所: `core/src/karaoke_controller.rs`、テスト: `core/tests/karaoke_controller.rs`。既存公開APIは変えず追加のみ(`pub mod karaoke_controller`、`KaraokeSessionHost::recover`)。デバイスは開かない。

- `SongSource::open(song_id) -> PreparedSong{duration_samples}`、`DeviceLeaseProvider::acquire(session_id) -> Box<dyn DeviceLease>`(dropで返却)。実装は後続スライスで SongLibrary / CPAL に差し替える。
- `select_song`: 曲を読めなければセッションを作らない(`song_unavailable`)。読めれば host に preparing を作る。非終端が残れば `session_busy`。
- `prepare`: 貸出に成功すれば ready、失敗は failed(`device_unavailable`)。preparing中に取消(stop)されていれば貸出を保持しない。`start` は両者の連続。
- `stream_error(origin, code)`: host の `interrupt` を呼び貸出を返す。自動復旧・自動再生しない。`recover(session_id)`: 貸出を取り直せた時だけ host の `recover`(ready/paused へ。失敗は interrupted のまま)。
- `reconcile()`: セッションが ready/playing/paused でなければ貸出を返す(クライアントのstop後など。`select_song`/`stream_error` は内部で呼ぶ)。貸出は同時に1つ。
- 再起動: 新しい host(新instance)と空の貸出で始まり、旧Sessionは復活せず、旧instance宛は `stale_instance`。

| # | 条件 | テスト(`core/tests/karaoke_controller.rs`) |
|---|---|---|
| T1 | 選曲→準備→ready→play(AC1/AC2) | `t1_select_prepare_reaches_ready_and_can_play` |
| T2 | 読めない曲はセッションを作らない | `t2_unreadable_song_creates_no_session` |
| T3 | 貸出競合でfailed(AC9) | `t3_second_session_fails_when_the_device_is_leased` |
| T4 | 2セッション目はbusy、終端後は可(AC9) | `t4_second_selection_is_busy_until_the_first_ends` |
| T5 | 準備中の取消で貸出を保持しない | `t5_cancelled_preparation_does_not_keep_the_lease` |
| T6 | stream errorでinterrupted・返却・自動復旧なし(AC7) | `t6_stream_error_interrupts_and_releases_without_auto_recovery` |
| T7 | 復旧は貸出を取れた時だけ・自動再生なし(AC7) | `t7_recover_requires_the_lease_and_never_autoplays` |
| T8 | 中断中以外の復旧は拒否 | `t8_recover_is_refused_unless_interrupted` |
| T9 | 再起動で旧Sessionを復活させない(AC8) | `t9_restart_does_not_resurrect_the_old_session` |
| T10 | stop後のreconcileで返却 | `t10_reconcile_releases_the_lease_after_a_client_stop` |

後続(未実装): 実 SongLibrary 検証、実 device lease、MusicDecode/CaptureDispatcher/PlaybackMix worker 組成、CPAL stream error の実検出、Controllerのバックグラウンド駆動(reconcileの定期呼出し)。実機品質は未測定。

## 107.4 提示timestampとclock_quality（実装済み・合成のみ）
場所: `core/src/karaoke_session.rs`、テスト: `core/tests/karaoke_session_clock.rs`。既存公開APIは追加のみ（`ClockQuality`、`PresentedStreamSample`、`SessionSnapshot::clock_quality`、`KaraokeError::ClockRejected`、`report_presented_from_clock`（session/registry/host））。既存`report_presented`は品質`unknown`で動作不変。

- `PresentedStreamSample(u64)`: 出力が提示したstream sample。送信済み/書込済みframe数と型で区別する（AC6）。
- `report_presented_from_clock(epoch, &ClockSession, clock_epoch, presented)`: session epoch確認→Output anchorで曲sampleへ写す→既存の提示規則（playingのみ・後退/範囲外拒否・曲末でfinished）→位置と品質を更新。clockのepoch不一致は`stale_rejections`に計上し`clock_stale_epoch`、anchor無し・曲頭前は`clock_unavailable`（状態不変、0へclampしない）。
- `ClockQuality`: `LatencyCalibration`を1対1で写す（`unknown`/`estimated`/`measured{uncertainty_samples}`）。昇格しない。seekで確定した位置と提示前は`unknown`。revisionは増やさない（位置更新と同じ）。

| # | 条件 | テスト(`core/tests/karaoke_session_clock.rs`) |
|---|---|---|
| T1 | 未測定遅延は`unknown`（0遅延/測定済みにしない） | `t1_unknown_latency_is_reported_as_unknown_not_zero_or_measured` |
| T2 | estimated/measured(不確かさ)を昇格なしで保持 | `t2_estimated_and_measured_quality_are_preserved_without_promotion` |
| T3 | 位置は提示mapping由来で生stream数ではない(AC6) | `t3_position_follows_the_presented_mapping_not_the_raw_stream_count` |
| T4 | clock失敗は状態不変、古いclock epochは計上 | `t4_clock_failures_leave_state_unchanged_and_stale_clock_is_counted` |
| T5 | session規則(stale epoch/後退/範囲外/非playing)維持(AC5) | `t5_session_rules_still_apply_stale_epoch_regression_range_and_state` |
| T6 | seekで`unknown`に戻る、曲末到達で品質保持 | `t6_seek_resets_quality_and_reaching_the_end_keeps_it` |
| T7 | JSONに`clock_quality`(AC14) | `t7_plain_report_presented_has_unknown_quality_and_json_carries_the_field` |
| T8 | 他instance/sessionはclockを使う前に拒否(AC8) | `t8_registry_rejects_foreign_origin_before_using_the_clock` |

未確認: 実デバイスのpresentation timestamp供給、実測遅延・不確かさ、AudioIo/Controllerからの配線（後続、#40/#19の実機ゲート）。

## 草案(PR #111)との差
- 草案の`Stopped`を踏襲。`command_id`履歴は草案例どおり128。
- 草案は「Stop on terminalは同じsnapshot」。本書はさらにStopをrevision免除にした（理由は上記）。
- 草案のAPI表は#41のversioningに合わせて確定する。107.2のURLは暫定（`X-Open-Karaoke-Api-Version: 1`の下に追加のみ。変更時は#41の手順で版を上げる）。

## 未確定・検証方法
- 実デバイスのhotplug検出、可聴時刻の測定遅延: 107.3/107.4と#40/#19の実機ゲート。
- SSE上限・購読数: 107.5で測定して決める（イベントログ64・購読8は暫定）。
