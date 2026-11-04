# #43 オフラインAI推論とリアルタイム音声の資源分離 設計

> 状態: 設計のみ。実測は音声基盤（#12/#107）が動いてから。数値目標は実機基線後に決める。

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

**方針（音声優先）**
1. audio callbackは`AudioIo`の固定容量キューだけを触る（lock/alloc/IO/HTTP/Python禁止）。これは#12/#107の契約で既に要求。
2. Python worker子プロセスを低優先度（Windows: BELOW_NORMALなど）で起動する案。実測で効果が無ければ撤回（未決）。
3. `PlaybackActive`状態の間、新規の重いJob（stems/lyrics/song）は開始待ちにする、または同時実行数を1に制限。実行中Jobは中断せず、キャンセルは利用者操作のみ。
4. Python worker異常終了・Jobキャンセルは再生セッションの状態に影響しない（Job表とセッションは独立所有）。
5. 診断: `AudioCounters`（`output_underrun_frames`・`input_overrun_frames`、`audio_engine.rs`）と再生中Job数を同じ時刻で記録し、underrun発生とJob負荷を相関できるようにする。

**データ契約（案）**: Coreの設定 `inference_policy { max_concurrent_heavy_jobs, defer_while_playing: bool, worker_priority }` を永続化せず起動引数/設定で保持。Job応答に `deferred_reason: Option<String>`（UIが「再生中のため待機」と表示）。
**エラー**: 待機中Jobのキャンセル/タイムアウトは既存のcancelled/failed遷移を使う。待機は無期限にしない（上限時間は未決）。
**スレッド**: ポリシー判定はJob作成/開始時にCoreのasync側で行う。callbackから参照しない。

## 実装スライス
| # | 内容 | 依存 | 完了条件・テスト |
|---|---|---|---|
| 43.1 | 計測: 合成負荷（CPU/メモリ）下でmock音声経路のunderrun/p95を記録するevaluator | #12 | `core/examples/`系の測定ツール。結果は`Unverified`欄に条件付きで記録。**実装済み**: `core/examples/inference_load_evaluator.rs`（手順は`docs/audio_engine.md`）。合否ゲートなし・CI対象外 |
| 43.2 | 再生状態の公開と`defer_while_playing`（Job開始待ち） | #107, #2 | `core/tests/job_api.rs`: 再生中の重いJobが待機、再生終了で開始、cancel可 |
| 43.3 | worker優先度/同時数の設定と適用 | 43.1の結果 | Windows実機で43.1を再測定し効果を比較（効果なしなら採用しない） |
| 43.4 | worker crash/Job失敗が再生に波及しない回帰 | #107 | 疑似crash中も`AudioCounters`が増え続けテストが再生継続を確認 |
| 43.5 | 長時間+低スペック実機測定（#19の一部） | #39 | 手順書化。実機結果なしに合格と書かない |

## 触るファイル
`core/src/lib.rs`（Job作成の枠・ポリシー）、`core/src/analysis_service.rs`（worker起動引数/優先度）、`core/src/audio_engine.rs`（`AudioCounters`読み出しのみ）、`python/src/open_karaoke_analysis/service.py`（変更は最小）、`core/tests/job_api.rs`、`docs/analysis_service.md`・`docs/audio_engine.md`の更新。`core/src/lib.rs`は競合しやすい。

## 守る規則
- [realtime分離](../architecture.md#設計原則)と[宣言コメント必須](../coding_rules.md#5-ソースコードコメント)
- 実測前に数値目標・合格を断定しない。合成負荷の成功を実機の安定性と呼ばない（[testing](../testing.md)）
- CI（Linux）で測れるのは契約のみ。Windows実機はUnverifiedとして分けて報告

## 未決事項
- worker優先度の効果（実測次第）とWindows以外の扱い
- 再生中Job待機の上限時間と、GPU/VRAM競合の扱い
- 「再生中」状態の公開方法（#107のセッション状態とJob層の結合点）
