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

## 後続（本書の範囲外）

19.4 基準値の校正手順(#42後)、19.5 配布物監査の組込み(#39/#67)、19.6 リリース判定ゲートworkflow。
