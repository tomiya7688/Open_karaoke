# #38 任意の歌詞・採点ノート編集 設計

> 状態: 設計のみ（P3、#16の後続）。編集APIとEditor ViewModelは**新設**で現mainに存在しない。
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
- 出自の区別: `LyricSegment.source`は既存。`Note`にはsource欄が無い → 追加には`format_version`更新と`migrate_document`対応が必要（[song_format](../song_format.md#バージョニング--migration)）。
- 元の解析結果は上書きせず保持（`evidence.json`または編集前スナップショット。方式は未決）。
- 下流: 歌詞編集→`ArtifactKind::Lyrics`を`mark_regenerated`、依存する`Notes`/`Evidence`は`invalidate`の伝播規則に従う（`song_data.rs`既存API）。

**エラー**: `base_generation`不一致は409相当で拒否（他編集との競合）。保存失敗時は元データを保持（一時ファイル置換、既存`SongStore::save`の方式）。
**スレッド**: 編集は通常のJob/API経路。音声callbackと無関係。編集中のプレビュー再生は#107のセッションを読み取り専用で使用。

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
