# #109 全曲解析のモデルプラン固定と欠落モデル方針 設計

> 状態: 109.1（Python側）、109.2（Core転送）、109.4のfail closed比較部分を実装。109.3（GUI）とweight digestは未実装。数値・精度の主張はしない。

## 目的
個別Jobには保存済みモデル選択が適用されるが、`song` Job内部の各段（stems/lyrics/alignment/pitch/events/notes）には未反映だった。Job受付時点で各段のモデルを確定（pin）し、その決まり方を完了artifactへ記録する。

## 受入条件（[Issue #109](https://github.com/tomiya7688/Open_karaoke/issues/109) から番号化）
1. 優先順位は 明示指定(`options.models`) > 有効な保存済み選択 > 公式既定 を各段に適用する。
2. 確定プランはJob受付時に1回だけ作る。以後に`model_selection.json`・packageが変わっても進行中Jobは変わらない。
3. 各段のID/版/オプションと、選択の出所（source）・保存ID・fallback理由・解決時刻を完了artifact（provenance）へ記録する。
4. 保存済み選択が使えない段は既定へ戻し、`default_fallback`と理由を明示する（エラーにしない）。
5. 無効ID・role不一致・消えたpackage・明示指定の回帰がない（明示指定の不正は従来どおり失敗）。
6. 分析結果の同一性（`analysis_fingerprint`）はモデル・オプションだけで決まり、選択の出所では変わらない。
7. manifestがbackend参照のみのpackageは重み差し替えではない（現状は`backend`のAdapterを使う）と明記する。
8. ユーザーMODの任意ライセンス利用をloader側で禁止しない（変更なし）。

## 実装スライス
| # | 内容 | 状態 |
|---|---|---|
| 109.1 | Python: `submit`でsong各段の確定プラン(pin)を作り、provenanceへ`selection`を記録。テスト | 実装 |
| 109.2 | Core: Job応答へ`model_id`/`model_source`/`selected_model_id`/`selection_fallback_reason`と段プラン`model_plan`を転送（Python Jobにも`model_plan`を追加） | 実装 |
| 109.3 | GUI: 「選択したもの」と「実際に動いたもの」の表示、backend参照のみの注記 | 未 |
| 109.4 | Evaluator比較モードのfail closed（`Job::require_exact_model_plan`）は実装。weight digest/runtime のsnapshotは未（weightはAdapter側が公開していない） | 一部 |

## 109.1 データ契約
`provenance.stages[i].selection = {source: request|selection|default|default_fallback, selected_model_id: str|null, fallback_reason: str|null}`、`provenance.model_plan_resolved_at`（ISO8601 UTC）。`stages[i].model`は実際に動いたAdapterの情報。`analysis_fingerprint`の入力（model/options）は変更しない。

## 触るファイル
`python/src/open_karaoke_analysis/service.py`（受付時プラン）、`adapters.py`（`AnalysisContext.model_plan`）、`song.py`（記録）、`python/tests/test_model_selection_jobs.py`、`docs/model_plugins.md`、`docs/current_status.md`。

## 109.2 / 109.4 データ契約（Core）
Core Jobは任意で `model_id`・`model_source`(request|selection|default|default_fallback)・`selected_model_id`・`selection_fallback_reason`・`model_plan`（`{resolved_at, <stage>: {source, selected_model_id, fallback_reason}}`の平坦な形、songのみ）を持つ。worker値は未信頼入力: 未知のsource・未知フィールド・512文字超・制御文字・17段以上・不正な段名・理由のないfallbackは`analysis_protocol`でJob失敗（公開しない）。`Job::require_exact_model_plan`は比較評価用で、provenance欠落・`default_fallback`（Job全体または段）・songの段プラン欠落を`model_plan_missing`/`model_plan_fallback`で拒否する。
