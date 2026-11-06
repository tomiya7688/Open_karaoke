# 宣言コメント規約の適用状況

> 2026-11-04時点の `main` `907d166b2ca0bcfbaf739593a53004d9e0eef02e` を抜粋調査。
> **網羅的・言語仕様に基づくlintの結果ではない。** 単純なソース構文パターンで主要ファイルの宣言付近を照合し、実例をソースで再確認してから判断する。

## 結論

[コーディングルール §5](coding_rules.md#5-ソースコードコメント) は、
`AGENTS.md` → `docs/collaboration.md` → `docs/coding_rules.md` の導線に含まれ、
`AI_CONTEXT.md`やCopilot/Claude向けの入口でも参照されている。
**ただし「参照されている」だけでは準拠していない。**

通常のCIはRuff、Rustfmt/Clippy、.NETのビルドなどを実行するが、従来は宣言コメントの専用検査がなかった。今回`scripts/check_declaration_comments.py`を追加し、PRで**追加・署名変更した本番コードの宣言だけ**にJSON-like規約の必須項目を検査する（既存コード全体の自動監査ではない）。

### 代表例

| 影響範囲 | 規約コメントが不足する代表例 | 状況 |
|---|---|---|
| Python Service | `python/src/open_karaoke_analysis/service.py` の`JobManager`、`submit`、`run` | 主要契約にコメント不足。API/Job境界を改修するときに優先して補う |
| Python Adapter | `alignment.py` の`AlignmentAdapter`、`pitch.py`の`PitchAdapter`、`vocal_events.py`の`VocalEventAdapter`、`notes.py`の`NotesAdapter` | 入出力・例外を理解する際の追加コード読込が必要になっている |
| Rust Song Data | `core/src/song_data.rs`の`SongBundle`、`SongManifest`など | 公開保存形式の正本なのに型宣言への説明が少ない |
| Rust Core API | `core/src/lib.rs`の`JobStatus`、`Job`など | Job状態・互換性を型宣言近くで確認できない |
| C# 接続 | `gui/OpenKaraoke.Client/CoreConnection.cs`の`ICoreConnection`、`CoreApiClient.cs`のデータrecord | 公開契約がコードを追わないと分かりにくい |
| 既に充実 | `core/src/audio_clock.rs`・`audio_engine.rs`、`gui/OpenKaraoke.Client/KaraokeViewModel.cs`・`SongTimeline.cs` | 一部にJSON-like宣言コメントがある。全ファイルが不足というわけではない |

この例は「対象ファイルの一部が不足している」ことを示す。**数値で全体の準拠率と呼ばない**。
複数行宣言、属性、継承した契約、docstringの位置などは単純な正規表現だけでは判定できない。

## 新規・変更部分の扱い

1. 実装者は[編集マップ](implementation_navigation.md)から触る型を選び、**その型と変更する関数の宣言コメントを先に確認**する。
2. 変更対象のコメントがなければ、コード変更時に`責務`/`フィールド`/`処理`/`引数`/`戻り値`を実態に沿って補う。
3. 実装の振る舞いが変わったら**コメントも同じコミットで更新**し、[設計書](collaboration.md#2-作業中)の責務・不変条件と照合する。
4. 既存ファイルの数百宣言すべてを、無関係な機能PRで一括変更しない。後続の変更時に段階的に修正する。
5. 自動検査は、**差分に新規追加・署名変更のある宣言のみ**をまず対象にする。既存の不足を理由に無関係なPRをすべて失敗させない。正規表現ヒューリスティックだけで完全に守られたと宣言しない。

## 差分自動検査（今回追加）

- CI: Linuxの標準workflowで`python scripts/check_declaration_comments.py --base HEAD^1`を実行。checkoutはbaseの親commitを確認できるようdepth 2とする。
- ローカル: 既存の単一入口`pwsh scripts/check_local.ps1`に同じ検査を組み込む。作業ツリーで変更した宣言と未追跡の新規sourceも対象。
- 対象: `core/src/**/*.rs`、`python/src/open_karaoke_analysis/**/*.py`、`gui/OpenKaraoke.Client/**/*.cs`、`gui/OpenKaraoke.Gui/**/*.cs`。
- PythonはASTで宣言とdocstringを照合。Rust/C#は宣言開始行と直前の`///`を**簡易検査**。複雑な構文・宣言の内部変更・既存の未整備箇所は依然として手動レビューが必要。
- 欠落の自動検査は**設計書の内容・コメントの正確さを保証しない**。コードと違うコメントや必要なエラーの記述不足は人間/エージェントのレビューが必要。
- テスト: `python/tests/test_declaration_comments.py`。

## コメントに最低限記載すること

### 型・状態オブジェクト

- `責務`: 何を保証するか。
- `フィールド`: 持つ状態、所有・破棄するデータ、重要な不変条件。
- 必要ならAPI/ソングフォーマットの単位、共有可否、エラーへの対応。

### 関数・メソッド

- `責務`: 呼び出し側の意図が分かる1文。
- `処理`: 設計上意味のある処理順。コード1行ずつの言い換えにはしない。
- `引数`: 名前、単位、取り得る範囲。
- `戻り値`: 成功時と特別な戻り値。
- `エラー`: 呼び出し側が対処する必要のある失敗。
- 関数内部には「なぜ」が必要な部分だけ簡潔な通常コメントを記入する。

**コメントだけで設計書を置き換えない。** コンポーネント間のデータフロー・責務の所有・契約は
[実装者向け編集マップ](implementation_navigation.md)と、そこから辿る機能別設計書を正本にする。
