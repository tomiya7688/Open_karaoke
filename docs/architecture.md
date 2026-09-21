# アーキテクチャ

## 対象OS

初期対象は Windows とする。

将来的な他OS対応を妨げない構成にはするが、初期実装では Windows での安定動作を優先する。

## 配布方針

ユーザーに Python、FFmpeg、AIモデル実行環境などの個別セットアップを要求しない。

アプリ本体、必要なランタイム、補助ツール、既定モデル実行環境は可能な限りアプリ側で同梱または自動管理する。

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

例:

```text
GET  /health
GET  /models

POST /analysis/stems
POST /analysis/lyrics
POST /analysis/alignment
POST /analysis/pitch
POST /analysis/notes
POST /analysis/song
```

`POST /analysis/song` は、初期実装では原曲から必要な解析を一括実行する上位APIとする。

将来的な進捗通知、長時間解析、キャンセル制御のために、必要であればSSEまたはWebSocketを補助的に追加できる。

## オーディオ内部仕様

すべての入力音源は解析・再生用内部フォーマットへ正規化する。

初期仕様:

- Sample rate: 48,000 Hz
- 内部タイムライン: 整数
- 基準時刻: sample index
- DSP向けPCM: float32を基本候補とする

時刻は浮動小数秒を正本にせず、sample indexを正本とする。

例:

```text
start_sample = 590400
sample_rate  = 48000

time = 12.3 sec
```

UIやログ表示時のみ秒へ変換する。

## マイク処理

一般的なカラオケと同様、ユーザーのマイク音声を低遅延でモニタリングしつつ、採点処理にも利用する。

重要な分岐:

```text
Microphone
   |
   +--> Raw signal --> Pitch Detection --> Scoring
   |
   +--> Gain --> Echo / Reverb --> Mixer --> Output

Accompaniment ----------------------^
```

採点用には原則としてエフェクト前の生マイク信号を使う。

エコーやリバーブをかけた信号をPitch Detectionへ戻さない。

## モデル拡張性

初期実装は以下の組み合わせでE2Eを通す。

- Stem Separation: Demucs系
- ASR: Whisper large-v3系
- Alignment: WhisperX系
- Pitch: CREPE / pYIN / YINのうち初期採用品
- Note transcription: Basic Pitchまたは自前変換

ただしモデルはコードへ固定埋め込みせず、役割単位で差し替え可能にする。

想定ロール:

- ASR
- StemSeparator
- Aligner
- PitchDetector
- NoteTranscriber
- Validator
- Optimizer

将来的には開発者でなくてもモデルパッケージを追加できる形を目標とする。

## モデルパッケージ

例:

```text
models/
  whisper-large-v3/
    manifest.json
    ...
  htdemucs/
    manifest.json
    ...
  custom-singing-asr/
    manifest.json
    ...
```

`manifest.json` には最低限以下を持たせる。

```json
{
  "id": "whisper-large-v3",
  "name": "Whisper Large V3",
  "type": "asr",
  "backend": "whisper",
  "version": "1",
  "languages": ["ja", "en", "multi"]
}
```

将来はモデル追加UI、互換性チェック、ダウンロード、更新も検討する。

## 初期実装の最優先事項

まずは高度な合議ではなく、以下のE2Eを完成させる。

```text
Original Song
  -> normalize
  -> stem separation
  -> Whisper lyrics
  -> lyric timing
  -> pitch / notes
  -> save song data
  -> accompaniment playback
  -> microphone input
  -> pitch bar
  -> basic scoring
```

複数ASR、内製モデル、Transformer破綻検出、Song Global Optimizer、whole-song optimization、曲単位LoRA / Adapterは、E2E完成後の精度改善フェーズで追加する。
