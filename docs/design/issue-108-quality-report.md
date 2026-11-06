# #108 版付き解析品質レポート 設計

> 状態: 108.1 実装（Python純関数とデータ型のみ）。108.2以降は未実装。精度・実歌唱での有効性は主張しない（合成flagでの契約テストのみ）。
> 上位設計は [quality_and_ux_design.md](../quality_and_ux_design.md)（PR #139 で統合済み）。衝突時は同書へ追随する。

## 目的
構造的に正しいだけで不正確な解析出力を、成功/失敗とは別の「品質状態」として版付きで記録する。再解析(RepairPlanner)の入力になる土台を、Core/GUIに依存しない自己完結した層として先に作る。

## 受入条件（Issue #108 と依頼から番号化）
1. `quality_report` は `format_version` 付きの版付きデータ型とJSON形式を持つ。未知の版・未知のキー・重複キー・型違いは `QualityReportError` で拒否する（fail closed）。
2. 次を記録する: assessor(model/weights/options とその `options_sha256`)、各段のモデル参照、入力artifactの名前・版・SHA-256、flagごとのsample範囲（48 kHz整数）、診断名、校正状態、処理出典(attempt)。
3. 校正状態 `hard_integrity` / `diagnostic` / `calibrated` を分離する。`calibrated` は `calibration_id` が必須、他は `calibration_id` を持てない。`hard_integrity` は `heuristic_score` を持てない。
4. 不正なSHA-256（64桁小文字16進数以外）と、`report_sha256` 不一致は読込時に拒否する。
5. 品質警告は解析成功とは別の状態: レポートは成功/失敗を表さず `playability` = `playable` / `playable_with_warnings` / `blocked` だけを持つ。`blocked` は `hard_integrity` flag（必須契約違反）があるときだけ。診断警告は `playable_with_warnings` 止まり。`playability` と `warning_codes` は導出値で、読込時に再計算して一致を要求する。
6. 0..1 heuristic は `heuristic_score` と呼び、`probability`/`confidence` 等のキーを許さない。確率として扱ってよいのは `calibrated` のflagに限り、それでもキー名は変えない。
7. 同じ入力+設定で出力バイト列が決定的（キー順固定、flagの正規ソート、`ensure_ascii=False`、NaN/Infの拒否）。
8. 合成flag入力（Whisper repeat/hallucination、alignment低coverage、境界不整合、上流のintegrity検査結果）から純関数でレポートを組める。閾値は版付き `QualityOptions` に置き `options_sha256` へ反映する。

## 非目標（後続スライス）
| スライス | 内容 |
|---|---|
| 108.2 | `service.py`/`song.py` へ接続し完了artifactに `quality_report.json` を公開（重複公開検証） |
| 108.3 | RepairPlanner（上限回数・長さ・CPU/VRAM/時間・キャンセル、共有artifact変更追跡と下流invalidate） |
| 108.4 | RepairExecutor（選択的再解析、attempt出典の記録、`kind=repair` と親レポートhash） |
| 108.5 | Core API転送とGUIのwarning/repair availability表示（一意コードの文言表） |
| 108.6 | #42権利処理済みセットでの校正・ベースライン比較（`calibrated` を使う唯一の経路） |

## 設計
- 場所: `python/src/open_karaoke_analysis/quality_report.py`（型・検証・直列化・組立の純関数。I/O・時計・乱数なし）。
- 型: `QualityOptions` / `ModelRef` / `InputRef` / `Attempt` / `QualityFlag` / `QualityReport`（すべて frozen dataclass、継承なし）。信号入力: `LyricSegmentSignal` / `AlignmentCoverageSignal` / `BoundarySignal` / `IntegrityCheck`。
- flagコード: `whisper_repeat` `whisper_hallucination` `alignment_low_coverage` `boundary_inconsistent`（診断、`diagnostic`）と `integrity_violation`（`hard_integrity`）。
- 時刻は48 kHz整数sample index（`0 <= start < end`）。秒やモデルhopは持たない。
- `attempt = {pass_index, kind: initial|repair, parent_report_sha256}`。`repair` は親hash必須、`initial` は親を持てない。
- 関数: `build_quality_report`（組立）, `report_to_json`（正規直列化）, `report_from_json`（検証付き読込）, `assess_*`（信号→flag）。
- JSON形: `{format_version, assessor, attempt, inputs[], stage_models[], flags[], playability, warning_codes[], report_sha256}`。`report_sha256` は当該キーを除いた正規JSONのSHA-256。

## 守る規則
48 kHz整数、日本語宣言コメント、継承禁止、モデル/校正を推測しない（未公開値はnull）、数値精度を主張しない。

## 未決事項
- GUI文言と一意コードの表（108.5）。
- `calibrated` の `calibration_id` の発行元と、#42データでの検証手順（108.6）。
