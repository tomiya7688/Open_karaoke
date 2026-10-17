# Full-song analysis — Issue #36

## 現在の範囲

`song-pipeline-v1` は、**正規化済みの48 kHz・stereo・float32 WAV**から
既存の6解析Adapterを1つの `analysis.song` Jobとして実行する。
WAV/WAVEX、非空、最大30分。NaN/Infinity、サンプルレート・チャンネル・形式違いを拒否する。

処理順: Stem分離 → Whisper歌詞候補 → Alignment → F0 → Vocal Event → 採点用Notes。
同じworker内でAdapterを直接呼ぶ。1並列workerへ子Jobをenqueueして待つことはしない。
モデルIDを指定できるが、存在しないモデルをmockへ置き換えることはない。

**未接続:** 原ファイルのFFmpeg正規化をこのJobへ組み込む処理、音声assetの検査付きコピーと
SongStoreへのatomic commitを行う製品API、GUIからの楽曲取込。
既存の [Audio Import](audio_import.md) と [SongStore](song_format.md) は後続で接続する。
`analysis.song` が完了しただけで曲ライブラリへ登録されるわけではない。Issue #36は継続。

## Core APIから呼ぶ

[Desktop session](desktop_session.md)で起動したCoreに認証付きで要求する。
下は `POST /jobs` のJSON。Pythonの非公開ポートへGUIから直接要求しない。

```json
{
  "kind": "analysis.song",
  "analysis": {
    "model_id": "song-pipeline-v1",
    "input_artifact": "inputs/original_48k.wav",
    "options": {
      "title": "曲名",
      "artist": "歌手名",
      "analysis_version": 1,
      "reference_lyrics_artifact": "inputs/reference.txt",
      "models": {},
      "stage_options": {}
    }
  }
}
```

入力はCoreのartifact root配下の相対パス。referenceは任意のUTF-8テキスト（64 KiBまで）。
`title` は必須。`models` / `stage_options` のキーは `stems`, `lyrics`, `alignment`, `pitch`,
`events`, `notes`。省略したモデルは既存roleの既定Adapterを使う。
各段階の `*_artifact` と `analysis_version` はパイプライン所有のためstage_optionsで上書きできない。
その他のオプションは各Adapterの既存validationに従う。

Adapter登録と実モデルのruntime・重みの導入は別。依存や重みがなければ段階名付きerrorとなり、
架空の解析結果で成功しない。導入は [Stem](stem_separation.md)、[Lyrics](lyric_transcription.md)、
[Alignment](lyric_alignment.md)、[Pitch](pitch_analysis.md) の手順を参照。新たな重みや依存は同梱しない。

## 失敗・キャンセルと時刻整合

Job用領域へ元WAVと任意の参考歌詞のスナップショットを保存する。
各段階は固有の `stages/<role>/` 配下だけへ結果を作り、同じvocals音声を共有する。
音声長、48kHz、input SHA-256、canonical ID・参照・span・analysis_versionを照合する。
前段成果物は生成直後と全段終了後にhashを比較し、後段による書き換えを検出する。

進捗stageは `song.stems.*` など段階名を含む。成功前のprogressは1未満。
キャンセルは既存Adapterのcheckpointへ伝播する。失敗・キャンセルではJobManagerが
そのJob用領域を掃除してからterminal状態を公開し、artifact一覧は空になる。
有効な既存Stem cacheを破棄することはない。完成済みSongStoreを編集する処理は呼ばない。

## 出力と登録の境界

`artifacts` の最後に `song-result.json` がある。内容:

- `format_version: 1`
- `bundle`: Rust `SongBundle`に対応したsong / lyrics / notes / evidence文書。
- `assets`: Job内の `source`、将来の曲ディレクトリ内 `destination`、SHA-256、byte数。

`destination` と `bundle.song.files` は**登録先として予定するパス**であり、すでにコピーした
曲ファイルではない。このJSONを作っただけでSongStore登録済みとはみなさない。
参考歌詞、各モデル出力・ASR原文・境界evidence等はprovenance assetとして保持する。
モデルID/版・確定オプション・worker側参照と曲側予定参照を保存する。

`analysis_fingerprint` は元入力・参考歌詞・モデル/版・確定オプションから決まり、Job IDの違いには
依存しない。これは再解析追跡用であり、全曲結果cacheを実装したという意味ではない。
`singing_accuracy_verified: false` は合成回帰を実歌唱精度と混同しないための明示。

## F0観測窓と音符区間

結合テストにより、譜面生成1.0.0がPitchの1920-sample観測窓（480-sample hopで重複）を
非重複区間と取り違えて拒否していたことを検出した。譜面生成1.0.1は宣言された窓長、全frame数、
start/end/centerの式を検査し、元のPitch/Event観測は変更しない。

音符生成の内部だけ、隣接観測centerの整数中点を境界に区間を分割する。
最初と最後は曲の0/durationに接する。各centerはその区間内にあり、隙間・重複・時間の二重計上がない。
旧480-sample非重複fixtureはそのまま使う。`pitch_frame_policy`で方式を記録する。
この変換は時間表現の整合であり、歌詞/音符境界の実歌唱精度を証明しない。

## 検証と次の実装

```text
python -m pytest python/tests/test_song.py python/tests/test_service.py python/tests/test_note_window_contract.py -q
cargo test --locked --test song_bundle_contract -- --ignored
```

専用CIはWindows/Linuxで、段階failure/cancel・不整合・モデル欠落・実際のPitch/Events/Notesによる
合成トーン処理と、Python結果→Rust SongBundle validation→SongStoreの文書往復を検証する。
後者はmetadataだけのテストであり、assetを曲へコピーする本番登録APIではない。

続きは、Coreの正規化Job接続、全assetと文書の検証付きstaging/atomic登録、GUI取込導線、
実曲のRelease Evaluation。未処理/途中キャンセルを完成曲にしない境界を引き続き維持する。
