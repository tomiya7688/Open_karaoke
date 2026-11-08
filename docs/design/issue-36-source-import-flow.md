# #36 残り: 原曲取込から曲登録までの一括導線 設計

> 状態: Python側`analysis.song`（正規化済みWAV→SongBundle）とCore側`song.import`（完了Job→SongLibrary登録）は実装済み。
> **36.1 実装済み（Core Job）**: `kind: audio.import`（`source_path`）で元音源(WAV/AAC/MP3/MP4)をFFmpeg正規化し、private staging→1回のrenameで公開する（失敗・取消で部分出力を残さない）。コードは`core/src/audio_job.rs`、`audio_import.rs`。実機のFFmpeg・実曲での確認は未実施（Unverified）。
> **未接続**: 正規化→`analysis.song`→`song.import`の合成Job（36.2）、入力受け渡し方式（36.3）、GUIの原曲取込・進捗・登録操作（36.4）。最終確認日: 2026-11-08
> （[Current status](../current_status.md)、[SongStore import](../song_import.md)）。

## 目的
GUIから元音源を選ぶだけで、正規化→全曲解析→検査→SongLibrary登録まで進み、失敗・キャンセル時に未完成曲が完成扱いされない状態にする。

## 受入条件（[Issue #36](https://github.com/tomiya7688/Open_karaoke/issues/36) から転記・未達分を強調）
- GUIなしのCore Job APIから曲を登録し、SongStoreへ読み戻せる（登録APIは達成済み。**元音源からの入力は未達**）
- 失敗や途中キャンセルで未完成曲を完成扱いしない
- モデル未導入は明確なエラーで報告される
- パイプライン各段階の出力とモデルID/版が追跡可能
- テスト: mock/fixtureのE2E契約、入力hash・sample時刻・artifact依存の整合。実曲精度は別のRelease Evaluation

## 非目標
- 各解析アルゴリズムの精度改善（#6〜#11）、品質判定・再解析（#108）、モデル確定計画（#109）
- 曲一覧UIの完成形・歌唱導線（#16/#110）
- 任意のファイルパスをクライアントがSongStoreへ渡す経路（禁止。既存`song.import`の安全方針を維持）

## 設計
**責務の分割（現状の所有を維持）**
- Rust Core: 入力ファイルの受理・FFmpeg正規化（`audio_import.rs`）、Job状態、`song.import`の確定。
- Python worker: 正規化済みWAVを入力に`analysis.song`を実行（`song.py` `SongAdapter`）。入力に「正規化済みWAV必須」(`input_required`)。
- 合成Job: 新Job kind（案: `song.ingest`）をCoreが所有し、内部で`normalize`→`analysis.song`→`song.import`を順に実行する**オーケストレータ**。各段の子JobIDをartifactとして記録し、取消は全段へ伝播。**案であり未実装・名称は未決**。

**データ契約（案）**
- 入力: `song_ingest { source_token, title?, artist?, reference_lyrics? }`。クライアントは任意パスではなく、Core管理の取込領域へアップロード済みのファイルを指す`source_token`（または利用者が選んだファイルをCoreが読む安全な方式。方式は未決）を渡す。
- 出力: 段ごと`stage`(`normalizing|analyzing|importing`)、`progress`、`imported_song{song_id,reused}`、失敗時は段名つき`error_code`。
- 正規化artifactは48kHz stereo f32 WAV（[Audio import](../audio_import.md#internal-format)）。`duration_samples`は正規化後のsample数から取り、SongBundleと一致を検査。

**エラー**: FFmpeg無し/不正buildconf/破損入力/モデル未導入(`input_required`等)は段名つきの明示errorで`failed`。途中失敗・取消時は`song.import`を実行せず、SongLibraryに未完成曲を作らない（既存の確定境界を使う）。同一入力hashの再実行は既存の`reused`規則に従う。
**スレッド**: 重いFFmpeg/解析はJob内。再生中は#43のポリシーで待機。GUIは既存のJob取得/キャンセル（200ms snapshot）を再利用。

## 実装スライス
| # | 内容 | 依存 | 完了条件・テスト |
|---|---|---|---|
| 36.1 | 正規化Job化: 元音源→正規化WAVのCore Job（artifact参照で出力） | #4 | `core/tests/job_api.rs`系: 形式matrix fixture、`duration_samples`一致、破損入力のerror、取消 |
| 36.2 | 合成Job（正規化→`analysis.song`→`song.import`）の結線と取消伝播 | 36.1, 既存`song.import` | mock worker E2E契約: 失敗/取消で未登録、段別error、入力hash整合 |
| 36.3 | 入力受け渡し方式の決定と実装（`source_token`等）、サイズ/拡張子制限 | #41 | 任意パス拒否のnegative test、上限超過拒否 |
| 36.4 | GUI取込操作: ファイル選択→進捗→完了後に曲一覧へ反映、失敗表示 | 36.2, 36.3, #15 | `Client.Tests`のモックCore契約、`Gui.Smoke`で取込操作の描画 |
| 36.5 | 実曲での通し確認（非公開音源。CIに載せない） | 36.4, モデル導入 | 手順と結果を`Unverified`/実測として別記録。合成成功を精度証明にしない |

## 触るファイル
`core/src/audio_import.rs`、`core/src/lib.rs`（Job kind・`prepare_import`周辺）、`core/src/song_api.rs`・`song_library/`、`core/tests/job_api.rs`・`song_import.rs`、`python/src/open_karaoke_analysis/song.py`（入力契約は原則不変）、`gui/OpenKaraoke.Client/`・`gui/OpenKaraoke.Gui/`、`docs/song_import.md`・`current_status.md`。`core/src/lib.rs`/`ShellViewModel`は競合しやすい。

## 守る規則
- [宣言コメント必須](../coding_rules.md#5-ソースコードコメント)、[境界とエラー](../coding_rules.md#3-境界とエラー)
- 48kHz整数sample indexを全段で一貫（[architecture](../architecture.md#オーディオ内部仕様)）
- FFmpegはLGPL構成のみ受理（[Licensing](../licensing.md#ffmpeg-and-codecs)）。ユーザー楽曲の権利は別（[Licensing](../licensing.md#music-lyrics-and-user-content)）
- 「解析Job完了」「登録完了」「歌える」は別状態（[Current status](../current_status.md)）。実モデル未導入はmock成功で代替しない

## 未決事項
- Job kind名と、単一合成Jobか既存Jobのクライアント連鎖か（取消・再接続の簡潔さで決定）
- 元音源の受け渡し方式（アップロード/Coreが選択ファイルを読む）と一時領域の掃除
- 同一入力hashの再取込で新しい解析Jobが作られる場合の再利用規則（現行`verify_reuse`は受領記録の解析Job IDに依存する疑い。36.2で既存の`reused`挙動を確認し、重複登録しない方式を決める）
- 参照歌詞入力の画面側仕様
