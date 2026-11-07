# #43 オフラインAI推論とリアルタイム音声の資源分離 設計

> 状態: 43.1〜43.4は実装済み（43.3は機構のみで既定値は不変）。実機での効果測定（43.5/H）は未実施でUnverified。最終確認日: 2026-11-08。

## 目的

Whisper/Stem等の重い推論が走っていても、再生・マイクモニターの遅延増加や音切れを起こさない。音声を常に優先する。

## 受入条件（[Issue #43](https://github.com/tomiya7688/Open_karaoke/issues/43) から転記）

- audio callbackにPython/重い推論やblocking filesystem/HTTPを持ち込まない
- 負荷時の音声優先方針が文書化される
- Job失敗/キャンセルが再生停止につながらない
- テスト: 合成負荷、Windows実機でのunderrun・p95 latency・長時間再生・クラッシュ復旧

## 非目標

- 推論の高速化・モデル量子化、CUDA/ROCm対応
- 配布物の最低スペック宣言（#39/#19）
- 新しいスケジューラの自作（OSの優先度とJob枠で足りるかをまず測る）

## 設計

**現状（コードで確認済み）**: Coreのメモリ内Job枠 `MAX_ACTIVE_JOBS=16` / `MAX_RETAINED_JOBS=512` / `MAX_SSE_CONNECTIONS=32`（`core/src/lib.rs`）、SongLibrary取込は`Semaphore(4)`（`song_library/mod.rs`）。Python workerは子プロセス（`core/src/analysis_service.rs`）でaudio callbackとはプロセス・スレッドとも別。**推論の同時実行数・OS優先度・メモリ上限の制御は無い**（grepで該当なし）。

### 方針（音声優先）

1. audio callbackは`AudioIo`の固定容量キューだけを触る（lock/alloc/IO/HTTP/Python禁止）。これは#12/#107の契約で既に要求。
2. Python worker子プロセスを低優先度（Windows: BELOW_NORMALなど）で起動する案。実測で効果が無ければ撤回（未決）。
3. `PlaybackActive`状態の間、新規の重いJob（stems/lyrics/song）は開始待ちにする、または同時実行数を1に制限。実行中Jobは中断せず、キャンセルは利用者操作のみ。
4. Python worker異常終了・Jobキャンセルは再生セッションの状態に影響しない（Job表とセッションは独立所有）。
5. 診断: `AudioCounters`（`output_underrun_frames`・`input_overrun_frames`、`audio_engine.rs`）と再生中Job数を同じ時刻で記録し、underrun発生とJob負荷を相関できるようにする。

**データ契約（案）**: Coreの設定 `inference_policy { max_concurrent_heavy_jobs, defer_while_playing: bool, worker_priority }` を永続化せず起動引数/設定で保持。Job応答に `deferred_reason: Option<String>`（UIが「再生中のため待機」と表示）。
**設定面の定義（43.3。機構は実装済みで既定は現行挙動のまま。既定値を変える判断は43.5/Hの実機比較後）**

| 項目 | 型・取り得る値 | 既定（現行挙動を変えない） | 検証 |
|---|---|---|---|
| `defer_while_playing` | bool | `true`（43.2で実装済み） | 実装済み |
| `defer_max_wait` | 正のDuration | 30分（実装済み） | 実装済み |
| `max_concurrent_heavy_jobs` | 1以上の整数 または 無制限 | 無制限（現状と同じ） | 機構実装済み（環境変数`OPEN_KARAOKE_MAX_HEAVY_JOBS`、`unlimited`または1以上の整数）。効果は未測定 |
| `worker_priority` | `inherit` / `below_normal` / `idle` | `inherit`（現状と同じ） | 機構実装済み（環境変数`OPEN_KARAOKE_WORKER_PRIORITY`、Windowsはpriority classを起動時に付与）。効果は未測定 |

