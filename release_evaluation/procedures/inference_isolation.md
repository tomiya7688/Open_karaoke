# AI推論負荷下の音声優先 実機評価手順（Issue #43 スライス43.5 / 43.3比較）

**これは手順書であり、実施結果ではない。** 実機で実施するまで、全項目を`unverified`のままにする。
合成負荷（`inference_load_evaluator`）やCIの成功を、実機の安定性の根拠として書かない。
様式・前提・証拠の扱いは[real_device.md](real_device.md)に合わせる。結果は別ファイルに残し、`evidence_ref`で参照する。
個人情報・非公開音源は記録しない。

## 前提（記録必須）
- real_device.mdの「前提」すべて（`app_sha`、配布物SHA-256、OS版、機材、ドライバ版、モード、サンプルレート、バッファ）
- 機体区分: **低スペック機**（CPUコア数・クロック、RAM、GPUの有無とVRAM、ストレージ種別）と、比較用の標準機があればその諸元。低スペックの定義は#39/#19が決めるまで未決。実測機の諸元をそのまま記録する
- 負荷条件: 実Python worker（解析Job種別 `stems` / `lyrics` / `song`）、同時Job数、モデル名と版。モックは使わない
- ポリシー設定: `defer_while_playing`、`defer_max_wait`、（43.3で導入後）`max_concurrent_heavy_jobs`、`worker_priority`
- 手順書の版: 本ファイルのgit commit

## 事前（合成負荷、CIでは測れない部分の補助）
1. 対象機で`inference_load_evaluator`をidleと負荷ありで実行する（`docs/audio_engine.md`）。結果JSONを保存する。
2. これは補助であり、以降の実Job負荷下の結果を代替しない。

## E. 負荷下の再生（metric: `endurance/long_run_underruns`の一部、#43）
1. 歌唱セッションを`playing`にし、ループバック（real_device.md A）を接続する。
2. 解析Jobを再生中に投入する。`defer_while_playing=true`では重いJobが`deferred`（`deferred_reason: playback_active`）になること、再生終了後に開始することを確認する。
3. `defer_while_playing=false`（負荷を再生と同時に掛ける最悪ケース）でも同じ時間窓を測る。
4. 各窓で次を同じ時刻軸で記録する: `output_underrun_frames`と`input_overrun_frames`の増分、ループバック遅延のp95（200回以上）、CPU使用率、メモリ最大、実行中Job数。
5. 負荷なし（ベースライン）と負荷ありを同じ機材・同じ曲で比較する。

## F. 長時間（metric: `endurance/long_run_underruns`）
1. 連続稼働時間は**未決**（real_device.md Dと同じ）。決まるまで暫定8時間とし、報告に暫定と明記する。
2. 再生・解析Job投入・キャンセルを繰り返す。underrun回数、最大メモリ、メモリ増加傾向、ハンドル数、クラッシュの有無を1時間ごとに記録する。
3. 待機上限（既定30分）を超えた`deferred_timeout`の発生数も記録する。

## G. 失敗・復旧（#43受入条件: Job失敗/キャンセルが再生停止につながらない）
1. 再生中にworkerプロセスを強制終了する。Jobは`failed`となり、再生のunderrun増分が許容範囲を超えず、セッション状態が`playing`のままであることを確認する。
2. 再生中に実行中Jobと待機中Jobをキャンセルする。再生が継続することを確認する。
3. worker再起動後、新規Jobが開始できることを確認する。
4. 各3回以上。失敗時はログの該当箇所（個人情報を除く）を残す。

## H. worker優先度・同時数の比較（43.3の採否判断）
1. 設定の組合せごとにE・Fの短縮版（各30分以上）を同じ順序で測る: `worker_priority=inherit`（基準）/ `below_normal` / `idle`、`max_concurrent_heavy_jobs`=無制限 / 2 / 1。
2. 比較指標は、underrun増分、ループバックp95、Job完了時間（重いJobが過度に遅くならないか）。
3. 効果が基準と有意に変わらなければ**採用しない**。「有意」の基準は閾値が未校正の間は定めず、生の分布を記録して人が判断する。

## 記録様式
```text
実施日 / 実施者 / app_sha / 配布物SHA-256 / 手順書commit
機材: OS, デバイス型番, ドライバ版, モード, サンプルレート, バッファ
機体: CPU, コア数, RAM, GPU/VRAM, ストレージ
負荷: Job種別, 同時数, モデル名と版
ポリシー: defer_while_playing, defer_max_wait, max_concurrent_heavy_jobs, worker_priority
項目: E|F|G|H
結果: underrun増分, overrun増分, p95(ms), CPU, メモリ最大, Job完了時間, 試行数, 失敗数, 除外した外れ値と理由
証拠: ログ/CSV/evaluator JSONの保存場所とSHA-256
逸脱: 手順からの変更点
判定: 閾値(校正済みなら版付き) と status(pass|fail|unverified)
```
閾値が未校正（`null`）の間は、実測値を記録しても`status`は`unverified`とする。
