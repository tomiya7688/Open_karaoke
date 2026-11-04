# Agent entry point

最初に [AI_CONTEXT.md](AI_CONTEXT.md) を読む。
対応する[実装者向け編集マップ](docs/implementation_navigation.md)のIssue1行で、正本の設計書・編集対象・禁止する越境・最初のテストを特定する。その後、必要な原典だけを読む。
全docs・全Issues・過去のチャット・ログ全文を初期コンテキストへ投入しない。

## 必ず守る規則

Claude Code / Codex / ChatGPT / GitHub Copilot / 人間の共通規則。手順の正本は [docs/collaboration.md](docs/collaboration.md)。

1. **毎回、作業前に必ず pull する。** `git fetch` → `git switch main` → `git pull --ff-only origin main`。最新の `main` からブランチを作る。
2. 着手前に、同じIssue・ファイルの未統合PR/ブランチや統合済みの実装がないか確認し、Issueへ着手を宣言する。
3. **設計書を実装前に書き、実装中に更新する。** [共同作業ルール §2](docs/collaboration.md#2-作業中)に従い、型・API・状態・失敗経路を正本の設計書へ定義してからコードとテストを揃える。コーディングルール [docs/coding_rules.md](docs/coding_rules.md) の宣言コメント（JSON-like）を含む「必須」に従い、新規・変更宣言のコメントを同時更新する。
4. PR作成前のローカル検査は[共同作業ルール §3](docs/collaboration.md#3-作業終了-prを作る)に従う。
5. **作業終了時は必ずPRを作成する。** `main` へ直接pushしない。
6. CIの結果(最新head SHAで全て成功)と自己レビューの**両方**が問題なければ、`gh pr merge --squash --delete-branch` でマージする。問題があれば直して再検証し、判断が要る場合は[人へ確認する条件](docs/collaboration.md#5-マージ)に従いPRを開いたまま報告する。
7. 未検証の範囲は成功と区別して `Unverified` と書く。
