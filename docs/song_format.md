# Song Data Format

## 方針

Open_karaoke内部ではJSONベースの独自Song Data Formatを使用する。

外部形式をそのまま正本にはせず、MIDIや既存カラオケ形式はImport / Exportで内部形式へ変換する。

時間の正本は48kHz基準の整数sample indexとする。

## ディレクトリ例

```text
songs/
  <song-id>/
    song.json
    original/
      source.mp3
    audio/
      original_48k.wav
      vocals.wav
      accompaniment.wav
    analysis/
      lyrics.json
      notes.json
      evidence.json
    cache/
```

## song.json

曲全体のメタデータと生成物への参照を持つ。

例:

```json
{
  "format_version": 1,
  "song_id": "example-id",
  "title": "Example Song",
  "artist": "Example Artist",
  "sample_rate": 48000,
  "duration_samples": 9600000,
  "analysis_version": 1,
  "files": {
    "original": "audio/original_48k.wav",
    "vocals": "audio/vocals.wav",
    "accompaniment": "audio/accompaniment.wav",
    "lyrics": "analysis/lyrics.json",
    "notes": "analysis/notes.json"
  }
}
```

## lyrics.json

歌詞本文とタイミングを保持する。

初期段階では行・単語・音節の粒度を将来拡張できるようにする。

例:

```json
{
  "format_version": 1,
  "segments": [
    {
      "id": "lyric-0001",
      "text": "君の声が",
      "start_sample": 590400,
      "end_sample": 686400,
      "text_confidence": 0.94,
      "timing_confidence": 0.88,
      "source": "whisper"
    }
  ]
}
```

将来は以下を追加可能にする。

- words
- syllables
- phonemes
- reference lyric mapping
- ASR candidates
- evidence

## notes.json

採点用の正解ノート列を保持する。

例:

```json
{
  "format_version": 1,
  "notes": [
    {
      "id": "note-0001",
      "start_sample": 590400,
      "end_sample": 638400,
      "midi_note": 64,
      "pitch_confidence": 0.93,
      "lyric_segment_id": "lyric-0001"
    }
  ]
}
```

実歌唱のpitch contourをそのまま保存するデータと、採点用に正規化したnoteは分離して扱える設計にする。

## evidence.json

初期E2Eでは必須ではない。

将来の複数モデル合議用に、各モデルの観測結果や信頼度を保持できるよう予約する。

例:

```json
{
  "format_version": 1,
  "items": [
    {
      "target_id": "lyric-0001",
      "evidence": {
        "whisper": {
          "text": "君の声が",
          "confidence": 0.91
        },
        "reference_lyrics": null
      }
    }
  ]
}
```

## バージョニング

最低限以下を分離する。

- format_version
- analysis_version
- model version

モデル更新や解析ロジック変更で結果を再生成できるようにする。

既存の曲データが新バージョンでも読めるよう、schema migrationを前提とする。

## 再解析

解析単位は将来的に部分更新できるようにする。

例:

- lyricsのみ再生成
- alignmentのみ再生成
- notesのみ再生成
- stemのみ再生成
- 全体再解析

依存関係を追跡し、必要な下流データだけをinvalidateできる設計を目標とする。
