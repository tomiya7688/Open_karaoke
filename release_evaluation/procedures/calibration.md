# 基準値の校正手順（19.4）

**これは手順書であり、校正結果ではない。** 現在の台帳[thresholds.json](../thresholds.json)は全閾値が`null`（`ledger_version: 0`、`history: []`）で、blocking項目は`unverified`のままリリース不可になる。
検証: `python -m open_karaoke_analysis.release_calibration release_evaluation/thresholds.json [report.json]`（0: 台帳が正当/リリース可、1: 不可、2: 入力不正）。

## 前提
- 権利処理済みの実歌唱データ（#42）が、**調整用（tuning）**と**holdout**に分割済みで、それぞれ`dataset_manifest.py`のmanifest hashがある。
- 評価は#18のEvaluatorを同一条件・同一モデルsnapshotで実行した結果（`report.json`の値）を使う。非公開音源は報告に含めずhashと集計のみ。

## 閾値決定ルール
1. 調整用データで指標の分布を観測し、候補の閾値（例: `max`指標なら観測p95に余裕を加えた値）を**holdoutを見る前に**決める。理由を文章で残す。
2. holdoutで一度だけ検証する。holdoutを閾値調整に使い回さない（調整用とholdoutのmanifest hashは異なる必要がある）。
3. 製品要件上の下限・上限が先にある指標（hotplug成功率など）は、要件値を理由に明記して採用する。
4. 実機評価の指標（#40/#43）は、手順書[real_device.md](real_device.md)の実測の分布から同様に決める。実測が無い間は`null`のまま。
5. 配布物の`distribution/license_inventory_match`は全件一致を要求する規則（`threshold: 1.0`）で、データ校正の対象外（台帳に含めない）。

## 更新手順（`null` → 版付き値）
1. `thresholds.json`の`history`に1件追加する: `version`（1から連番）、`key`（`area/metric`）、`old`（直前の値。初回は`null`）、`new`、`tuning_manifest_hash`、`holdout_manifest_hash`、`reason`、`approved_by`。
2. 同じ変更で`thresholds`の該当`value`を`new`にし、`ledger_version`を`history`の件数にする。
3. 検証コマンドを実行する。historyが無い値、版の飛び、`old`の不一致、holdout流用、台帳値と履歴再生の不一致は検証で失敗する。
4. `report.json`の`threshold`は台帳と同じ値にする。`apply_thresholds`は台帳の閾値で`pass/fail`を再判定し、食い違う報告を拒否する。
5. 変更履歴はgitの履歴に加えて`history`が正本。過去の版を書き換えず、追記のみとする（変更時は`old`に旧値を入れた新しい版を追加）。

## 変更・見直し
- 閾値の緩和は理由と承認者を必須とし、強化と同じ手順を通す。
- 新しい実データが出た場合は、新しいholdoutで再校正する（旧holdoutを調整用に移して良い）。
