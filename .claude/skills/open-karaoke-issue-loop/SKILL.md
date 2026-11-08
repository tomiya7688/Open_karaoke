---
name: open-karaoke-issue-loop
description: Open_karaoke（tomiya7688/Open_karaoke）でIssueを「設計→実装→評価→レビュー→PR→CI→マージ」まで進める手順と、サブエージェントへの短い依頼文テンプレート。worktree作成、ローカルテスト手順、レビュー実行、落とし穴、使用量削減の原則をまとめる。複数Issueを並列に進める時に使う。
---

> **クラウド環境での注意**: 本手順はローカルPC(Windows)向けの `E:/` パスやレビューCLI・共通キューを含みます。クラウドセッションでは、それらのローカル専用手順は使わず、このリポジトリの `AGENTS.md` と `.github/workflows` を正とします。Issueの選択順・設計→実装→検証→PRの流れ・落とし穴の部分のみ参照してください。

# Open_karaoke Issue反復ループ

正本は各worktreeの `AGENTS.md` / `AI_CONTEXT.md` / `CLAUDE.md` と `E:/projects/REVIEW_OPERATIONS.md`。本手順は繰り返し使う手順の要約で、矛盾したら正本が優先。依頼文にこれらを書き写さず、このskillを読ませる。

## 使用量を抑える原則（ai-context-reducer の手法に基づく）
- 先に目的・守る条件・終わりの目安を決める。終わりが無い調査をさせない。
- 全文を読む前に `Grep`/`Glob` で関係箇所を絞り、そのファイルだけ読む。全docs・全Issue・ログ全文を渡さない。
- 長い規則は書き写さずリンクで渡す（このskill、AGENTS.md）。同じ説明を依頼ごとに繰り返さない。
- 実行するテストは変更範囲に絞る。全体は最後に1回。タイミング依存で落ちたテストは単独で再実行して判定する。
- 調査の打切り条件を決める（読む範囲を広げるのは、根拠が見つからない時と共有部分を変える時だけ）。
- 不要な往復を避ける: 起票・マージ・片付けなど機械的な手順は1回のコマンドにまとめる。
- 参考: https://github.com/tomiya7688/ai-context-reducer

## 恒久方針（今回だけでなく将来の削減）
- 同じ行動は手で繰り返さず、スクリプト/テンプレートにする。開始は `pwsh E:\projects\.review-tools\Start-IssueWork.ps1 -Issue N -Topic <t> -Branch <b>`（fetch・worktree確認・E:作成・REVIEW_OPERATIONSのハッシュ確認・設計書特定・必須チェックリスト表示を1回で行う）。レビューは Invoke-AllReviews.ps1。
- 実装前に設計書（`docs/design/issue-N-*.md`）の受入条件を番号で抜き出し、実装後に1件ずつ照合する。漏れは手戻りの主因。設計書が無ければ先に書く。
- 毎回読む規則は省略しない。コストは「読まない」でなく「変更検知（SHA256）で冒頭だけ読む」「リンクで渡す」で下げる。
- 同じ手順を3回以上やったら、スクリプトかこのskillに追記する。サブエージェントには手順を書き写さず、このskill名を渡す。

- advisor（Opus）は方針確定前・同じ失敗2回・完了報告前の3場面だけ。呼出し条件の正本は repo の `docs/advisor.md`。レビュー証跡の代わりにならない。サブエージェント依頼文には書き写さない。

## 0. 開始時（Start-IssueWork.ps1 が行う内容。手動の場合のみ以下）
```powershell
git -C E:/projects/Open_karaoke fetch -q origin
git -C E:/projects/Open_karaoke worktree list   # C: のパスがあれば報告。他セッションのworktreeは触らない
```
共有checkout `E:\projects\Open_karaoke` は触らない。他セッションが扱うIssue（worktreeが既にあるもの）は取らない。

## 1. worktree（E:のみ）
```powershell
git worktree add -b <branch> E:/projects/worktrees/<topic>/Open_karaoke origin/main
# 新worktreeは初回のみ: dotnet restore gui/OpenKaraoke.Client.Tests（他のguiプロジェクトも）
```
scratchは `E:/program_files/soft/claude/tmp`。C:・%TEMP%・/tmp 禁止。

