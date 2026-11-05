# 1.0.0 Release Evaluation（評価領域×指標×証拠）

設計: [issue-19-release-evaluation.md](design/issue-19-release-evaluation.md)。
**この表は定義であり、合格の記録ではない。** 実データ・実機の結果が無い項目は`unverified`であり、リリース可否の根拠にならない。
合成fixtureのCI成功（[Testing](testing.md)）と実機・実歌唱の合格は別の証拠である。

## 区分と指標（19.1）

区分: `blocking`=1つでも`fail`/`unverified`なら「リリース可」を出さない。`non-blocking`=記録のみ（暫定。#42のholdout確定時に見直す）。
`threshold`は全て未校正（`null`）で、実データ収集後に版付きで確定する。

| 領域 (area) | 指標 (metric) | 方向 | 区分(暫定) | 証拠種別 | 担当Issue | データ源 |
|---|---|---|---|---|---|---|
| lyrics | cer | max | blocking | 実歌唱評価 | #18, #42 | 権利処理済み実歌唱+holdout |
| lyrics | wer | max | non-blocking | 実歌唱評価 | #18, #42 | 同上 |
| alignment | boundary_p95_ms | max | blocking | 実歌唱評価 | #18, #42 | 人手境界注釈 |
| pitch | cent_error_median | max | blocking | 実歌唱評価 | #18, #42 | 正解F0 |
| notes | note_f1 | min | blocking | 実歌唱評価 | #18, #42 | 人手検証済み譜面 |
| stems | si_sdr_db | min | non-blocking | 実歌唱評価 | #18, #42 | 正解vocals/伴奏のある音源 |
| audio_clock | loopback_latency_p95_ms | max | blocking | 実機評価 | #40, #43 | Windows WASAPI実機 |
| audio_clock | clock_drift_ppm | max | blocking | 実機評価 | #40 | 実機の長時間記録 |
| device | hotplug_recovery | min | blocking | 実機評価 | #43 | USB抜き差し手順（成功率） |
| endurance | long_run_underruns | max | blocking | 実機評価 | #43 | 店舗相当の連続稼働 |
| distribution | license_inventory_match | min | blocking | 配布物評価 | #39, #67 | 成果物hashとinventory（SBOM/NOTICE/FFmpeg buildconf/モデルライセンス） |
| distribution | signature_attestation | min | blocking | 配布物評価 | #67 | 署名・Artifact Attestation |
| product | no_edit_usage_rate | min | non-blocking | 製品評価 | #19, #42 | 人手評価（手順・母集団は事前固定。未決） |

「方向」: `max`=値が閾値以下で合格、`min`=値が閾値以上で合格。
各領域の判定は[report.json契約](#reportjson契約192)の集約を通す。非公開音源・歌唱者情報は報告に含めず、hashと集計のみとする。

## report.json契約（19.2）

スキーマ: [release_evaluation/schema/report.schema.json](../release_evaluation/schema/report.schema.json)。集約・検証: `python/src/open_karaoke_analysis/release_evaluation.py`。

```text
python -m open_karaoke_analysis.release_evaluation <report.json>   # 0: リリース可 / 1: 不可 / 2: 入力不正
```

- `results[].status`は`pass|fail|unverified`。`pass`は`value`と`evidence_ref`が必須。
- `threshold:null`は未校正。`blocking`に挙げた項目は`threshold`が非nullで`value`が方向に沿わなければ`pass`にできない。
- `blocking`に挙げた項目の結果が無い場合は`unverified`扱い。`blocking`が空の報告は不可。
- 歌詞は既存の`evaluation_summary.lyric_summary`の要約から`lyric_result`で変換する。

## 実機評価手順（19.3）

[release_evaluation/procedures/real_device.md](../release_evaluation/procedures/real_device.md)。手順書であり、実施結果ではない。

## 基準値の校正手順（19.4）

[release_evaluation/procedures/calibration.md](../release_evaluation/procedures/calibration.md)。版付き台帳は[release_evaluation/thresholds.json](../release_evaluation/thresholds.json)（現在は全て`null`=未校正）。`python -m open_karaoke_analysis.release_calibration <thresholds.json> [report.json]`で台帳を検証し、台帳の閾値で`pass/fail`を再判定する。手順書であり、校正済みの値は存在しない。

## 配布物・ライセンス監査（19.5）

`python -m open_karaoke_analysis.distribution_audit <tools/ffmpeg> <runtime-manifest.json> --inventory <ffmpeg-inventory.json> --evidence-ref <ref>`。
`scripts/dependency-inventory.ps1 -FfmpegDirectory`が出力する`ffmpeg-inventory.json`（実体hash・buildconf hash）、`runtime-manifest.json`の`ffmpeg`台帳、`NOTICE.txt`、実バイナリの対応と、buildconfにGPL/nonfreeが無いことを検査する（規則は`core/src/ffmpeg_bundle.rs::verify_bundle`と同じ）。結果は`distribution/license_inventory_match`の行（全件一致=`1.0`、閾値`1.0`は規則でありデータ校正値ではない）。必須ファイルまたはinventoryが無い場合は`unverified`。SBOM・モデルライセンス・署名/Attestation(`distribution/signature_attestation`)は#39/#67の成果物待ちで本検査の範囲外（`unverified`のまま）。

## リリース判定ゲート（19.6）

[.github/workflows/release-gate.yml](../.github/workflows/release-gate.yml)。**手動実行のみ**(workflow_dispatch、PR/pushでは動かない)。入力report_path(既定release_evaluation/1.0.0/report.json)に対し上記CLIをそのまま実行し、終了コードがjobの成否になる。0(全blockingがpass)以外は全て失敗: 報告書が無い・JSON不正(2)・fail/unverified(1)。continue-on-errorや「|| true」で失敗を隠さない。判定JSONはartifact release-verdictに残る。

**現状は常に失敗する**: thresholds.jsonは全てnull(未校正)で、実歌唱・実機・配布物の結果も無く、release_evaluation/1.0.0/report.jsonはコミットされていない。blockingのpassは校正済み閾値が必須なので、実データが揃うまで合格に出来ない(合格の偽装禁止)。この性質はpython/tests/test_release_gate.pyが固定する。
