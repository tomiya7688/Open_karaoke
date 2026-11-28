# #36 残り: 原曲取込から曲登録までの一括導線 設計

> 状態: Python側`analysis.song`（正規化済みWAV→SongBundle）とCore側`song.import`（完了Job→SongLibrary登録）は実装済み。
> **36.1 実装済み（Core Job）**: `kind: audio.import`（`source_path`）で元音源(WAV/AAC/MP3/MP4)をFFmpeg正規化し、private staging→1回のrenameで公開する（失敗・取消で部分出力を残さない）。30分超は`audio_too_long`で拒否し、#43の重いJobとして再生中は待機する。コードは`core/src/audio_job.rs`、`audio_import.rs`。実機のFFmpeg・実曲での確認は未実施（Unverified）。
> **36.2 決定（2026-11-28）**: 合成Job（`song.ingest`案）は作らず、クライアント連鎖（`audio.import`→`analysis.song`→`song.import`）を採用する。理由と契約は「連鎖方式の決定」。
> **36.3 実装済み**: 入力は`source_path`維持、4GiB上限、`imports`の7日掃除。
> **36.4a 実装済み**: クライアントの連鎖ロジック`SongIngestFlow`。
> **36.4b 実装済み**: 取込画面とShell結線。
> **未接続**: 重複取込の警告・GUIの描画/実機確認(36.5)。(取込操作の画面自体は36.4bで実装済み)。最終確認日: 2026-11-28
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
- 入力: 合成Jobは作らないため`song_ingest`は存在しない。クライアントは任意パスではなく、36.3で決める安全な受け渡し方式で元音源を指す（方式は未決）。`title`は`analysis.song`のオプションで必須（空文字不可）のため、クライアントが段2の要求に必ず付ける（既定はファイル名由来。決め方は36.4）。
- 出力: 段の進行は各Jobの`stage`/`progress`で読む。`imported_song{song_id,reused}`は段3のJobに付く。失敗は各Jobの`error`文字列（`<code>: <message>`形式）で、`error_code`の別フィールドは無い。クライアントは先頭の`code`で分類する（段1=`audio_*`、段2=解析サービスの`code`、段3=`song_import_*`）。
- 正規化artifactは48kHz stereo f32 WAV（[Audio import](../audio_import.md#internal-format)）。`duration_samples`は正規化後のsample数から取り、SongBundleと一致を検査。

**エラー**: FFmpeg無し/不正buildconf/破損入力/モデル未導入(`input_required`等)は段名つきの明示errorで`failed`。途中失敗・取消時は`song.import`を実行せず、SongLibraryに未完成曲を作らない（既存の確定境界を使う）。同一入力hashの再実行は既存の`reused`規則に従う。
**スレッド**: 重いFFmpeg/解析はJob内。再生中は#43のポリシーで待機。GUIは既存のJob取得/キャンセル（200ms snapshot）を再利用。

## 入力受け渡し方式の決定（36.3）
- **採用**: `audio.import { source_path }`（利用者が選んだローカルファイルの絶対パスをCoreが直接読む）。Coreは127.0.0.1限定・認証済みクライアントのみ受け付けるため、アップロード経路（二重コピーと一時領域の管理）を足す利点がない。`source_token`は不採用。
- **入力制限**: 絶対パス、対応拡張子（WAV/AAC/MP3/MP4）、通常ファイル、読取可、4GiB以下。ここまでの違反はJob作成前に4xx（上限超過は413）で拒否する。長さ30分以下は正規化後に判定するため、超過したJobは`failed`（`audio_too_long`）になる。
- **任意パスの扱い**: 読むのは元音源の正規化だけで、結果は`imports/<id>`にコピーされた成果物からのみ公開される。SongStoreへ任意パスを渡す経路は作らない（既存の`song.import`方針を維持）。
- **掃除**: Core起動後にバックグラウンドで、`imports`直下の正規形（小文字・ハイフン付き）の`<uuid>`と`.staging-<uuid>`で最終更新が7日より古いものを削除する。`imports`自体がリンクなら何もせず、子のリンクと別表記の名前は触らない。失敗は警告に出して続行する。公開済みの成果物も7日で消えるため、クライアントは7日を超えて参照を保持しない。同じデータ領域を2つのCoreが同時に使う構成は想定しない。取込済みの曲は`song.import`でコピー済みのため影響しない。実行中のJobの作業領域は新しいので消えない。
- **前提と限界**: 4GiB上限は受付時の事前検査で、読み込み中の実量上限ではない。出力の大きさは30分上限とffmpegの`-t`で別に制限される。検査後のファイル差し替えや`imports`直下の差し替え競合は、artifact領域へ書き込める別プロセスを想定しないローカル単一ユーザー前提として許容する。
- **未実装**: 実行中にディスク使用量の上限を超えた場合の扱い、利用者が手動で掃除する操作（必要なら36.4で検討）。

## 連鎖方式の決定（36.2）
各段は既に独立Jobとして公開済みで、連鎖に必要な接続点が揃っている。新しい合成Jobを足すと、子Job追跡・取消伝播・再接続時の状態復元をCoreに二重実装することになるため採用しない。

| 段 | Job | 次段へ渡すもの |
|---|---|---|
| 1 | `audio.import { source_path }` | 完了Jobの`artifacts`から名前`original_48k.wav`のもの（`imports/<id>/original_48k.wav`。添字固定にせず、`normalized.json`の`audio_artifact`でも同じ値を得られる）。値は相対のartifact参照文字列で、絶対パスは渡さない。`duration_samples`は`normalized.json`にあり、段2の解析結果との一致検査に使う |
| 2 | `analysis.song { analysis.input_artifact: <1の参照> }` | 完了Job（結果artifactと取込用export） |
| 3 | `song.import { song_import.analysis_job_id: <2のJob> }` | `imported_song{song_id,reused}` |

- **取消**: クライアントが現在段のJobだけを取り消し、後続段を作らない。`song.import`は完了した解析Jobだけを受理するため、解析が完了していない段階での失敗・取消では曲は登録されない（確認済みの範囲）。連鎖状態（3つのJob IDと現在段）の保持・再接続後の復元・取消と次段作成が競合した場合の扱いは未決で、36.4で決める。
- **段別エラー**: 失敗したJob自身の`error`文字列の先頭`code`が段名の代わりになる（1=`audio_*`、2=`input_required`等、3=`song_import_*`）。
- **再接続**: 各Jobは既存のSSE/snapshotで復元できる。ただしJob履歴はCoreのメモリ上にあり、Core再起動後は旧Job IDが404になる。再起動を跨いだ連鎖の継続はできず、クライアントは段1からやり直す（公開済みの`imports/<id>`は残る）。クライアントが保持するのは3つのJob IDだけで、GUI側の連鎖状態機械は36.4で実装する。
- **契約の確認済み範囲**: 手順1の`artifacts`が`artifact_ref::validate_ref`を通ること（`audio_job/tests.rs`）、`analysis.input_artifact`が受付時に検証され、Python側が同じartifact root配下を解決すること（`service.py`）。1→2→3の通しは実モデル・実FFmpegが必要なため36.5で確認する（Unverified）。
- **重複登録（要注意）**: `song.import`の`reused`は「同じ解析Jobの再取込」だけを冪等にする。同じ元音源を再度連鎖すると解析Jobが別になるため、別の曲として登録される見込み（コード上の条件からの推測で未実測）。内容hashによる重複検出の有無も未確認で、未決事項に残し、36.4のGUIで警告するか別Issueにする。

> 本節は決定の記録を兼ねる（この規模の決定は個別のADRにせず、設計書内に状態つきで残す。上書きする場合は本節を更新する）。

## 実装スライス
| # | 内容 | 依存 | 完了条件・テスト |
|---|---|---|---|
| 36.1 | 正規化Job化: 元音源→正規化WAVのCore Job（artifact参照で出力） | #4 | `core/tests/job_api.rs`系: 形式matrix fixture、`duration_samples`一致、破損入力のerror、取消 |
| 36.2 | **決定済み（本書）**: クライアント連鎖。Core側の追加実装なし | 36.1, 既存`song.import` | 契約は「連鎖方式の決定」。連鎖の組立てと失敗/取消で未登録のE2Eは36.4（`Client.Tests`のモックCore）へ移す |
| 36.3 | **実装済み**: 入力は`source_path`方式を維持し、4GiB上限（413 `audio_source_too_large`）と`imports`の7日掃除を追加。`source_token`は不採用 | #41 | `audio_job/tests.rs`: 上限境界、掃除が`<uuid>`と`.staging-<uuid>`だけを消すこと。相対パス・未対応拡張子・非通常ファイルの拒否は36.1から |
| 36.4a | **実装済み**: `SongIngestFlow`（`gui/OpenKaraoke.Client`）。連鎖の組立て・段別の失敗・現在段だけの取消。`CreateJobRequest`に`audio_import`/`song_import`、`JobSnapshot`に`imported_song`を追加 | 36.1〜36.3 | `Client.Tests`の5契約（段順・失敗で後続を作らない・取消・不正入力・要求本文）。**画面（ファイル選択・進捗・曲一覧反映）と`ShellViewModel`結線は36.4bで未実装** |
| 36.4b | **実装済み**: `SongImportViewModel`（入力検証・進捗・取消・切断で破棄）、`ShellViewModel`結線（完了後に曲一覧を再取得して選択）、「曲の取り込み」タブ（パス・参照ダイアログ・曲名・取込/取消・進捗）。重複取込の警告は未実装 | 36.2, 36.3, #15 | `Client.Tests`の4契約（入力検証・成功/失敗・取消/切断・Shell結線）。**`Gui.Smoke`での描画確認と実機操作は未実施（Unverified）** |

## 触るファイル
`core/src/audio_import.rs`、`core/src/lib.rs`（Job kind・`prepare_import`周辺）、`core/src/song_api.rs`・`song_library/`、`core/tests/job_api.rs`・`song_import.rs`、`python/src/open_karaoke_analysis/song.py`（入力契約は原則不変）、`gui/OpenKaraoke.Client/`・`gui/OpenKaraoke.Gui/`、`docs/song_import.md`・`current_status.md`。`core/src/lib.rs`/`ShellViewModel`は競合しやすい。

## 守る規則
- [宣言コメント必須](../coding_rules.md#5-ソースコードコメント)、[境界とエラー](../coding_rules.md#3-境界とエラー)
- 48kHz整数sample indexを全段で一貫（[architecture](../architecture.md#オーディオ内部仕様)）
- FFmpegはLGPL構成のみ受理（[Licensing](../licensing.md#ffmpeg-and-codecs)）。ユーザー楽曲の権利は別（[Licensing](../licensing.md#music-lyrics-and-user-content)）
- 「解析Job完了」「登録完了」「歌える」は別状態（[Current status](../current_status.md)）。実モデル未導入はmock成功で代替しない

## 未決事項
- 同じ元音源を再取込した場合の重複登録の扱い（`verify_reuse`は解析Job ID一致が条件のため、内容hashでは検出されない見込み・未実測。警告のみか、取込前の既存曲照合かを36.4で決める）
- 参照歌詞入力の画面側仕様