## 2. 実装・整形・テスト
- 型とメソッドすべてに日本語のJSON風宣言コメント（`/// { 責務/処理/引数/戻り値/フィールド }`、Pythonはdocstring）。
- 整形: `dotnet format` / `E:\projects\Open_karaoke\.venv\Scripts\ruff.exe`（format+check）/ `cargo fmt` + `cargo clippy -D warnings`。
- Pythonテスト: 基本Python `E:\program_files\soft\IDE\compiler\python\python3.14\python.exe`、`PYTHONPATH=<worktree>\python\src;E:\program_files\soft\claude\tmp\open-karaoke-venv\Lib\site-packages`、pytest は `--basetemp E:/program_files/soft/claude/tmp/<名前>`。
- C#実結合: `dotnet run --project gui/OpenKaraoke.Client.Tests --configuration Release -- <core.exe> <python.exe>`。
- 最後に `scripts/check_local.ps1 -Python 'E:\projects\Open_karaoke\.venv\Scripts\python.exe'`。

## 3. レビュー（1コマンド）
資料（12000文字以内、秘密なし、BASE/HEAD/差分/テスト結果/未確認範囲）を `E:\projects\.review-work\Open_karaoke\<topic>\packet-01.txt` に書き:
```powershell
# REVIEW_OPERATIONS.md「共通キュー経由で起動する」に従い、外側の起動口を1回だけ資源 review で包む（run_in_background で1回、内側の個別レビューは包まない）
pwsh E:/projects/small_projects/shared-queue/Invoke-SharedQueue.ps1 -Resource review -Project Open_karaoke -WorkDir <資料のdir> -Command "& 'E:/projects/.review-tools/Invoke-AllReviews.ps1' -Packet '<packet-01.txt>' -Repo tomiya7688/Open_karaoke -Pr <番号>" -MaxWaitMinutes 60
```
待機超過(124)は省略として記録し再投入しない。修正後の再確認は修正差分だけの packet-02 を Luna 1者に送る（全文の再レビューはしない）。
全レビュアーを並列起動し、`evidence.md` と CodeRabbit 依頼を自動で作る。指摘は原典と照合し有効なものだけ修正。Haiku/Copilotは依頼しない。拒否・無応答は省略として記録済みなので追加作業しない。

## 4. PR・マージ
- コミット末尾: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`（`-F ファイル`で渡す）。PR本文末尾: `🤖 Generated with [Claude Code](https://claude.com/claude-code)`。
- 全CI成功＋自己レビュー後に `gh pr merge --squash --delete-branch`。完了後worktreeを削除（`Filename too long` は `\\?\` パスで削除）。

## 5. 落とし穴
- `Start-Sleep` は使えない（Monitorで待つ）。PowerShellの `[IO.File]` は絶対パス。
- 共有の `E:\projects\Open_karaoke\.venv` はその checkout の editable install。worktree での実結合テスト（C#→Core→Python）には worktree専用venv（例 `E:\program_files\soft\claude\tmp\venv<名前>`）を使う。共有venvだと503で失敗する。
- `gh pr merge` が GraphQL の TLS タイムアウトで失敗したら REST（`gh api -X PUT repos/tomiya7688/Open_karaoke/pulls/<N>/merge -f merge_method=squash`）で代替する。
- CRLF警告で `git diff --check` が落ちる場合は作業ファイルをLFに戻す。
- 複数セッションが `core/src/lib.rs` と `ShellViewModel` を触る: マージ前に `origin/main` へrebase。
- `pwsh -File` で `-Reviewers A,B` を渡すとスクリプト側でカンマ分割される(修正済み)。
- Luna(Codex)が `exit=1` で skipped のときは ERROR: usage limit(枠切れ。復活時刻はログに出る、2026-11-05は22:26)。再試行しない。
- Ori（無料nemotron）は2026-11-05に実測で、小資料でも約113秒、本番資料(≈12000字)では180秒で3回連続無応答(#19/#109)。省略として記録し再試行しない。Oriは排他mutexなので並列エージェントでは1者だけが使う。デバッグに時間を割かない。
- 署名は「Tomiya」。既存表記の一括置換はしない。

## 6. サブエージェント依頼文（これだけでよい）
```
repo tomiya7688/Open_karaoke。skill `open-karaoke-issue-loop` に従って Issue #<N> の <範囲> を実装し、
CI成功と自己レビュー後にマージせよ。worktree名: <topic>。受入条件: <3行以内>。触らない: <競合範囲>。
完了時はPRリンク・マージSHA・検証結果・未確認事項を10行以内で報告。
```
