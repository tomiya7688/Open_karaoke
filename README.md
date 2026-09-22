# Open_karaoke

ローカルの原曲から、カラオケ用伴奏・歌詞・譜面を生成し、マイク入力、音程表示、採点まで行うことを目標とするオープンソースカラオケソフトです。

## Repository layout

- `core/` — Rust Core
- `python/` — Python Analysis Service
- `gui/` — C# / Avalonia GUI
- `scripts/` — build, check, dependency inventory

Windowsでの開発手順は [Development](docs/development.md) を参照してください。

## Documents

- [要件](docs/requirements.md)
- [アーキテクチャ](docs/architecture.md)
- [実装計画](docs/implementation_plan.md)
- [Rust Core API](docs/core_api.md)
- [曲データ形式](docs/song_format.md)
- [モデル拡張設計](docs/model_plugins.md)
- [解析パイプライン](docs/analysis_pipeline.md)
- [採点設計](docs/scoring.md)
- [品質評価・CI](docs/testing.md)
- [既存手法・参考プロジェクト](docs/research.md)
