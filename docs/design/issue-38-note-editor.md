# #38 任意の歌詞・採点ノート編集 設計

> 状態: 38.1実装済み（編集コマンド型と検証の純関数 `core/src/song_edit.rs`）。38.2実装済み（保存・世代管理・下流invalidate `core/src/song_library/edit.rs`）。38.3実装済み（`PATCH /songs/{id}` `core/src/song_api.rs`）。38.4以降（GUI）は未実装。P3、#16の後続。
> 通常導線（編集せず歌う）を妨げないことが最優先。

## 目的
自動生成した歌詞・採点ノートを、必要な場合だけ手直しできるようにする。元の解析evidenceと手編集結果を区別し、編集後も下流artifactの整合を保つ。

## 受入条件（[Issue #38](https://github.com/tomiya7688/Open_karaoke/issues/38) から転記）
- 編集なしでカラオケ再生を開始する通常導線を妨げない
- 編集結果を再読込しても保持できる
- 不正なsample rangeや存在しない参照IDを拒否する
- GUI側からCore API経由で編集する
- テスト: UI/ViewModel、保存復元、Undo/Redo、schema契約、48kHz時刻整合

## 非目標
- 自動品質改善・再解析（#108）、MIDI取込（#37）、歌唱画面の再生制御（#16）
- 共同編集・クラウド同期
- 編集UIの高度な波形表示（初版は表形式+プレビュー再生）

## 設計
**責務**
- Core（正本）: `SongEditService`（新設, `core/src/song_library/`配下想定）が編集コマンドを検証→`SongBundle::validate()`→原子的保存。GUIは`SongBundle`を直接書かない。
- GUI: `SongEditorViewModel`（新設, `gui/OpenKaraoke.Client/`）が編集状態・Undo/Redoスタックを持つ（UI状態のみ。確定はCore）。

**データ契約（案）**
- 編集コマンド: `EditCommand { song_id, base_generation, ops[] }`。`ops`は `update_lyric{id,text,start_sample,end_sample}` / `update_note{id,start_sample,end_sample,midi_note}` / `add_*` / `delete_*`。sampleは48kHz整数。
- 検証（既存`SongBundle::validate`に加え）: `start<end`、`duration_samples`以内、`midi_note 0..=127`、`lyric_segment_id`の存在、ID重複なし。違反は全op不適用（原子的）で拒否。
- 出自の区別（**38.1で決定: `format_version`は上げない**）: 歌詞は既存の`LyricSegment.source`へ`"manual"`を入れる。`Note`には欄を足さず、`EvidenceDocument`の`target_id=note.id`の項目へ`evidence["edit_origin"]={"source":"manual"}`を追記する。既存型・既存JSONは不変で追加のみ（未知キーは既存読込側が許容するevidenceの自由マップ）。Noteへ型の欄を足す案は、全Note生成箇所とPython出力・migrationの変更を要し、公開契約を変えるため採らない。将来必要になった時点で`format_version`上げを別Issueで判断する。
- 編集による信頼度の扱い（38.1）: 本文を変えた歌詞は`text_confidence`、範囲を変えた歌詞は`timing_confidence`、音高を変えたノートは`pitch_confidence`を`None`にする（自動結果の信頼度を手編集結果に残さない）。値が不変なら保持。削除したノートは出自を記録しない（既存evidenceは破壊しない）。
- 元の解析結果は上書きせず保持（`evidence.json`または編集前スナップショット。方式は未決）。
- 注意: `SongLibrary::load`系は無効(`valid=false`)artifactを含む束を拒否し得る（`song_library/validation.rs`の検査）。編集で下流を`invalidate`した曲が一覧/再生から消えないよう、「編集済みで再生成待ち」状態は38.2で定義した（下記38.2）。
- 下流: 歌詞編集→`ArtifactKind::Lyrics`を`mark_regenerated`、依存する`Notes`/`Evidence`は`invalidate`の伝播規則に従う（`song_data.rs`既存API）。

**エラー**: `base_generation`不一致は409相当で拒否。**世代比較から保存までを曲単位の排他で直列化**する（同時要求が同じ世代を読んで後勝ち上書きしない）。保存は全文書で原子的でなければならない: 既存`SongStore::save`は文書を順次置換し、`write_json`は既存ファイルを削除してからrenameする（`song_data.rs`）ため、途中失敗で新旧が混在し得る。**38.2で決定: 既存`SongStore::save`は変えず、編集用に「私的stagingへ完全な曲ディレクトリを組み立て→ディレクトリ置換」を新設した**（下記38.2）。
**スレッド**: 編集は通常のJob/API経路。音声callbackと無関係。編集中のプレビュー再生は#107のセッションを読み取り専用で使用。