- 範囲外の値は起動時に拒否する（暗黙に丸めない）。`worker_priority`はWindows以外で`inherit`のみ有効とし、他値は警告して`inherit`扱い。
- 既定値を`inherit`/無制限以外へ変える条件は、[inference_isolation.md](../../release_evaluation/procedures/inference_isolation.md)のHで基準より改善が実測されること。実測前に既定を変えない。
- 適用位置は`analysis_service.rs`のworker起動（優先度）とJob開始判定（同時数）。audio callbackからは参照しない。

**エラー**: 待機中Jobのキャンセル/タイムアウトは既存のcancelled/failed遷移を使う。待機は無期限にしない（上限時間は未決）。
**スレッド**: ポリシー判定はJob作成/開始時にCoreのasync側で行う。callbackから参照しない。

## 実装スライス

| # | 内容 | 依存 | 完了条件・テスト |
|---|---|---|---|
| 43.1 | 計測: 合成負荷（CPU/メモリ）下でmock音声経路のunderrun/p95を記録するevaluator | #12 | `core/examples/`系の測定ツール。結果は`Unverified`欄に条件付きで記録。**実装済み**: `core/examples/inference_load_evaluator.rs`（手順は`docs/audio_engine.md`）。合否ゲートなし・CI対象外 |
| 43.2 | 再生状態の公開と`defer_while_playing`（Job開始待ち） | #107, #2 | `core/tests/job_api.rs`: 再生中の重いJobが待機、再生終了で開始、cancel可  **実装済み**: `lib.rs` の `wait_while_playing`(読み取りのみ)・`core/src/lib.rs` 単体試験・`job_api.rs`(python,ignored)。待機上限は既定30分 |
| 43.3 | worker優先度/同時数の設定と適用 | 43.1の結果 | Windows実機で43.1を再測定し効果を比較（効果なしなら採用しない）  **機構実装済み(既定は不変)**: `core/src/inference_policy.rs`(解析・拒否・OS別有効化)、`lib.rs` の `acquire_heavy_slot`(同時数、待機は再生待ちと締切を共有し合計`defer_max_wait`以内・cancel可・`deferred_reason=heavy_job_limit`)、`analysis_service.rs` の `apply_worker_priority`(起動時のみ)、`main.rs`(範囲外の値は起動を拒否)。効果比較(Windows実機で43.1を再測定)は**Unverified**で、既定は変えていない |
| 43.4 | worker crash/Job失敗が再生に波及しない回帰 | #107 | 疑似crash中も`AudioCounters`が増え続けテストが再生継続を確認  **実装済み(契約のみ)**: `job_api.rs` の `job_failure_and_cancel_do_not_disturb_playback` と python crash試験。実機は未確認 |
| 43.5 | 長時間+低スペック実機測定（#19の一部） | #39 | 手順書化。実機結果なしに合格と書かない  **手順書化済み**: [inference_isolation.md](../../release_evaluation/procedures/inference_isolation.md)（E負荷下再生/F長時間/G失敗復旧/H優先度比較）。実機測定は未実施でUnverified |

## 触るファイル

`core/src/lib.rs`（Job作成の枠・ポリシー）、`core/src/analysis_service.rs`（worker起動引数/優先度）、`core/src/audio_engine.rs`（`AudioCounters`読み出しのみ）、`python/src/open_karaoke_analysis/service.py`（変更は最小）、`core/tests/job_api.rs`、`docs/analysis_service.md`・`docs/audio_engine.md`の更新。`core/src/lib.rs`は競合しやすい。

## 守る規則

- [realtime分離](../architecture.md#設計原則)と[宣言コメント必須](../coding_rules.md#5-ソースコードコメント)
- 実測前に数値目標・合格を断定しない。合成負荷の成功を実機の安定性と呼ばない（[testing](../testing.md)）
- CI（Linux）で測れるのは契約のみ。Windows実機はUnverifiedとして分けて報告

## 未決事項

- worker優先度の効果（実測次第）とWindows以外の扱い
- 再生中Job待機の上限時間と、GPU/VRAM競合の扱い
- 「再生中」状態の公開方法: 43.2で `AppState::playback_active()`(セッションstateがplaying)に確定
