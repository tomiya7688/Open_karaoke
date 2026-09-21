# アーキテクチャ

## 対象OS

初期対象は Windows とする。

将来的な他OS対応を妨げない構成にはするが、初期実装では Windows での安定動作を優先する。

## 配布方針

ユーザーに Python、FFmpeg、AIモデル実行環境などの個別セットアップを要求しない。

アプリ本体、必要なランタイム、補助ツール、既定モデル実行環境は可能な限りアプリ側で同梱または自動管理する。

採用コンポーネントは商用利用可能であることを必須条件とし、コード・重み・バイナリ・依存関係ごとにライセンスを確認する。

## アプリ構成

初期候補:

- GUI: React
- Desktop shell: Tauri
- Native / realtime core: Rust
- AI analysis process: Python

GUIフレームワーク自体は、ユーザー側に追加環境構築を要求しないことを最優先とする。

## Rust と Python の責務

### Rust / App Core

- GUIとの連携
- 曲ライブラリ管理
- 音声再生
- マイク入力
- Mixer
- Echo / Reverb
- 低遅延処理
- 採点用リアルタイム処理
- AI解析プロセスの起動・監視
- APIクライアント
- キャッシュ管理

### Python / Analysis Service

- Stem Separation
- Whisper ASR
- Forced Alignment
- Pitch / F0 analysis
- Note transcription
- Song structure analysis
- 将来のOrchestrator
- 将来のTransformer validator
- 将来の内製モデル

## 通信方式

アプリ本体とAI解析プロセスの通信には通常のローカルREST APIを使用する。

解析はJobとして扱い、テスト・自動化からGUIを介さず実行できるようにする。

例:

```text
GET    /health
GET    /models

POST   /jobs
GET    /jobs/{job_id}
POST   /jobs/{job_id}/cancel

POST   /analysis/stems
POST   /analysis/lyrics
POST   /analysis/alignment
POST   /analysis/pitch
POST   /analysis/notes
POST   /analysis/song
```

Job状態の最低限:

- queued
- running
- completed
- failed
- cancelled

Jobには進捗、処理段階、開始/終了時刻、エラー、生成物参照を持たせる。

`POST /analysis/song` は、初期実装では原曲から必要な解析を一括実行する上位APIとする。

進捗通知にはSSEを第一候補とし、双方向制御が必要になった場合のみWebSocketを検討する。

## オーディオ内部仕様

すべての入力音源は解析・再生用内部フォーマットへ正規化する。

初期仕様:

- Sample rate: 48,000 Hz
- 内部タイムライン: 整数
- 基準時刻: sample index
- DSP向けPCM: float32を基本候補とする

時刻は浮動小数秒を正本にせず、sample indexを正本とする。

UIやログ表示時のみ秒へ変換する。

## マイク処理

一般的なカラオケと同様、ユーザーのマイク音声を低遅延でモニタリングしつつ、採点処理にも利用する。

```text
Microphone
   |
   +--> Raw signal --> Pitch Detection --> Scoring
   |
   +--> Gain --> Echo / Reverb --> Mixer --> Output

Accompaniment ----------------------^
```

採点用には原則としてエフェクト前の生マイク信号を使う。

## モデル拡張性

初期実装は以下の系統でE2Eを通す。

- Stem Separation: 商用利用条件を確認した高精度モデル
- ASR: Whisper large-v3系
- Alignment: WhisperX系
- Pitch: 複数のF0 detectorを切替・併用可能にする
- Note transcription: Basic Pitchを候補生成器として含む専用統合パイプライン

譜面生成はコア機能のため、単一モデルの出力をそのまま正式譜面にせず、F0、onset、音節境界、曲キー、note transcription、時間方向最適化を統合して最初から精度重視で設計する。

### F0 / Pitch Detection 方針

F0検出器は1種類に固定しない。

最低でも以下を満たす。

- detector単位で切替可能
- 複数detectorの同時実行が可能
- 各detectorのconfidenceを保持
- 同一時刻のpitch候補を統合可能
- detectorごとの失敗・octave errorを他detectorで補完可能
- Evaluatorでモデル単体とensemble双方を比較可能

候補としてCREPE系、pYIN、YIN系などを比較する。

初期設定では複数detectorのensembleを利用しつつ、デバッグ・性能比較・低スペック環境向けに単一detectorへ切り替えられるようにする。

統合器は単純平均に固定せず、confidence、voiced probability、近傍時間との連続性、octave consistency等を用いて最終F0候補を決定できる構造にする。

モデルはコードへ固定埋め込みせず、役割単位で差し替え可能にする。

## 初期実装の最優先事項

まずは以下のE2Eを完成させる。

```text
Original Song
  -> normalize
  -> stem separation
  -> Whisper lyrics
  -> lyric timing
  -> high-accuracy score generation
  -> save song data
  -> accompaniment playback
  -> microphone input
  -> pitch bar
  -> scoring
```

複数ASR、内製モデル、Transformer破綻検出、Song Global Optimizer、whole-song optimization、曲単位LoRA / Adapterは、E2E完成後の精度改善フェーズで追加する。