## 38.1 受入条件と照合（実装済み）
| # | 条件 | 実装/テスト（`core/tests/song_edit_contract.rs`） |
|---|---|---|
| 1 | 不正range（start>=end、duration超過）を拒否 | `rejects_invalid_sample_ranges` |
| 2 | 不正参照ID（存在しないID、重複ID、存在しない歌詞参照、参照中歌詞の削除）を拒否 | `rejects_unknown_duplicate_and_dangling_references` |
| 3 | MIDI範囲外・空ID/空本文・空op・op数上限・別曲IDを拒否 | `rejects_invalid_midi_and_empty_text_or_id`、`rejects_empty_oversized_and_foreign_commands` |
| 4 | 原子性: 1件でも違反なら全op不適用、入力束は不変 | `is_atomic_when_one_op_is_invalid` |
| 5 | 決定的出力: start/end/id順に整列、同入力で同一JSON | `output_is_deterministic_and_order_independent_for_adds` |
| 6 | 出自表現の決定を記録（format_version不変、manual出自） | `applies_valid_edits_and_records_manual_origin`、上記「出自の区別」 |
| 7 | 既存Song Data公開契約を変えない | `song_data.rs`は無変更（`song_edit.rs`新設とlib.rs登録のみ） |

`base_generation`の競合判定・保存・invalidateは38.2で実装した。

## 38.2 決定事項と受入条件の照合（実装済み）
**決定**
- 公開API: `SongLibrary::edit_song(id, &EditCommand) -> Result<EditedSong, SongEditSaveError>`、`edit_generation(&SongBundle)`。`song_data.rs`・`song_edit.rs`は無変更（追加のみ）。
- 世代: `base_generation`は`Evidence` artifactの`generation`。編集成功ごとにEvidenceを必ず再生成するため単調増加する（初期取込直後は1）。
- 排他: ライブラリ全体のOSファイルロック(`.writer.lock`)を世代照合から置換完了まで保持する（曲単位より強い）。取得できなければ`Busy`（再試行は呼び出し側）。同一世代の同時編集は片方だけ成功し、他方は`GenerationConflict`か`Busy`。
- 原子的保存: `.staging-*`へ非編集資産をハードリンク（不可ならコピー）→`SongStore::save`で文書書込+fsync→`import-receipt.json`の`bundle_sha256`更新→往復検証→`<id>`を`.retired-<id>-*`へ退避→staging改名→退避削除。どの段階で失敗しても元のディレクトリを保つ。退避〜改名の短い間は`load`が`NotFound`になり得る。この間に中断された場合は次回の`edit_song`と同曲の`import`（writerロック下）が`.retired-*`を復旧する（import側は復旧後に`Conflict`となり編集を保つ）。復旧が走るまで`list`/`load`はその曲を表示しない。復旧rename自体が失敗した場合も元の曲は`.retired-*`に残る。
- サイズ上限: 書き出す各文書（整形JSON）が読込側上限16MiB未満であること。超過は書込前に`TooLarge`。op数は38.1の`MAX_EDIT_OPS`。
- 下流規則: 歌詞opがあれば`Lyrics`を`invalidate`（`Notes`/`Evidence`へ伝播）してから`mark_regenerated`。ノートopがあれば`Notes`も同様。`Evidence`は毎回再生成。**再生成されなかった派生artifact（例: 歌詞だけ編集した時の`Notes`）は`valid=false`の「再生成待ち」で公開する**。ノートopを含む次の編集で`Notes`がvalidに戻る。
- 公開済み検証の緩和: `load`系は`Lyrics/Notes/Evidence`の`valid=false`を許す（`validate_published`）。原本・stemsは常にvalid必須。取込時の検証は従来どおり厳格。
- 再取込: 編集済み曲に同じ解析Jobを再取込すると、既存の`bundle != export.bundle`により`Conflict`（上書きしない）。再解析との衝突規則は#36側（未決事項）。
- 未決のまま: 元結果のsnapshot保持（編集前の文書は置換時に捨てられる）。必要なら38.3以降で別途決める。

| # | 条件 | テスト（`core/tests/song_edit_save.rs`） |
|---|---|---|
| 1 | 編集結果を再読込・別インスタンスでも保持、資産は不変 | `edit_survives_reload_and_new_library_instance` |
| 2 | `base_generation`不一致を拒否し何も変えない | `stale_base_generation_is_rejected_and_nothing_changes` |
| 3 | invalidate伝播と再生成での回復、再生成待ちでも一覧に残る | `invalidate_propagates_and_is_cleared_by_regeneration` |
| 4 | 検証違反は全ファイル不変・残骸なし | `rejected_edit_leaves_every_file_untouched` |
| 5 | サイズ上限超過は書込前に拒否 | `oversized_document_is_rejected_before_any_write` |
| 6 | 未登録曲・別曲IDを拒否 | `unknown_song_and_foreign_song_id_are_rejected` |
| 7 | 置換中断からの復旧 | `interrupted_swap_is_recovered_before_the_next_edit` |
| 8 | 書込排他（Busy）、同時編集で成功は1件 | `concurrent_writer_gets_busy_and_the_song_is_unchanged`、`racing_edits_with_the_same_base_never_both_succeed` |
| 9 | 再取込で編集を上書きしない（中断後の再取込でも復旧して保つ） | `reimporting_the_original_analysis_never_overwrites_an_edited_song`、`reimport_after_a_crashed_swap_recovers_edits_instead_of_republishing` |
| 10 | 破損した公開曲を編集で変更しない | `damaged_published_song_is_never_modified_by_an_edit` |

