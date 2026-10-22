# アーキテクチャ

> **この文書は製品の目標構成・決定済みの技術方針です。** 現在の実装状況は
> [現在の実装状況](current_status.md) を参照してください。CoreにAPI基盤はありますが、
> CPAL音声エンジン、リアルタイム採点、GUIのCore連携が完成したという意味ではありません。

## 設計原則

堅牢性を優先し、コンポーネント境界で契約、入力、状態遷移、失敗を明示する。変更は責務を保てる小さな単位に分け、境界の振る舞いをテスト可能にする。具体的な必須・推奨事項とレビュー項目は[コーディングルール](coding_rules.md)に定める。

オブジェクト設計はSmalltalk型のmessage-orientedな考え方を指針とする。オブジェクトは外部へ小さく明確な操作（message）を公開し、自身の状態と不変条件を管理する。interface継承やアクセス制御は契約や安全性を高める場合に使ってよい。特定の構文やパターンを機械的に適用しない。

[Message Oriented Design](https://github.com/tomiya7688/Message-Orientated-Design)は作成途中の設計資料であるため、このプロジェクトでは上記の指針を先に適用し、未完成の詳細規則への完全準拠を要求しない。ここでのmessageはオブジェクト間の明示的な操作契約を指し、メッセージブローカーや非同期キューの導入を意味しない。

責務は論理的に`UI` / `Process` / `Data`へ分け、層を飛び越えた直接依存を避ける。この三分類を実行プロセスやパッケージへ機械的に一対一対応させない。詳細は[コーディングルール](coding_rules.md)を参照する。

[UPD Commander Base Design](https://github.com/tomiya7688/upd-commander-base-design)のUI / Process / Data分離を参照するが、Commander / Messengerのクラス構造や呼出し手順は採用要件にしない。各境界は既存のAPI、型付きデータ契約、必要なインターフェースで表現する。

## 対象OS

初期対象は Windows とする。

将来的な他OS対応を妨げない構成にはするが、初期実装では Windows での安定動作を優先する。

## 配布方針

ユーザーに Python、FFmpeg、AIモデル実行環境などの個別セットアップを要求しない。

アプリ本体、必要なランタイム、補助ツール、既定モデル実行環境は可能な限りアプリ側で同梱または自動管理する。

採用コンポーネントは商用利用可能であることを必須条件とし、コード・重み・バイナリ・依存関係ごとにライセンスを確認する。

## 正式採用する技術スタック

### Rust Core

Rustをアプリケーションのコア実装言語とする。

主な責務:

- Audio Engine
- CPALによるAudio I/O
- WASAPI対応
- 再生クロック
- マイク入力
- Mixer
- Echo / Reverb
- リアルタイムDSP
- リアルタイムPitch Detection
- Scoring
- Song Data管理
- キャッシュ管理
- Device Management
- Job Management
- Python Analysis Serviceの起動・監視
- GUI向けAPI

リアルタイム音声処理・採点・デバイス制御等の製品中核をRust側へ集約する。

### C# / Avalonia GUI

GUIはC# + Avaloniaを第一採用とする。

GUIは表示と操作に責務を限定し、Audio I/O、AI推論、曲データの正本管理を直接行わない。

主な責務:

- 曲一覧 / 検索
- カラオケ再生画面
- 歌詞表示
- 音程バー
- 採点結果
- 設定
- マイク / オーディオデバイス設定
- モデル管理
- 解析進捗
- 将来の譜面・歌詞編集UI

GUIはRust CoreのAPIのみを利用する。

GUIはコアから分離し、必要に応じて将来別GUIへ差し替え可能な構造を維持する。

### Python Analysis Service

AI・オフライン解析はPythonを利用する。

主な責務:

- Stem Separation
- Whisper ASR
- Forced Alignment
- F0 / Pitch Analysis
- Note Transcription
- Vocal Event / Boundary Detection
- Song Structure Analysis
- 高精度Score Generation
- 将来のOrchestrator
- 将来のTransformer Validator
- 将来の内製歌唱モデル

Pythonはリアルタイム再生経路には置かず、曲生成・再解析等のオフライン処理を担当する。

## 全体構成（目標）

```text
+---------------------------+
| C# / Avalonia GUI         |
| Presentation / Operation  |
+-------------+-------------+
              |
              | Local API
              v
+---------------------------+
| Rust Core                 |
|                           |
| Audio / CPAL / WASAPI     |
| Mixer / DSP               |
| Realtime Scoring          |
| Song Library              |
| Device Management         |
| Job Management            |
+-------------+-------------+
              |
              | REST API
              v
+---------------------------+
| Python Analysis Service   |
|                           |
| Whisper                   |
| Stem Separation           |
| Alignment                 |
| F0 Ensemble               |
| Boundary Fusion           |
| Score Generation          |
+---------------------------+
```

## 通信方式

コンポーネント間通信は通常のローカルAPIを基本とする。

Rust Core と Python Analysis Service は localhost REST API で通信する。

GUI と Rust Core も安定したAPI境界を持ち、GUIからコア内部実装へ直接依存しない。

解析はJobとして扱い、テスト・Evaluator・CIからGUIを介さず実行できるようにする。

現在の **Rust Core公開API**:

```text
GET    /health
GET    /analysis/health
GET    /models
POST   /jobs
GET    /jobs/{job_id}
POST   /jobs/{job_id}/cancel
GET    /jobs/{job_id}/events
```

解析の起動は `POST /jobs` の `kind: "analysis.<role>"` を使用します。
`POST /analysis/{role}` はPython子プロセス内部の認証付きAPIであり、Coreの公開ルートではありません。
`GET /devices` や GUI向け再生APIは**将来実装**の対象です。正確な実装仕様は
[Core API](core_api.md) と [Analysis Service](analysis_service.md) に記載します。

Job状態の最低限:

- queued
- running
- completed
- failed
- cancelled

Jobには進捗、処理段階、開始/終了時刻、エラー、生成物参照を持たせる。

進捗通知や再生中の状態通知にはSSEを第一候補とする。双方向性が必要な用途のみWebSocketを検討する。

## オーディオ内部仕様

時刻同期、capture/output clock、遅延校正、drift、seek時のepoch切り替えは
[Audio clock / latency contract](audio_clock.md) に定義する（#40の純粋変換契約・合成テスト）。
**CPAL/WASAPIを用いた実機タイムスタンプ取得・物理遅延計測はまだ未実装**であり、
#12と#14の音声エンジン・採点機能へ接続する必要がある。

すべての入力音源は解析・再生用内部フォーマットへ正規化する。

初期仕様:

- Sample rate: 48,000 Hz
- 内部タイムライン: 整数
- 基準時刻: sample index
- DSP向けPCM: float32を基本とする

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

標準経路はWASAPIを優先する。ASIOは商用配布条件と必要性を確認したうえで追加候補とする。

## モデル拡張性

初期実装は以下の系統でE2Eを通す。

- Stem Separation: 商用利用条件を確認した高精度モデル
- ASR: Whisper large-v3系
- Alignment: WhisperX系
- Pitch: 複数のF0 detectorを切替・併用可能にする
- Note transcription: Basic Pitchを候補生成器として含む専用統合パイプライン

モデルはコードへ固定埋め込みせず、役割単位で差し替え可能にする。

## F0 / Pitch Detection 方針

F0検出器は1種類に固定しない。

最低でも以下を満たす。

- detector単位で切替可能
- 複数detectorの同時実行が可能
- 各detectorのconfidenceを保持
- 同一時刻のpitch候補を統合可能
- detectorごとの失敗・octave errorを他detectorで補完可能
- Evaluatorでモデル単体とensemble双方を比較可能

候補としてCREPE系、pYIN、YIN系などを比較する。

統合器は単純平均に固定せず、confidence、voiced probability、近傍時間との連続性、octave consistency等を用いて最終F0候補を決定できる構造にする。

## 譜面生成方針

譜面生成は製品のコア機能として最初から精度重視で設計する。現行のベースラインはF0・Vocal Eventと候補生成・時系列DPの組み合わせであり、実録歌唱精度を検証済みという意味ではない（[Score Generation](score_generation.md)）。

単一モデルの出力を正式譜面とせず、以下を統合する。

- F0 Ensemble
- Vocal Event / Boundary Detection
- onset
- voiced / unvoiced
- spectral / energy features
- phoneme / syllable evidence
- Whisper / forced alignment
- Basic Pitch等のnote transcription
- key / scale estimation
- repeated-section consistency
- whole-song temporal optimization

実歌唱のビブラート、しゃくり、こぶし、ポルタメント等をそのまま別音符へ変換せず、採点用の理想ノート列へ正規化する。

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

複数ASR、内製モデル、Transformer破綻検出、曲全体を複数回読み直す
高度な Song Global Optimizer、曲単位LoRA / Adapterは、E2E完成後の精度改善候補とする。
**譜面候補間の時系列DPは現行ベースラインに既に存在**し、ここで言う将来の全曲最適化とは別である。
