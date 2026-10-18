# Full-song analysis — Issue #36

## 現在の範囲

`song-pipeline-v1` は、正規化済み48kHz・stereo・float32 WAV/WAVEXから6解析Adapterを
1つの `analysis.song` Jobとして実行する。非空、最大30分。非有限サンプル・形式違いを拒否する。
処理順は Stem分離 → Whisper歌詞候補 → Alignment → F0 → Vocal Event → 採点用Notes。
同じworker内でAdapterを呼び、子Jobをenqueueして待たない。モデル欠落時にmockへ切り替えない。

結果の出力後は、別の **`song.import` Core Jobで音声込みSongStore登録**ができる。
[検査付きSongStore登録](song_import.md)にAPI・一括公開・再登録・キャンセルの契約がある。
`analysis.song` の完了だけでは自動登録しない。元ファイルのFFmpeg正規化をJobへ組み込む処理と
GUIの原曲取込・曲一覧操作は未接続。Issue #36はこれらと実曲評価を含めて継続する。

## Core APIから呼ぶ

[Desktop session](desktop_session.md)で起動したCoreに認証付きで `POST /jobs` を送る。
Pythonの非公開ポートへGUIから直接接続しない。

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

入力はCore artifact root配下の相対パス。titleは必須、referenceは任意のUTF-8テキスト64KiBまで。
models / stage_optionsのキーは `stems`, `lyrics`, `alignment`, `pitch`, `events`, `notes`。
省略したモデルは各roleの既定Adapterを使う。各段階の `*_artifact` とanalysis_versionは
パイプライン所有のため上書き禁止。その他のオプションは各Adapterのvalidationに従う。

Adapter登録とruntime・重み導入は別。依存や重みがなければ段階名付きerrorで失敗し、
架空の結果を返さない。導入は [Stem](stem_separation.md)、[Lyrics](lyric_transcription.md)、
[Alignment](lyric_alignment.md)、[Pitch](pitch_analysis.md) を参照する。

## 失敗・キャンセルと時刻整合

Job用領域へ元WAVと任意の参考歌詞をsnapshot保存する。各段階は固有のstages/role配下へ
結果を作り、同じvocals音声を共有する。音声長・48kHz・input SHA-256・canonical IDと参照・span・
analysis_versionを照合する。前段成果物のhashを全段終了後に再検査し、後段の書換えを検出する。

進捗stageは `song.stems.*` 等の段階名を含み、成功前はprogressが1未満。
キャンセルは既存Adapterのcheckpointへ伝播し、失敗・キャンセルではJobManagerが
そのJob領域を掃除してからterminal状態を公開する。artifact一覧は空になる。
有効な既存Stem cacheを破棄せず、登録済み曲を変更しない。

## 出力と登録

artifactsの最後に `song-result.json` があり、format_version、Rust互換bundle、assetsを含む。
bundleはsong / lyrics / notes / evidence。各assetはsource、destination、SHA-256、byte数。
参考歌詞・ASR原文・モデル結果・境界evidenceをprovenanceとして保持し、モデルID/版・確定オプション・
worker側参照と曲側予定参照を記録する。

destinationとbundle.song.filesは、解析時点ではコピー予定パス。
完了したCore解析Job IDを `song.import` へ渡し、登録Jobのcompletedとimported_songを確認して
初めて登録済みとする。登録済み文書は `/songs/{song_id}` で取得できる。

analysis_fingerprintは元入力・参考歌詞・モデル/版・確定オプション由来でJob IDに依存しない。
これは再解析の追跡用であり、全曲cacheや原曲単位の自動重複排除ではない。
singing_accuracy_verified:falseは、合成回帰を実歌唱精度と区別する明示。

## F0観測窓と音符区間

譜面生成1.0.0には、Pitchの1920-sample重複観測窓（480-sample hop）を非重複区間と取り違えて
拒否する結合不具合があった。1.0.1は宣言窓長・全frame数・start/end/centerの式を検査し、
元のPitch/Event観測を書き換えない。

Vocal Eventの出力frameは480-sample非重複区間で、window_samplesは音響分析用の窓。
PitchとEventはhash・duration・rate・hop・開始位置を共有するが、end/centerの意味は異なる。
各frameをそれぞれの時間契約で検証する。

音符生成内部のみ、隣接Pitch観測centerの整数中点から非重複区間を作る。
端は曲の0/durationへ接し、各centerは自区間内にあり、隙間・時間の二重計上がない。
旧480-sample fixtureはそのまま使い、宣言窓長だけ実際の区間に合わせる。
音程・区間・正解ラベル・metric式・gateは変えず、方式をpitch_frame_policyに記録する。
これは時刻表現の整合であり、実歌唱境界精度の証明ではない。

## 検証

```text
python -m pytest python/tests/test_song.py python/tests/test_service.py python/tests/test_note_window_contract.py -q
cargo test --locked --test song_bundle_contract -- --ignored
cargo test --locked --test song_import -- --ignored
```

Full-song regressionはfailure/cancel・不整合・モデル欠落と、実Pitch/Events/Notesによる合成トーン処理を
検査する。song_bundle_contractは文書互換の往復だけ、song_importは音声込みの本番登録処理を検査する。
どちらの結合fixtureも上流3段はテスト用であり、全モデル実歌唱評価を完了したという意味ではない。

次は [Audio Import](audio_import.md) のJob接続、GUI取込導線、実曲Release Evaluation。
登録の永続化・破損検出範囲・原子的公開の限界は [SongStore import](song_import.md) に定義する。
