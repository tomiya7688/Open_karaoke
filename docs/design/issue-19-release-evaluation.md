# #19 v1.0.0リリース評価 設計

> 状態: 設計のみ（P2、最終ゲート）。基準値の最終数値は実データ収集後に校正する（Issue Out of scope）。
> 合成fixtureのCI成功と実機・実歌唱の合格は**別の証拠**（[Testing §1.0.0](../testing.md#100-release-evaluation未完了)）。

## 目的
1.0.0のリリース可否を、再現可能な手順・基準・証拠で判定する統合評価セットを定義する。

## 受入条件（[Issue #19](https://github.com/tomiya7688/Open_karaoke/issues/19) から転記）
- Release Evaluation手順が再現可能
- metric baselineが記録される
- blocking regression criteriaが定義される
- 主要機能が要件通り一通り動作する

## 非目標
- 基準値そのものの最終固定（実データ収集後）
- 評価データの権利処理・収集（#42）、Evaluator基盤（#18）、実機遅延の測定器実装（#40）
- 高度ASR等の研究（#44）

## 設計
**責務**: 個別Evaluator・実機手順・配布物検査の**結果を集約して判定する層**。新しいアルゴリズムは作らない。
**評価区分（混同しない）**
1. 合成回帰（CI）: 既存workflow（pitch/note/vocal-event/lyric-regression等、[Testing](../testing.md#ciの入り口)）。アルゴリズム回帰防止であり店頭品質ではない。
2. 実歌唱評価: #42の権利処理済みデータ+holdout、同一条件で複数モデル比較（`dataset_manifest.py`）。指標は[Testing §機能別指標](../testing.md#機能別指標)（CER/WER、SI-SDR、alignment p95、cent誤差、note F1）。
3. 実機評価: Windows WASAPI実機のloopback遅延 中央値/p95、clock drift、デバイス抜き差し、長時間（店舗相当）稼働、underrun（#40/#43）。
4. 配布物評価: クリーンWindows、SBOM/NOTICE/FFmpeg buildconf、モデルライセンス、署名/Attestation（#39/#67）。
5. 製品評価: 「手直しなしの利用率」。人手評価の手順と母集団を事前固定。

**データ契約（案）**: `release_evaluation/<version>/report.json { app_sha, dataset_manifest_hash, model_snapshot, results[{area, metric, value, threshold|null, status: pass|fail|unverified, evidence_ref}], blocking[] }`。`threshold:null`は未校正。`status`に「未評価」を`pass`として入れない。非公開音源は報告に含めずhashと集計のみ。
**エラー/判定**: blocking基準に`fail`か`unverified`が1つでもあれば「リリース可」を出さない。基準値はholdout収集後に版付きで確定し、変更履歴を残す。

## 実装スライス
| # | 内容 | 依存 | 完了条件・テスト |
|---|---|---|---|
| 19.1 | 評価領域×指標×証拠種別の表（blocking/非blockingの暫定区分）をdocsに定義 | #18 | 本書+`testing.md`を更新、全指標に担当Issueとデータ源がある |
| 19.2 | `report.json` schemaと集約スクリプト（既存`evaluation_summary.py`を再利用） | #18 | schema検証、既知pass/fail/unverified fixtureで判定が正しい |
| 19.3 | 実機評価手順書（loopback、長時間、抜き差し）と記録様式 | #40, #43 | 手順どおり第三者が再実行可能。実施結果は別途 |
| 19.4 | 基準値の校正手順（holdout、閾値決定ルール、変更履歴） | #42 | 閾値が`threshold:null`→版付き値へ移る手順の定義 |
| 19.5 | 配布物・ライセンス監査の組込み | #39, #67 | 成果物hashとinventoryの対応チェック |
| 19.6 | リリース判定ゲート（CI手動workflow） | 19.2-19.5 | 全blocking pass以外はjob失敗 |

## 触るファイル
`docs/testing.md`、`python/src/open_karaoke_analysis/evaluation_summary.py`・`job_runner.py`（読むのが中心）、新設`release_evaluation/`（schemaと手順）、`.github/workflows/`（手動workflow）。

## 守る規則
- 未評価を「合格」と書かない（[Testing](../testing.md#100-release-evaluation未完了)、[AI_CONTEXT](../../AI_CONTEXT.md)の境界）
- 非公開音源・歌唱者の個人情報を公開CI artifactへ出さない（#42）
- [宣言コメント必須](../coding_rules.md#5-ソースコードコメント)、[ライセンス監査](../licensing.md#release-gate)

## 未決事項
- 代表曲数・歌唱者数・ジャンル構成の最小値（#42のデータ量次第）
- 「手直しなし利用率」の測り方と母集団
- 長時間稼働の時間定義（店舗相当の連続時間）
