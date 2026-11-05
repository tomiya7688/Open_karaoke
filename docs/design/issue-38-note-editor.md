# #38 任意の歌詞・採点ノート編集 設計

> 状態: 38.1実装済み（編集コマンド型と検証の純関数 `core/src/song_edit.rs`）。38.2以降（保存・API・GUI）は未実装。P3、#16の後続。
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
- 注意: `SongLibrary::load`系は無効(`valid=false`)artifactを含む束を拒否し得る（`song_library/validation.rs`の検査）。編集で下流を`invalidate`した曲が一覧/再生から消えないよう、「編集済みで再生成待ち」状態の扱いを38.2で定義する（未決）。
- 下流: 歌詞編集→`ArtifactKind::Lyrics`を`mark_regenerated`、依存する`Notes`/`Evidence`は`invalidate`の伝播規則に従う（`song_data.rs`既存API）。

**エラー**: `base_generation`不一致は409相当で拒否。**世代比較から保存までを曲単位の排他で直列化**する（同時要求が同じ世代を読んで後勝ち上書きしない）。保存は全文書で原子的でなければならない: 既存`SongStore::save`は文書を順次置換し、`write_json`は既存ファイルを削除してからrenameする（`song_data.rs`）ため、途中失敗で新旧が混在し得る。**編集用に「全文書を一時領域へ書いてから切替」する保存を新設するか既存を直すかを38.2で決める**。
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

`base_generation`はコマンドが保持するだけで、競合判定・保存・invalidateは38.2で行う。

## 実装スライス
| # | 内容 | 依存 | 完了条件・テスト |
|---|---|---|---|
| 38.1 | 編集コマンド型と検証（純関数）、出自表現の決定（`format_version`上げ要否） | #3 | `core/tests/`: 不正range・不正参照ID拒否、原子性、決定的出力 |
| 38.2 | 保存+世代管理+下流invalidate（`SongLibrary`上） | 38.1 | 再読込保持、`base_generation`競合、invalidate伝播 |
| 38.3 | Core公開API（`PATCH`系、#41の版管理に従う） | 38.2, #41 | 認証/上限/サイズ制限のcontract test |
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
- APIの形（#41確定後）と同時編集の扱い
