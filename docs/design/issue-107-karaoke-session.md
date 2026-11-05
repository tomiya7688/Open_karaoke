# #107 Core所有の KaraokeSession 設計

> 状態: 107.1（純粋な状態機械）を実装。107.2以降は**未実装**。数値・実機品質の主張はしない。
> 参照のみ: PR #111 の `docs/karaoke_session_design.md`（草案。マージ前のため本書は内容を再掲せず、差異は下記「草案との差」に記す）。

## 目的と責務の分け方
再生・マイク・採点・クロックを横断する「1つの歌唱セッション」の**状態とコマンド順序の正本**をCoreに置く。
本スライスはデバイス・スレッド・HTTPを持たない純粋型だけを作る。理由: 状態遷移・冪等性・epoch排除は音声デバイス無しで決定的にテストでき、後続スライス（worker組成、transport）が同じ契約に乗れるため。

継承は使わず、後続のworker群は本型を**保持・委譲**する。`AudioIo`/`ClockSession`/`Mixer`/`WavFeeder` には触らない。

## 受入条件（Issue #107 を番号化。実装後に1件ずつ照合）
| # | 条件 | 107.1での扱い |
|---|---|---|
| AC1 | UIなしで 保存曲→再生状態→提示timestamp→停止 の一連が成立 | 実装（状態機械＋presented報告。保存曲の実体検証は107.2） |
| AC2 | preparing→ready→playing↔paused→finished/failed/interrupted の遷移と不正遷移の拒否 | 実装（全状態×全コマンド表をテスト） |
| AC3 | play/pause/seek/stop の冪等性と重複操作拒否 | 実装（no-op、`command_id`再送、同IDで別内容は拒否、履歴128件上限） |
| AC4 | `expected_revision` による競合拒否 | 実装（Stopのみ免除） |
| AC5 | epoch不一致の観測（進行位置・加点・割込み）を除外し件数を残す | 実装 |
| AC6 | 位置は**提示済み**timestampのみ。送信済みframe数から推測しない | 実装（frame数を受け取るAPIを作らない。後退を拒否） |
| AC7 | 機器抜去・stream errorは明示的に`interrupted`。自動復旧・自動再生をしない | 実装（状態機械側）。実デバイス検出は107.2 |
| AC8 | 再起動で旧Sessionを復活させない。旧instanceのコマンド/イベントを拒否 | 実装（`core_instance_id`照合、永続化しない） |
| AC9 | 2セッションの競合を処理（device lease相当は1つだけ） | 実装（registryが非終端セッション1つのみ許可）。実device leaseは107.2 |
| AC10 | practice/whole-songを区別。シーク後の重複加点禁止 | 実装（`ScoreEligibility`、加点区間の重複除去ledger） |
| AC11 | 状態イベントは上限付き。遅い購読者はgapを検知しsnapshotで復旧 | 実装（`SessionEventLog`）。SSE配線は107.4 |
| AC12 | audio callbackはlock/alloc/network/Python無し | 本スライスはcallbackに接続しない（型に音声デバイス依存なし、`unsafe`禁止） |
| AC13 | APIは#41のversioningに従う | 追加のみ（`pub mod karaoke_session`）。既存公開APIは不変。URLは未確定のまま |
| AC14 | GUI/scoreに届くtimestamp/scoreにsource/quality情報 | 部分: 本スライスは位置の`PositionSource::Presented`と`ScoreEligibility`まで。`clock_quality`は107.3 |

## 実装スライス
| # | 内容 | 依存 | 状態 |
|---|---|---|---|
| 107.1 | 純粋な状態機械・registry・イベントログ・加点ledger（`core/src/karaoke_session.rs`） | - | **実装** |
| 107.2 | `KaraokeSessionController`: SongLibrary検証、device lease、MusicDecode/CaptureDispatcher/PlaybackMix worker組成、stream error→`interrupt` | #12 #13 | 未 |
| 107.3 | output presentation timestamp（`ClockSession`のanchor/遅延品質）をpresented報告へ接続、`clock_quality` | #40 | 未 |
| 107.4 | 認証付きtransport（HTTP snapshot/command、上限付きSSE）、C# client契約 | #41 #36 | 未 |
| 107.5 | リアルタイム音程・採点の接続（credit ledgerを利用） | #14 | 未 |
| 107.6 | GUI接続・hotplug復旧UI・実機ゲート | #16 #40 #19 | 未 |

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

## 草案(PR #111)との差
- 草案の`Stopped`を踏襲。`command_id`履歴は草案例どおり128。
- 草案は「Stop on terminalは同じsnapshot」。本書はさらにStopをrevision免除にした（理由は上記）。
- 草案のAPI表は107.4で#41に合わせて確定する。本スライスでは経路名・URLを決めない。

## 未確定・検証方法
- 実デバイスのhotplug検出、可聴時刻の測定遅延: 107.2/107.3と#40/#19の実機ゲート。
- SSE上限・購読数: 107.4で測定して決める（64は暫定）。
