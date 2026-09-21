# 既存手法・参考プロジェクト

この文書は技術選定前の調査メモである。採用を確定したライブラリ一覧ではない。

## 参考プロジェクト

### Nightingale

今回の構想に近い統合型カラオケプロジェクト。

参考になる点:

- ローカル曲ライブラリ
- ボーカル分離
- Whisper / WhisperX 系の歌詞処理
- マイク入力
- ピッチ採点
- キー / テンポ変更
- Tauri / Rust とPython解析プロセスの分離
- 解析結果キャッシュ

主に「統合アプリとしてどう構成するか」の参考にする。

### UltraSinger

原曲からUltraStar向け歌詞・音程データを生成する方向で非常に近い。

参考になる点:

- Demucsによる歌声分離
- Whisperによる歌詞解析
- F0抽出
- 音符生成
- 曲キーを利用した音程量子化
- MIDI / UltraStarデータ生成

主に「原曲から採点用譜面をどう生成するか」の参考にする。

### UltraStar Deluxe / Vocaluxe / Performous

既存譜面を使って歌唱するカラオケ / 音楽ゲーム系。

参考になる点:

- マイク入力
- ピッチ判定
- リズム判定
- スコアリング
- 複数プレイヤー
- カラオケUI
- 譜面データ形式

主にリアルタイム歌唱側の実装参考とする。

### OpenKara

ローカル音源をAIで分離してカラオケ化するプレイヤー系。

参考になる点:

- 曲ライブラリ
- stemキャッシュ
- Tauri / React / Rust
- AI生成物の保存方法

## 既存手法

### 音声 / 動画入出力

候補:

- FFmpeg

用途:

- WAV / MP3 / AAC / MP4読み込み
- 動画からの音声抽出
- 形式変換

### Stem Separation

候補:

- Demucs
- UVR 系
- ONNX化した分離モデル

用途:

- vocals
- accompaniment

### ASR

候補:

- Whisper large-v3 系
- faster-whisper
- その他の歌唱に強いASR

Whisper単独を正解源にしない。

歌唱ASRは通常発話より難しく、長音、ビブラート、崩した発音、息声などで精度が下がる。

### Forced Alignment

既知歌詞がある場合の主要手法。

候補:

- WhisperX
- CTC alignment
- wav2vec2 / MMS 系 aligner
- Qwen 系 forced aligner

「何を歌っているか」より「既知テキストをいつ歌ったか」に問題を縮小できる。

### 時系列アラインメント

候補:

- Dynamic Programming
- Needleman-Wunsch
- Viterbi
- CTC alignment

歌詞順序や時間単調性を制約として利用する。

### Contextual Biasing / Rescoring

曲全体から得た語彙、頻出フレーズ、既知歌詞、反復区間等を、再認識や候補再ランキングへ使う。

関連する既存概念:

- contextual biasing
- prompted ASR
- N-best rescoring
- shallow fusion
- language model rescoring
- retrieval augmented ASR

### Self-training / Test-time Adaptation

高信頼なpseudo-labelを利用して、その曲やドメインに再適応する研究方向。

本当にLoRAやadapterを更新する方法も候補だが、初期実装では優先しない。

### Pitch Detection

候補:

- CREPE
- pYIN
- YIN

リアルタイム歌唱とオフライン譜面解析の両方に利用可能。

### Note Transcription

候補:

- Basic Pitch
- 自前のF0 + onset + quantization

### 曲構造解析

利用したい情報:

- Verse
- Pre-Chorus
- Chorus
- Bridge
- Outro
- repeated sections

反復構造をASRおよび譜面推定の信頼度向上へ利用する。

### DSP / Audio Engine

必要機能:

- マイク入力
- 音楽再生
- mixer
- gain
- echo / reverb
- latency compensation

候補技術は実装言語とGUI / Audio Engine決定後に選定する。

## 技術的な実現性

現時点で、各機能単体について実現不可能と考えられるものはない。

最大の研究要素は以下。

1. 実歌唱のpitch contourから採点用の理想譜面を生成すること
2. 歌唱ASRの誤りを複数モデルと曲全体情報でどこまで自動訂正できるか
3. 歌詞、音程、音節、時間、反復構造を一貫したSong Global Optimizerで統合すること

既存技術を組み合わせつつ、統合ロジックが本プロジェクトの中心的な差別化要素になる。