## 38.3 決定事項と受入条件の照合（実装済み）
**決定**（契約の詳細は [Core API](../core_api.md#曲の手編集patch-songsid-38-38-3)）
- `PATCH /songs/{id}`、本文`{base_generation, ops}`のみ。曲IDはパスから取りEditCommandへ詰める（本文の`song_id`は`deny_unknown_fields`で422。パスと本文の食い違いは構造上起きない）。
- 応答は`EditedSong`（`song_id`,`generation`,`pending`）。GUIは`GET /songs/{id}`の`song.artifacts.evidence.generation`を`base_generation`に使う（新しい読取APIは足さない）。
- 状態写像: `GenerationConflict`→409、`EditError`→422、`TooLarge`→413、`NotFound`→404、`Busy`/I/O budget満杯→429、他の保存層エラーは既存`/songs`と同じ写像（Io/Json→422）。`error`は`{"error":"<接頭辞>: ..."}`の1文字列で、接頭辞が機械判別キー。違反の詳細（op位置）はメッセージ連結のみで、op単位の構造化は38.4で必要になった時に追加（任意フィールド追加はv1互換）。
- 制限: 本文は全ルート共通64KiB（`MAX_EDIT_OPS`=1024は到達前に本文上限で止まり得る）。保存はblocking taskで、Song library I/O budget（4枠）の枠を1つ使う。認証・Origin・版・no-storeは既存ミドルウェア/Sessionを通る。
- 互換: 既存routeへのmethod追加＝v1後方互換。`GET /songs/{id}`の形は不変。

| # | 条件 | テスト |
|---|---|---|
| 1 | PATCHで保存・世代+1・GETで保持（歌詞`source=manual`、`pending=["notes"]`） | `song_api::tests::patch_saves_edit_advances_generation_and_get_reflects_it` |
| 2 | 競合409・検証失敗422（空op含む）・曲なし404、いずれも曲不変 | `patch_maps_conflict_validation_and_missing_song_to_status_codes` |
| 3 | 不正本文（未知項目・`song_id`本文指定・欠落・負数・未知op・MIDI範囲外）は422、不正パスIDは4xx、曲不変 | `patch_rejects_malformed_bodies_without_touching_the_song` |
| 4 | 本文64KiB超は413、I/O budget満杯は429、曲不変 | `patch_enforces_body_limit_and_io_budget` |
| 5 | Origin拒否403・API版不一致426 | `patch_keeps_origin_and_version_guards` |
| 6 | 認証なしは401+no-store（全製品ルート一覧に追加） | `session::tests::every_product_route_requires_a_session` |
| 7 | 保存・原子性・同時編集は38.2のテストで担保 | `core/tests/song_edit_save.rs`（変更なし） |

## 実装スライス
| # | 内容 | 依存 | 完了条件・テスト |
|---|---|---|---|
| 38.1 | 編集コマンド型と検証（純関数）、出自表現の決定（`format_version`上げ要否） | #3 | `core/tests/`: 不正range・不正参照ID拒否、原子性、決定的出力 |
| 38.2 | 保存+世代管理+下流invalidate（`SongLibrary`上）**実装済み** | 38.1 | 再読込保持、`base_generation`競合、invalidate伝播 |
| 38.3 | Core公開API（`PATCH`系、#41の版管理に従う）**実装済み** | 38.2, #41 | 認証/上限/サイズ制限のcontract test |
| 38.4 | `SongEditorViewModel`+Undo/Redo+一覧表示 | 38.3, #15 | `Client.Tests`: Undo/Redo往復、拒否時のロールバック表示 |
| 38.5 | プレビュー再生・シーク連携 | 38.4, #16/#107 | 編集中の区間を再生できる（headless smoke+実機は`Unverified`） |

## 触るファイル
`core/src/song_data.rs`（型/validate/migration）、`core/src/song_library/`、`core/src/song_api.rs`（ルート追加）、`gui/OpenKaraoke.Client/CoreApiClient.cs`・新設`SongEditorViewModel.cs`、`gui/OpenKaraoke.Client.Tests/`、必要なら`docs/song_format.md`。

## 守る規則
- [宣言コメント（JSON-like）必須](../coding_rules.md#5-ソースコードコメント)、[境界とエラー](../coding_rules.md#3-境界とエラー)
- 48kHz整数sampleのみ（[architecture](../architecture.md#オーディオ内部仕様)）。秒の浮動小数を保存しない
- 保存形式変更は`format_version`+migration+テスト必須。未知の将来versionを黙って読まない
- 元evidenceを破壊的に上書きしない。自動結果と手編集の区別をUIに出す

## 未決事項
- 元結果の保持方式（snapshotファイルか`evidence.json`内か）とNoteのsource欄
- 部分編集後に#36の再解析が走った場合の衝突規則（手編集を優先するか）
- 部分失敗のop単位構造化エラー（38.4で必要になった時に任意フィールドとして追加）
