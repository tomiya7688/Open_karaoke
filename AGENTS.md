# Agent entry point（リンクポータル）

ここは**索引**。下の表で「やりたいこと」から**読む1ファイル**を選ぶ。全docs・全Issues・過去のチャット・ログ全文を初期コンテキストへ投入しない。

| やりたいこと | 読む1ファイル |
|---|---|
| プロジェクト概要・正本・守る境界 | [AI_CONTEXT.md](AI_CONTEXT.md) |
| 実装者向け編集マップ（Issue→設計書・編集対象・禁止する越境・最初のテスト） | [docs/implementation_navigation.md](docs/implementation_navigation.md#issueから編集箇所を選ぶ) |
| Issueの設計・実装スライスを知る | [docs/design/README.md](docs/design/README.md) → 該当Issueの設計書（`docs/design/issue-<N>-*.md`） |
| 着手前・PR前に確認する（唯一のチェックリスト） | [docs/implementation_checklist.md](docs/implementation_checklist.md) |
| pull・ブランチ・PR・CI・自己レビュー・マージの手順 | [docs/collaboration.md](docs/collaboration.md) |
| 触るsource/testsを絞る | [docs/ai_workflow.md#task-routes](docs/ai_workflow.md#task-routes) |
| コーディングルール（JSON-like宣言コメント含む） | [docs/coding_rules.md](docs/coding_rules.md) |
| 現在の実装状況と未評価範囲 | [docs/current_status.md](docs/current_status.md) |
| 実装順序・優先度・分割済みの実装単位 | [docs/implementation_plan.md](docs/implementation_plan.md) |
| ビルド・ローカル検査・CI | [docs/development.md](docs/development.md)、[docs/testing.md](docs/testing.md) |
| レビュー（複数AI・PRレビュー）の起動と証跡 | `E:/projects/REVIEW_OPERATIONS.md`（作業の開始時と完了報告前に**実際に開く**） |
| フォーマッター | `E:/projects/FORMATTER_OPERATIONS.md`、言語別コマンドは[チェックリスト§C](docs/implementation_checklist.md#c-整形とテスト変更範囲のみ全体は最後に1回) |
| 強いモデルへ相談する（advisor）場面と禁止事項 | [docs/advisor.md](docs/advisor.md) |
| ライセンス・モデル・配布条件 | [docs/licensing.md](docs/licensing.md) |

## 必ず守る規則

Claude Code / Codex / ChatGPT / GitHub Copilot / 人間の共通規則。手順の正本は [docs/collaboration.md](docs/collaboration.md)、確認表は [docs/implementation_checklist.md](docs/implementation_checklist.md)。

1. **毎回、作業前に必ず pull する。** `git fetch` → `git switch main` → `git pull --ff-only origin main`。最新の `main` からブランチを作る。
2. 着手前に、同じIssue・ファイルの未統合PR/ブランチや統合済みの実装がないか確認し、Issueへ着手を宣言する。
3. **コーディングルール [docs/coding_rules.md](docs/coding_rules.md) の「必須」には必ず従う。** 設計書（[索引](docs/design/README.md)）を実装前に確認・更新し、型・API・失敗経路が変わるなら設計書も同じPRで更新する。 新規・変更宣言の日本語JSON-like宣言コメントを同時更新する（`scripts/check_declaration_comments.py`でCI検査）。
4. PR作成前のローカル検査は[共同作業ルール §3](docs/collaboration.md#3-作業終了-prを作る)に従う。
5. **作業終了時は必ずPRを作成する。** `main` へ直接pushしない。
6. CIの結果(最新head SHAで全て成功)と自己レビューの**両方**が問題なければ、`gh pr merge --squash --delete-branch` でマージする。問題があれば直して再検証し、判断が要る場合は[人へ確認する条件](docs/collaboration.md#5-マージ)に従いPRを開いたまま報告する。
7. 未検証の範囲は成功と区別して `Unverified` と書く。

## 作業環境・レビュー・整形（要点。詳細は上表のリンク先）

- worktree・検証用checkout・一時ファイルは**Eドライブのみ**（`E:/projects/worktrees/<topic>/Open_karaoke`、scratchは`E:/program_files/soft/claude/tmp`）。共有checkout `E:\projects\Open_karaoke` では作業しない。
- 実装・整形・関連テスト・自己レビューの後に外部AIレビュー（`E:\projects\.review-tools\Invoke-AllReviews.ps1`）。証跡のない項目は「未実施」と報告する。レビュー担当は編集禁止・別レビュアーを起動しない。
- 変更した言語の整形（`ruff` / `cargo fmt` / `dotnet format`）を変更範囲だけに実行し、コマンドと結果を報告に残す。
- 方針確定前・同じ失敗が2回続いた時・完了報告前だけadvisor（Opus）に相談する。レビュー証跡の代わりにはならない。詳細は[docs/advisor.md](docs/advisor.md)。
- 署名・著作権者の新規表記は「Tomiya」。既存表記の一括置換はしない。
