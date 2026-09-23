# Model Plugin Design

## 目的

初期実装は固定モデルで開始するが、将来的には開発者でなくてもモデルを追加・切り替えできる構造にする。

## モデルロール

初期想定:

- asr
- stem_separator
- aligner
- pitch_detector
- note_transcriber
- validator
- optimizer

各ロールは共通インターフェースを持つ。

## 基本方針

アプリ本体はモデル固有のPythonコードや推論APIを直接知らない。

AI Analysis Serviceが各モデルをロードし、共通REST APIへ正規化する。

```text
App
 |
REST API
 |
Analysis Service
 |
Model Adapter
 +-- Whisper
 +-- Stem Separator
 +-- WhisperX
 +-- Future Model
 +-- Internal Model
```

## manifest

モデルパッケージはmanifestを持つ。

ライセンス情報は必須項目とする。

初期例:

```json
{
  "manifest_version": 1,
  "id": "whisper-large-v3",
  "name": "Whisper Large V3",
  "type": "asr",
  "backend": "whisper",
  "version": "1",
  "languages": ["ja", "en", "multi"],
  "license": "MIT",
  "license_url": "https://example.invalid/license",
  "commercial_use": true,
  "redistribution": true,
  "capabilities": {
    "timestamps": true,
    "word_timestamps": true,
    "confidence": true
  }
}
```

公式配布・公式Model Registryへ登録するモデルは、コードだけでなく学習済み重みの利用条件も確認済みであることを要求する。

以下は公式配布対象外とする。

- commercial_use != true
- redistribution条件を満たせない
- license不明
- モデル重みのライセンス不明
- Non-Commercial条項を含む

## 将来の追加方法

候補:

1. 所定のmodelsフォルダへモデルパッケージを配置
2. GUIからZIP / packageをImport
3. 信頼できるModel RegistryからInstall

初期実装では 1 を最も単純な方式として想定し、2・3は後から追加する。

## 内製モデル

将来的に日本語歌唱向け内製モデルを合議メンバーとして追加できるようにする。

初期段階ではWhisper置換を目的とせず、Whisperと異なる誤り傾向を持つ補助ASR / CTCモデル等を想定する。
