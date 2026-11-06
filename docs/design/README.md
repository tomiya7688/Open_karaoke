# 設計書索引（Issue別）

> 2026-11-05 / 基点 `main` `907d166`。ここは**索引**であり、仕様の正本は各設計書とsourceである。
> 実装状況の正本は [Current status](../current_status.md)。設計書は「実装済み」を意味しない。
> 着手手順は [実装チェックリスト](../implementation_checklist.md) に従う。

使い方: 対象Issueの行 → 設計書1つだけ読む → その「実装スライス」の次の1つを取る → 設計書を先に更新して実装。
設計書が無い行は、右端の既存docsが正本。新規の責務を足す場合は、先に本ディレクトリへ設計書を追加する。

## Issue一覧

「状態」は実装有無の要約（精度・実機の受入は別）。未マージPR #111 が追加するファイルは `コード表記` で示し、マージ後にリンクへ置換する。

| Issue | 状態 | 設計書 / 正本 | 依存 | 次に取るスライス |
|---|---|---|---|---|
| #2 Core API/Job | 実装済み（Job永続化は未） | [Core API](../core_api.md) | - | 永続化方針（要判断） |
| #3 Song Data | 実装済み | [Song Format](../song_format.md) | - | なし（#38で拡張） |
| #4 音源入力/48kHz | 実装あり（Job未接続） | [Audio Import](../audio_import.md) | - | [#36 36.1](issue-36-source-import-flow.md) |
| #6 Stem | Adapter実装/実録未評価 | [Stem](../stem_separation.md) | #4 | #42のデータ後に評価 |
| #7 歌詞認識 | Adapter実装/実録未評価 | [歌詞認識](../lyric_transcription.md) | #6 | 同上 |
| #8 歌詞整列 | Adapter実装/実録未評価 | [Alignment](../lyric_alignment.md) | #7 | 同上 |
| #13 監視/Mixer | DSPのみ。デバイス接続・API・GUI未 | [Audio engine §Mixer](../audio_engine.md#mixer-and-monitoring-effects--issue-13-first-slice)、`docs/karaoke_session_design.md` | #12 | #107のworker組成で接続 |
| #14 リアルタイム採点 | 未実装 | [#14設計](issue-14-realtime-scoring.md)、[Scoring](../scoring.md) | #12 #40 | 14.1 |
| #15 GUI/Core接続 | シェル実装済み | [Desktop client](../desktop_client.md) | #41 | #16/#110と並行 |
| #16 カラオケ画面 | 表示のみ（再生連動未） | [#16設計](issue-16-karaoke-screen.md) | #107 #14 | 16.2（#107のAPI確定後） |
| #17 モデル選択 | 検出・保存選択済み。song各段はPython側pin済み | [Model Plugins](../model_plugins.md) | - | [#109設計](issue-109-model-plan.md) 109.2 |
| #18 Evaluator/CI | 一部実装 | [Testing](../testing.md)、[実装単位](../implementation_plan.md#18の実装単位p0は空けておく) | - | #18.6cまで実装済み。精度回帰ゲートは後続 |
| #19 リリース評価 | 未 | [#19設計](issue-19-release-evaluation.md) | #18 #42 #39 | 19.1 |
| #36 全曲解析/登録 | Python/登録API済み。元音源〜GUI未 | [#36残り設計](issue-36-source-import-flow.md)、[全曲解析](../full_song_analysis.md)、[登録](../song_import.md) | #4 | 36.1 |
| #37 外部MIDI | 実装あり（実MIDI corpus未評価） | [MIDI import](../midi_import.md) | #3 #11 | 実MIDIでの評価 |
| #38 任意編集 | 38.1〜38.3実装済み（検証・保存・`PATCH /songs/{id}`）。GUI未（P3） | [#38設計](issue-38-note-editor.md) | #3 #15 #16 | 38.4 |
| #39 Windows配布 | 配布候補zipのみ（39.1 配置解決実装済み） | [#39設計](issue-39-windows-packaging.md) | #15 #17 #67 | 39.2 |
| #40 クロック/遅延 | 合成契約済み。物理校正未 | [Audio clock](../audio_clock.md)、[実装単位](../implementation_plan.md#40の実装単位p0は空けておく) | #12 | plan記載の次単位 |
| #41 Core API保護 | 一部実装、PR #95 | [Core API](../core_api.md)、[実装単位](../implementation_plan.md#41の実装単位p0は空けておく) | #2 #15 | PR #95 の統合後 |
| #42 実歌唱データ | 一部実装 | [データセット](../dataset_evaluation.md)、[実装単位](../implementation_plan.md#42の実装単位p0は空けておく) | #18 | plan記載の次単位 |
| #43 推論分離 | 未 | [#43設計](issue-43-inference-isolation.md) | #12 #107 | 43.1/43.2/43.4契約実装済み、43.3設定面定義・43.5手順書済み(実機は未確認) |
| #44 多ASR研究 | 後回し | `docs/quality_and_ux_design.md` | #7 #8 #42 | 基線評価後 |
| #106 設計レビュー | PR #111 | `docs/implementation_navigation.md` | - | PR #111 の統合 |
| #107 歌唱Session | 107.1実装（純粋な状態機械）、107.2実装（認証付きtransport API、デバイス無し）。deviceは未 | [#107設計](issue-107-karaoke-session.md)、`docs/karaoke_session_design.md`(PR #111) | #12 #13 #14 #40 #41 | 107.3（Controller・worker組成・device lease、#12/#13依存） |
| #108 品質/再解析 | 108.1実装（版付きquality_report型・合成flag→レポートの純関数）。service接続・Repair・Core/GUI未 | [#108設計](issue-108-quality-report.md)、`docs/quality_and_ux_design.md`(PR #111) | #36 #42 | 108.2（service/song接続と`quality_report.json`公開） |
| #109 実効モデル固定 | 設計（PR #111） | `docs/quality_and_ux_design.md` | #17 #36 #41 | 同上 |
| #110 初回UX | 設計（PR #111） | `docs/quality_and_ux_design.md` | #12 #13 #16 #17 #39 #40 #41 #43 | 同上 |
| #58 PR整理 / #66 Python CA / #67 署名 | 運用・セキュリティ | [Development](../development.md#python-trust-tooling-66) | - | 設計書不要（コード設計を伴わない） |

## 依存順（歌えるE2Eまでの推奨）

1. 先行契約: #40（clock）、#41（Core API）、#18/#42（評価基盤）。いずれも[実装単位](../implementation_plan.md)に分割済み。
2. 曲を用意する経路: #4 → #36（36.1〜36.4）。MIDI経路 #37 は自動解析を待たず採点/UIを試す入力になる。
3. 歌唱経路: #12 → #107（セッション）→ #14（14.1〜14.4は#107と独立に先行可）→ #13接続 → #16（16.2〜16.5）。
4. 品質と使いやすさ: #109 → #108 → #110。#43は音声基盤の実測後。
5. 仕上げ: #39（配布）、#38（編集、任意）、#19（リリース評価）。#44は基線後。

並列に進めるときは、同じ`core/src/lib.rs`・`ShellViewModel`を触るスライスを同時に出さない（競合）。

## 設計書の書式（新規追加時）
固定節: 目的 / 受入条件（Issueから転記）/ 非目標 / 設計（責務・データ契約・エラー・スレッド制約）/ 実装スライス / 触るファイル / 守る規則 / 未決事項。
存在しないAPI・型を既存のように書かない（「新設」「案」と明記）。未確認事項は未決事項へ。
