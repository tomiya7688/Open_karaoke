# Agent entry point

最初に [AI_CONTEXT.md](AI_CONTEXT.md) を読む。
そこから現在の依頼・Issueに対応する参照先だけを選び、原典と対応テストを確認する。
全docs・全Issues・過去のチャット・ログ全文を初期コンテキストへ投入しない。

## 必ず守る規則

Claude Code / Codex / ChatGPT / GitHub Copilot / 人間の共通規則。手順の正本は [docs/collaboration.md](docs/collaboration.md)。

1. **毎回、作業前に必ず pull する。** `git fetch` → `git switch main` → `git pull --ff-only origin main`。最新の `main` からブランチを作る。
2. 着手前に、同じIssue・ファイルの未統合PR/ブランチや統合済みの実装がないか確認し、Issueへ着手を宣言する。
3. **コーディングルール [docs/coding_rules.md](docs/coding_rules.md) の「必須」には必ず従う。**
4. **作業終了時は必ずPRを作成する。** `main` へ直接pushしない。
5. CIの結果(最新head SHAで全て成功)と自己レビューの**両方**が問題なければ、`gh pr merge --squash --delete-branch` でマージする。問題があれば直して再検証し、判断が要る場合は[人へ確認する条件](docs/collaboration.md#5-マージ)に従いPRを開いたまま報告する。
6. 未検証の範囲は成功と区別して `Unverified` と書く。
