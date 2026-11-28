# #36 残り: 原曲取込から曲登録までの一括導線 設計

> 状態: Python側`analysis.song`（正規化済みWAV→SongBundle）とCore側`song.import`（完了Job→SongLibrary登録）は実装済み。
> **36.1 実装済み（Core Job）**: `kind: audio.import`（`source_path`）で元音源(WAV/AAC/MP3/MP4)をFFmpeg正規化し、private staging→1回のrenameで公開する（失敗・取消で部分出力を残さない）。30分超は`audio_too_long`で拒否し、#43の重いJobとして再生中は待機する。コードは`core/src/audio_job.rs`、`audio_import.rs`。実機のFFmpeg・実曲での確認は未実施（Unverified）。
> **36.2 決定（2026-11-28）**: 合成Job（`song.ingest`案）は作らず、クライアント連鎖（`audio.import`→`analysis.song`→`song.import`）を採用する。理由と契約は「連鎖方式の決定」。
> **未接続**: 入力受け渡し方式（36.3）、GUIの原曲取込・進捗・登録操作と連鎖の組立て（36.4）。最終確認日: 2026-11-28
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
- 合成Job（`song.ingest`案）は**採用しない**（下記「連鎖方式の決定」）。

**データ契約（案）**
- 入力: `song_ingest { source_token, title?, artist?, reference_lyrics? }`。クライアントは任意パスではなく、Core管理の取込領域へアップロード済みのファイルを指す`source_token`（または利用者が選んだファイルをCoreが読む安全な方式。方式は未決）を渡す。
- 出力: 段ごと`stage`(`normalizing|analyzing|importing`)、`progress`、`imported_song{song_id,reused}`、失敗時は段名つき`error_code`。
- 正規化artifactは48kHz stereo f32 WAV（[Audio import](../audio_import.md#internal-format)）。`duration_samples`は正規化後のsample数から取り、SongBundleと一致を検査。

**エラー**: FFmpeg無し/不正buildconf/破損入力/モデル未導入(`input_required`等)は段名つきの明示errorで`failed`。途中失敗・取消時は`song.import`を実行せず、SongLibraryに未完成曲を作らない（既存の確定境界を使う）。同一入力hashの再実行は既存の`reused`規則に従う。
**スレッド**: 重いFFmpeg/解析はJob内。再生中は#43のポリシーで待機。GUIは既存のJob取得/キャンセル（200ms snapshot）を再利用。

## 連鎖方式の決定（36.2）
各段は既に独立Jobとして公開済みで、連鎖に必要な接続点が揃っている。新しい合成Jobを足すと、子Job追跡・取消伝播・再接続時の状態復元をCoreに二重実装することになるため採用しない。

| 段 | Job | 次段へ渡すもの |
|---|---|---|
| 1 | `audio.import { source_path }` | 完了Jobの`artifacts[0]`＝`imports/<id>/original_48k.wav`（`normalized.json`に`duration_samples`） |
| 2 | `analysis.song { analysis.input_artifact: <1の参照> }` | 完了Job（結果artifactと取込用export） |
| 3 | `song.import { song_import.analysis_job_id: <2のJob> }` | `imported_song{song_id,reused}` |

- **取消**: クライアントが現在段のJobだけを取り消し、後続段を作らない。`song.import`は完了した解析Jobだけを受理するため、前段失敗・取消で未完成曲は作られない。
- **段別エラー**: 失敗したJob自身の`error_code`がそのまま段名の代わりになる（1=`audio_*`、2=`input_required`等、3=`import_*`）。
- **再接続**: 各Jobは既存のSSE/snapshotで復元できる。クライアントが保持するのは3つのJob IDだけで、GUI側の連鎖状態機械は36.4で実装する。
- **契約の確認済み範囲**: 手順1の`artifacts`が`artifact_ref::validate_ref`を通ること（`audio_job/tests.rs`）、`analysis.input_artifact`が受付時に検証され、Python側が同じartifact root配下を解決すること（`service.py`）。1→2→3の通しは実モデル・実FFmpegが必要なため36.5で確認する（Unverified）。
- **重複登録（要注意）**: `song.import`の`reused`は「同じ解析Jobの再取込」だけを冪等にする。同じ元音源を再度連鎖すると解析Jobが別になるため、別の曲として登録される見込み（未実測）。内容hashによる重複検出は未決事項に残し、36.4のGUIで警告するか別Issueにする。

## 実装スライス
| # | 内容 | 依存 | 完了条件・テスト |
|---|---|---|---|
| 36.1 | 正規化Job化: 元音源→正規化WAVのCore Job（artifact参照で出力） | #4 | `core/tests/job_api.rs`系: 形式matrix fixture、`duration_samples`一致、破損入力のerror、取消 |
| 36.2 | **決定済み（本書）**: クライアント連鎖。Core側の追加実装なし | 36.1, 既存`song.import` | 契約は「連鎖方式の決定」。連鎖の組立てと失敗/取消で未登録のE2Eは36.4（`Client.Tests`のモックCore）へ移す |
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
- 元音源の受け渡し方式（アップロード/Coreが選択ファイルを読む）と一時領域の掃除
- 同じ元音源を再取込した場合の重複登録の扱い（`verify_reuse`は解析Job ID一致が条件のため、内容hashでは検出されない。警告のみか、取込前の既存曲照合かを36.4で決める）
- 参照歌詞入力の画面側仕様
