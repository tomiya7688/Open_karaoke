# Song Data Format

## 方針

Open_karaoke内部ではJSONベースの独自Song Data Formatを使用する。

外部形式をそのまま正本にはせず、MIDIや既存カラオケ形式はImport / Exportで内部形式へ変換する。

時間の正本は48kHz基準の整数sample indexとする。Rust実装では `u64` を使用し、浮動小数秒は保存しない。

## ディレクトリ

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

主要フィールド:

- `format_version`
- `song_id`
- `title`
- `artist`
- `sample_rate`（常に48000）
- `duration_samples`
- `analysis_version`
- `files`
- `artifacts`

`artifacts` は部分再解析のための状態を保持する。

```json
{
  "lyrics": {
    "generation": 2,
    "valid": true,
    "depends_on": ["vocals"],
    "analysis_version": 4
  },
  "notes": {
    "generation": 3,
    "valid": true,
    "depends_on": ["lyrics"],
    "analysis_version": 4
  }
}
```

上流artifactをinvalidateすると依存する下流artifactへ再帰的にinvalidateを伝播できる。再生成したartifactだけgenerationを進めるため、lyrics/notes等を独立して再生成できる。

## lyrics.json

歌詞本文とタイミングを保持する。各segmentは整数の `start_sample` / `end_sample` を持つ。

初期実装では以下を検証する。

- `start_sample < end_sample`
- 曲の `duration_samples` を超えない
- confidenceは0..1
- IDが空ではない

将来はwords / syllables / phonemes / reference mapping / ASR candidatesを追加できる。

## notes.json

採点用の正規化ノート列を保持する。

- 整数sample index
- MIDI note 0..127
- pitch confidence
- 任意のlyric segment参照

lyric segment参照は保存時・読込時に整合性を検証する。

## evidence.json

複数モデルの観測値や信頼度を保存するための拡張領域。任意のモデル固有JSONを `evidence` map配下に保持できる。

## Persistence

Rust Coreの `SongStore` が4文書を保存・読込する。

保存前・読込後にvalidationを実行する。JSONはpretty-print + LFで決定的に出力する。

書込時は一時ファイルを作成後に置換し、中途半端なJSONを書き込みにくい構造にする。

## バージョニング / migration

現在の `CURRENT_FORMAT_VERSION` は1。

読込時はJSONを一度 `serde_json::Value` として読み、`migrate_document` を経由してから型へdeserializeする。

初期migration hookではversion未指定（v0扱い）の文書へv1必須フィールドを補完する。未知の将来versionは黙って読まずエラーとする。

モデル更新や解析ロジック更新は `analysis_version` とartifact単位のversion/generationで追跡する。
