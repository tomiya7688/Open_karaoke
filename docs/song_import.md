# Verified SongStore import — Issue #36

## 解析結果の出力と曲登録は別操作

[全曲解析](full_song_analysis.md)の `analysis.song` はJob領域に結果を出力する。
今回の `song.import` は、その**完了済みCore Job ID**を指定して、音声・文書・根拠データを
検査し、`OPEN_KARAOKE_DATA_DIR/songs/<song_id>/` へ一括登録する。
クライアントは任意のファイルパス、登録先、差し替えたSongBundleを送れない。

元音源のFFmpeg正規化をJobにつなぐ処理、GUIの原曲取込・曲一覧操作は未接続。
本APIの追加だけで「原曲から歌えるアプリ」が完成したことにはしない。
Issue #36は残作業と実曲評価を含めて継続する。

## API

[Coreセッション認証](desktop_session.md)とAPI v1ヘッダーを使用する。
`analysis_job_id` はPythonのprivate Job IDではなく、Coreが返した `analysis.song` のID。

```json
{
  "kind": "song.import",
  "song_import": {
    "analysis_job_id": "11111111-1111-4111-8111-111111111111"
  }
}
```

`POST /jobs` は202と通常のqueued Jobを返す。既存のJob取得・SSE・cancel APIを使える。
コピー・検証・確定の進捗は `song_import.*`。成功時には次のフィールドが付く。

```json
{
  "kind": "song.import",
  "status": "completed",
  "progress": 1.0,
  "artifacts": [],
  "imported_song": {
    "song_id": "22222222-2222-4222-8222-222222222222",
    "reused": false
  }
}
```

`imported_song` は成功した登録Jobだけに存在する。既存クライアントとのJSON互換を保つため、
他Jobでは省略する。`artifacts` にライブラリパスやAPI URLを混ぜない。

`GET /songs?limit=50&after=<song_id>` は `{ "songs": [...], "next_after": null }`。
各行はsong_id・title・artist・duration_samples・error。UUID昇順のcursor方式で、limitは1〜100。
破損した登録曲はerror付きの行として報告し、正常な曲に見せかけたり黙って隠したりしない。
`GET /songs/{song_id}` は検査済みの `SongBundle`（song / lyrics / notes / evidence文書）を返す。
音声ファイルをHTTP配信するエンドポイントではない。

元Jobがない場合は404、未完了または全曲解析でない場合は409。任意の追加入力フィールドは拒否する。
ライブラリ未設定・Core終了中は503、同時I/O枠不足は429。登録開始後に判明したコピー・検証失敗は
Jobの `failed` と安全なerror code/messageで公開する。

## 検査するもの

公開済みJob artifact集合と `song-result.json` を照合し、別Jobのファイルを参照させない。
sourceとdestinationは管理下の相対パスに限定する。親パス、絶対パス、Windows ADS/予約名、
symlink/reparse point、destinationの大小文字衝突、文書を上書きするassetを拒否する。

全assetを64KiBの再利用バッファでコピーし、実byte数とSHA-256を確認する。3つの本番音声は
48kHz・stereo・float32 WAV/WAVEX、曲のdurationとの一致、全サンプルが有限であることを確認する。
元音源・vocalsのhash、6段階のモデル来歴、ID・歌詞参照・analysis_version・依存関係も検査する。
canonical lyrics/notesは検査したAlignment/Notesのstage文書と一致しなければ登録しない。

上限はexport JSON 16MiB、asset256件、各音声は30分相当、コピー合計8GiB。
ライブラリAPIのI/Oは共有4枠。一覧は最大10,000ディレクトリエントリを走査する。
これは資源上限であり、上限いっぱいの音源を実モデルで性能検証したという意味ではない。

## 公開・再登録・キャンセル

ライブラリと同じファイルシステムの `.staging-<random UUID>` に全ファイルを用意し、
検証・SongStore文書保存・receipt保存後、1回のディレクトリrenameで公開する。
その前には曲一覧へ出ない。OSファイルロックで、信頼された複数Coreの書き込みを直列化する。
ロックが使用中なら待ち続けずbusyになる。

キャンセル要求と登録確定には1つのatomicな順序判定がある。確定前に受理したキャンセルは
stagingを掃除してcancelled。確定を開始した後のキャンセルは受理せず、completedまたはfailedになる。
キャンセル返答がrunningでも直ちにcancelledと表示せず、terminal snapshotを確認する。
Core終了時も新規Job受付を止め、未確定の登録へキャンセルを要求する。

同じCore解析Jobと同じexportの再登録は、保存先の全assetのchecksumを再確認して `reused: true`。
既存曲が異なる・壊れている場合は上書きしない。同じ原曲を別解析Jobで再解析した場合は別song_idであり、
fingerprintによる曲単位の自動重複排除や既存曲の更新はまだ行わない。

## 永続化の範囲

登録済みの曲はCore再起動後も読め、元のworker artifactがなくても音声込みで自己完結する。
Job履歴は従来通りメモリ内のみ。再起動・履歴破棄後は古い解析Job IDから新規importを起こせないが、
すでに登録した曲の一覧・取得には影響しない。

通常の一覧・詳細取得はreceiptと文書hash、音声の存在・byte数を検査する。毎回全音声のSHA-256を
走査するわけではない。全assetのchecksum検証は初回登録と同一結果の再登録時に行う。

ディレクトリの一括公開は**突然の停電に対する全ファイルシステム共通の耐久性保証ではない**。
プロセス強制終了・I/O障害では未参照stagingが残り得るが一覧からは隠れる。GCは後続。
同じOS権限でファイルを並行改変する悪意あるプロセスからの隔離や、クライアント別ACLは未実装。
登録済み曲はこのAPIから編集・削除しない。

## 実装と検証

`core/src/song_library/` が検査・コピー・公開、`core/src/song_api.rs` がJob/HTTP配線。
WAV検査は既存 `audio_wav.rs` のparserを共有する。全処理はblocking worker上で実行し、
音声callbackにファイルI/Oやhash処理を入れない。

```text
cargo test --locked --test song_import
cargo test --locked -p open-karaoke-core song_api::tests
cargo test --locked --test song_import -- --ignored
```

最後のコマンドはPython dev依存が必要で、専用CIから明示実行する。
合成fixtureの失敗・キャンセル・再登録・破損・一覧・再起動を検査し、Pythonの実パイプライン出力を
Rustで音声込み登録する結合試験も行う。上流3段はテスト用、Pitch/Event/Notesは実アルゴリズム。
これはストレージとAPIの検証であり、実歌唱精度・実スピーカー・GUI取込の検証ではない。
