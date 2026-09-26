# Open_karaoke

ローカルの原曲から、カラオケ用伴奏・歌詞・譜面を生成し、マイク入力、音程表示、採点まで行うことを目標とするオープンソースカラオケソフトです。

**License: Apache-2.0**

## Repository layout

- `core/` — Rust Core
- `python/` — Python Analysis Service
- `gui/` — C# / Avalonia GUI
- `scripts/` — build, check, dependency inventory

Windowsでの開発手順は [Development](docs/development.md) を参照してください。

AIによる開発は [AI_CONTEXT.md](AI_CONTEXT.md) を入口にし、対象Issueの参照先と検証範囲を先に絞ります。

## Documents

- [要件](docs/requirements.md)
- [アーキテクチャ](docs/architecture.md)
- [実装計画](docs/implementation_plan.md)
- [Rust Core API](docs/core_api.md)
- [曲データ形式](docs/song_format.md)
- [モデル拡張設計](docs/model_plugins.md)
- [ライセンス方針](docs/licensing.md)
- [解析パイプライン](docs/analysis_pipeline.md)
- [採点設計](docs/scoring.md)
- [品質評価・CI](docs/testing.md)
- [既存手法・参考プロジェクト](docs/research.md)
- [Python Analysis Service](docs/analysis_service.md)
- [Stem separation](docs/stem_separation.md)
- [Pitch / F0 analysis](docs/pitch_analysis.md)
- [Vocal Event fusion](docs/vocal_events.md)

## Content rights

Open_karaoke自体のApache-2.0ライセンスは、ユーザーが読み込む楽曲・録音物・歌詞・譜面等の利用権を付与するものではありません。利用形態に応じて必要な権利処理を行ってください。
