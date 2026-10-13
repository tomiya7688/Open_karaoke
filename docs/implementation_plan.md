# Implementation Plan

## この文書の役割

この文書は**実装順序とIssueへの導線**です。現在使える機能の判定は
[Current status](current_status.md)、具体的な実行手順は
[Development](development.md) を参照してください。

0.1.x系は小さくリリースしてよい一方、1.0.0は[Requirements](requirements.md)にある主要機能を一通り実装し、
権利・モデル重み・遅延・実録歌唱精度を含むRelease Evaluationを通過する必要があります。

## 実装のまとまり

| 領域 | GitHub Issue | 開発の入口 |
|---|---|---|
| ワークスペース/CI土台 | [#1](https://github.com/tomiya7688/Open_karaoke/issues/1) | [Development](development.md) |
| Core Job API | [#2](https://github.com/tomiya7688/Open_karaoke/issues/2) | [Core API](core_api.md) |
| Song JSON | [#3](https://github.com/tomiya7688/Open_karaoke/issues/3) | [Song Format](song_format.md) |
| 音源読み込み/48kHz | [#4](https://github.com/tomiya7688/Open_karaoke/issues/4) | [Audio Import](audio_import.md) |
| Python Service | [#5](https://github.com/tomiya7688/Open_karaoke/issues/5) | [Analysis Service](analysis_service.md) |
| Stem / 歌詞 / Alignment | [#6](https://github.com/tomiya7688/Open_karaoke/issues/6)、[#7](https://github.com/tomiya7688/Open_karaoke/issues/7)、[#8](https://github.com/tomiya7688/Open_karaoke/issues/8) | [Stem](stem_separation.md)、[ASR](lyric_transcription.md)、[Alignment](lyric_alignment.md) |
| F0 / Vocal Events / 譜面生成 | [#9](https://github.com/tomiya7688/Open_karaoke/issues/9)、[#10](https://github.com/tomiya7688/Open_karaoke/issues/10)、[#11](https://github.com/tomiya7688/Open_karaoke/issues/11) | [Pitch](pitch_analysis.md)、[Vocal Events](vocal_events.md)、[Score Generation](score_generation.md) |
| Rust音声再生・DSP・採点 | [#12](https://github.com/tomiya7688/Open_karaoke/issues/12)〜[#14](https://github.com/tomiya7688/Open_karaoke/issues/14) | [Architecture](architecture.md)、[Scoring](scoring.md) |
| Avalonia GUI / カラオケ画面 | [#15](https://github.com/tomiya7688/Open_karaoke/issues/15)、[#16](https://github.com/tomiya7688/Open_karaoke/issues/16) | `gui/`、[Architecture](architecture.md) |
| モデル選択と拡張 | [#17](https://github.com/tomiya7688/Open_karaoke/issues/17) | [Model Plugins](model_plugins.md) |
| Evaluator / Release gates | [#18](https://github.com/tomiya7688/Open_karaoke/issues/18)、[#19](https://github.com/tomiya7688/Open_karaoke/issues/19) | [Testing / CI](testing.md)、[Licensing](licensing.md) |

**2026-10-14時点**: #1、#5、#9〜#11はclosed、#2〜#4と#6〜#8、#12〜#19はopen。
ただし、openのIssueにも実装済みのコードがある場合があります。実装が存在しても、
実データ評価やIssueの受け入れ条件が未完なら完了としません。最新状態は[Issues](https://github.com/tomiya7688/Open_karaoke/issues)を確認してください。

## 開発順序と依存

基本は基盤 → 解析 → 譜面 → 音声 → GUI → 統合評価。ただしCIやEvaluatorは最後にまとめず、**機能追加時に同時実装**します。

- Rust CoreのJob APIとSong Dataを安定させてから、解析Adapterが出すartifactを連結する。
- F0・Vocal Event・notesは同じ48kHz sample timelineと入力hashを共有する。
- GUIはRust Coreの公開APIへ接続し、Pythonの非公開ポートには接続しない。
- 全曲Job `analysis.song`、自動SongStoreへの登録、店舗向けリアルタイム音声機能は別途統合が必要。
- 実曲精度・商用配布ライセンス・Windows機材検証は1.0.0をブロックし得る品質ゲート。

## Issueの受け入れ条件

各Issueに目的、Scope/Out of scope、入力と出力、依存、Acceptance Criteria、Evaluator、
実行コマンドと検証結果を記録します。

合成fixtureによるAPI・回帰テスト成功、実モデルのロード成功、実録歌唱での精度確認、
製品としてのE2E/パッケージ検証は**別の証拠**として扱います。
