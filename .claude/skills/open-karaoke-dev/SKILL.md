---
name: open-karaoke-dev
description: Open Karaokeで1つのIssue/PRを最小の読込量で進める手順。Issue選定、Eドライブworktree、対象source/testだけの読込、1コマンドのローカル検査、PR、レビュー、マージまで。Open_karaokeで実装・修正・「次を進めて」と言われたら最初に使う。
---

# Open Karaoke 開発の最短手順

目的はトークン節約。**読むものを決めてから読む**。全docs・全Issue・ログ全文は読まない。

## 1. 開始（1回のコマンドにまとめる）

パスはこのPCの配置（メインcheckout `E:\projects\Open_karaoke`、worktreeは `E:/projects/worktrees/`）。別環境ではcheckoutの場所に読み替える。

```powershell
cd <メインcheckout>
git fetch -q; git status -sb; git worktree list
gh issue list --state open --limit 40 --json number,title --jq '.[]|"\(.number) \(.title)"'
gh pr list --json number,headRefName,title --jq '.[]|"\(.number) \(.headRefName) \(.title)"'
```

- メインcheckoutのdirty（AGENTS.md / CLAUDE.md / QWEN.md / .gitignore の方針配布差分）は他者作業。触らない・commitしない。
- 作業は必ず `git worktree add -b <branch> E:/projects/worktrees/<topic>/Open_karaoke origin/main`。Cドライブ・repo内 `tmp/` に作らない。
- 対象Issueは `gh issue view <n> --json body --jq .body` だけ読む。コメントは必要時のみ。
- 着手時にIssueへ「担当ツール・ブランチ名・実装単位」をコメントする（`docs/collaboration.md` §1）。ブランチ名は `<type>/issue-<番号>-<要約>`。

## 2. 読む範囲

- `AI_CONTEXT.md` の「正本」と `docs/ai_workflow.md` の **Task routes 表の該当1行** だけ。
- 該当sourceは `git grep -n` で位置を絞り、Readは offset/limit 付き。
- 読まない: `target/` `.venv/` `node_modules/` `artifacts/` `**/bin` `**/obj` 、モデル重み・音源。
- 守る境界: 48 kHz整数sample index、音声callbackへPython/I/O/alloc禁止、モデルはAdapter越し、mock/合成fixtureの成功を実曲精度と言わない。
- コーディングルールは `docs/coding_rules.md` の「必須」だけ確認（JSON-like宣言コメント）。

## 3. 検証

- PR前は **`pwsh scripts/check_local.ps1`** の1コマンド（変更ファイルから検査を自動選択、PASS/FAIL/SKIP/UNVERIFIEDを1つの要約で出力、FAILなら終了コード1）。
- 依存が無いと FAIL で止まる。自動installはしない。必要なら `python -m pip install -e "./python[dev]"` をユーザー環境で1回。
- 完全な検査は CI（`scripts/check.ps1` と各workflow）。成功ログ全文は読まず、失敗jobの最初のerrorだけ読む:
  `gh run view <id> --log-failed | Select-Object -First 60`

## 4. PR〜マージ

1. commit（末尾に `Co-Authored-By`）→ `git push -u origin <branch>` → `gh pr create`（本文末尾に Claude Code の署名行、`Closes #n` か `Refs #n`）。
2. Handoff形式（Goal / Base→Head / Changed / Verified / Unverified / Next）をPR本文に書く。
3. 外部レビューはユーザーレベルの `multi-ai-review` skill に従う（無ければ `E:/projects/REVIEW_OPERATIONS.md`）。証跡は `E:\projects\.review-work\Open_karaoke\<task>\`。
4. CIは `gh pr checks <n> --watch` ではなく、Monitor/バックグラウンドで待つ。全SUCCESS＋自己レビューOKなら
   `gh pr merge <n> --squash --delete-branch --match-head-commit <SHA>`。
5. マージ後: `git worktree remove <path>`。Issueのチェック項目はGitHub上で更新。`docs/current_status.md` を直す場合も `origin/main` からのブランチとPRで行う（メインcheckoutのdirtyと混ぜない）。

## 5. 報告

成功はコマンド・結果・SHAを1行ずつ。未実行・skip・実行中は `Unverified`。所要時間・削減率は測ったときだけ書く。
