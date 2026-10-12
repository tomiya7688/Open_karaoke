# Open_karaoke

ローカルの原曲から、カラオケ用伴奏・歌詞・譜面を生成し、マイク入力、音程表示、採点まで行うことを目標とするオープンソースカラオケソフトです。

**License: Apache-2.0**

## このリポジトリの構成

```text
Open_karaoke/
├─ core/                     Rust Core
│  ├─ src/                   Core実装
│  └─ tests/                 Coreのテスト
│
├─ python/                   Python Analysis Service
│  ├─ src/                   AI / オフライン解析実装
│  └─ tests/                 解析サービスのテスト
│
├─ gui/                      C# / Avalonia GUI
│  ├─ OpenKaraoke.Gui/       GUI本体
│  └─ OpenKaraoke.sln        Visual Studio solution
│
├─ scripts/                  開発・CI補助スクリプト
│  ├─ build.ps1              全体ビルド
│  ├─ check.ps1              lint / test / static check
│  └─ dependency-inventory.ps1
│                            依存関係・ライセンス確認
│
├─ docs/                     要件・設計・機能仕様
├─ .github/                  GitHub Actions / GitHub設定
│
├─ Cargo.toml                Rust workspace
├─ AI_CONTEXT.md             AI開発時の入口
├─ AGENTS.md                 AI/Agent向けリポジトリ指示
├─ LICENSE                   Apache License 2.0
└─ THIRD_PARTY_NOTICES.md    公式配布物の第三者ライセンス台帳
```

## コンポーネントの役割

### `core/` — Rust Core

製品のリアルタイム処理と中核ロジックを担当します。

主な責務:

- CPAL / WASAPIによるAudio I/O
- マイク入力
- 伴奏再生
- Mixer
- Echo / Reverb
- リアルタイムDSP
- リアルタイムPitch Detection
- 採点
- Song Data管理
- Device Management
- Job Management
- GUI向けAPI
- Python Analysis Serviceの起動・監視

設計は [Architecture](docs/architecture.md)、APIは [Rust Core API](docs/core_api.md) を参照してください。

### `python/` — Python Analysis Service

リアルタイム性を要求しないAI / 楽曲解析を担当します。

主な責務:

- Stem Separation
- Whisperによる歌詞認識
- 歌詞Alignment
- F0 / Pitch解析
- Vocal Event / Boundary Detection
- 採点用譜面生成
- 将来の複数モデル合議 / Song Global Optimizer

全体は [Analysis Pipeline](docs/analysis_pipeline.md)、サービス仕様は [Python Analysis Service](docs/analysis_service.md) を参照してください。

### `gui/` — C# / Avalonia GUI

表示とユーザー操作を担当します。

GUIはRust CoreのAPIのみを利用し、Audio I/OやAI推論を直接担当しません。

想定画面:

- 曲ライブラリ
- 楽曲解析進捗
- カラオケ再生
- 歌詞
- 音程バー
- 採点結果
- オーディオ / マイク設定
- モデル管理
- 将来の譜面・歌詞編集

### `scripts/`

ローカル開発とCIで共有する補助スクリプトです。

Windowsでのセットアップ・ビルド方法は [Development](docs/development.md) を参照してください。

## ドキュメント案内

### まず読むもの

- [Requirements](docs/requirements.md) — 製品として何を作るか
- [Architecture](docs/architecture.md) — Rust / Python / Avaloniaの責務と全体構成
- [Implementation Plan](docs/implementation_plan.md) — 実装順序とIssue分割方針
- [Development](docs/development.md) — 開発環境・ビルド・テスト

### データ・API

- [Song Data Format](docs/song_format.md) — 曲データのJSON形式、48kHz sample timeline
- [Rust Core API](docs/core_api.md) — Core API / Job API
- [Python Analysis Service](docs/analysis_service.md) — Python側REST API

### AI / 楽曲解析

- [Analysis Pipeline](docs/analysis_pipeline.md) — 楽曲解析全体
- [AI Workflow](docs/ai_workflow.md) — AI解析処理の流れ
- [Stem Separation](docs/stem_separation.md) — vocals / accompaniment分離
- [Lyric Transcription](docs/lyric_transcription.md) — Whisperによる歌詞生成
- [Lyric Alignment](docs/lyric_alignment.md) — 既存歌詞 / ASRと時間軸の整列
- [Pitch / F0 Analysis](docs/pitch_analysis.md) — 複数F0 detectorとensemble
- [Vocal Event / Boundary Fusion](docs/vocal_events.md) — 音符・歌詞境界の共通evidenceとfusion
- [Score Generation](docs/score_generation.md) — F0 / Vocal Eventから採点用notes.jsonを生成
- [Model Plugins](docs/model_plugins.md) — AIモデルの差し替え・追加

### カラオケ・採点

- [Scoring](docs/scoring.md) — Pitch / Timing / Coverage / Stabilityによる採点
- [Audio Import](docs/audio_import.md) — WAV / AAC / MP3 / MP4と48kHz正規化

### 品質・ライセンス・調査

- [Testing / CI](docs/testing.md) — 機能ごとのEvaluatorと回帰検出
- [Licensing Policy](docs/licensing.md) — Apache-2.0、公式配布物、MOD/モデルの扱い
- [Research](docs/research.md) — 既存プロジェクト・既存手法の調査
- [Third-Party Notices](THIRD_PARTY_NOTICES.md) — 公式配布物の第三者依存ライセンス

## AI / Agentで開発する場合

[AI_CONTEXT.md](AI_CONTEXT.md) を入口にしてください。

対象Issueに必要なドキュメントと検証範囲だけを読み、不要なコンテキストを増やさない方針です。

Agent固有のルールは [AGENTS.md](AGENTS.md) にあります。

## 開発の進め方

実装タスクはGitHub Issuesで管理しています。

基本的には、

```text
基盤
  ↓
解析サービス
  ↓
歌詞 / F0 / Boundary
  ↓
高精度譜面生成
  ↓
Rust Audio Engine
  ↓
リアルタイム採点
  ↓
Avalonia GUI
  ↓
統合評価
```

の順で進めます。

各機能は、実装と同時にEvaluator / 自動テストを用意し、CIで精度・品質の回帰を検出します。

## ライセンス

Open_karaoke本体は **Apache License 2.0** です。

公式配布物に含めるライブラリ・モデル・重みは、商用利用および必要な再配布条件を確認したものだけを採用します。

ユーザーが後から追加するMOD / Plugin / AIモデルは公式ライセンス審査の対象外です。

詳細は [Licensing Policy](docs/licensing.md) を参照してください。

## コンテンツの権利

Open_karaokeのApache-2.0ライセンスは、ユーザーが読み込む楽曲・録音物・歌詞・譜面等の利用権を付与するものではありません。

利用するコンテンツと利用形態に応じて、必要な権利処理は別途必要です。
