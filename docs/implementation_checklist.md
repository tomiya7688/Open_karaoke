# 実装チェックリスト（着手前・PR前・完了）

手戻りを防ぐ**唯一の確認表**。詳細は各リンク先が正本で、ここへ書き写さない。Issue別の設計・スライスは [設計書索引](design/README.md)。

## A. 着手前（5分）
- [ ] [共同作業ルール §1](collaboration.md#1-作業開始): `git fetch` → 最新`main`から分岐。同じIssue/ファイルの未統合PR・ブランチが無いか確認し、Issueへ着手を宣言
- [ ] worktreeは**E:のみ**: `git worktree add -b <branch> E:/projects/worktrees/<topic>/Open_karaoke origin/main`（共有checkout `E:\projects\Open_karaoke` は触らない。C:・`%TEMP%`・`/tmp`・リポジトリ内`tmp/`禁止。scratchは`E:/program_files/soft/claude/tmp`）。開始時に`git worktree list`でC:のパスが無いか確認
- [ ] 新worktreeは初回のみ依存を復元: `dotnet restore gui/OpenKaraoke.Client.Tests`（他のguiプロジェクトも）
- [ ] [設計書索引](design/README.md)のIssue行 → 設計書1つ → 取るスライスを決める。設計が無い/食い違うなら**コードより先に設計書を更新**（存在しないAPIを前提にしない）
- [ ] 触る範囲を [AI_CONTEXT](../AI_CONTEXT.md) と [Task routes](ai_workflow.md#task-routes) の該当行で絞る（全docs・全sourceを読まない）
- [ ] 競合しやすい`core/src/lib.rs`・`ShellViewModel`を触るなら、マージ直前に`origin/main`へrebase

## B. 実装中の必須規則
- [ ] 型・メソッド・関数すべてに日本語の**JSON風宣言コメント**（`/// { 責務 / 処理 / 引数 / 戻り値 / フィールド }`、Pythonはdocstring）: [コーディングルール §5](coding_rules.md#5-ソースコードコメント)。内部コメントはJSON風にしない
- [ ] 責務分離・境界・エラー: [§2](coding_rules.md#2-ui--process--dataの責務)、[§3](coding_rules.md#3-境界とエラー)。具象起点の継承禁止: [継承規約](coding_rules.md#継承の共通規約必須レビュー基準)
- [ ] 内部時刻は**48kHz整数sample index**。秒/MIDI tick/モデルhopは境界で変換（[architecture](architecture.md#オーディオ内部仕様)、[audio_clock](audio_clock.md)）
- [ ] **realtime分離**: audio callbackにlock/確保/I/O/HTTP/Python/モデル推論を入れない。採点入力はエフェクト前のraw mic
- [ ] モデルはAdapter越し。コードと重みのライセンスは別に確認し、公式配布物に含める依存は[Licensing](licensing.md)を満たす。ユーザーMODのloaderにライセンス硬制限を作らない
- [ ] 保存形式・公開APIを変えるなら`format_version`/API版・migration・互換テストを同じPRに入れる
- [ ] 無関係なリファクタ・一括置換・生成物・秘密・重み/音源をPRに混ぜない。署名/著作権者の新規表記は「Tomiya」（既存表記は一括置換しない）
- [ ] 仕様・テスト・文書（`current_status.md`、各設計書冒頭の「状態」行と[設計書索引](design/README.md)の「状態・次に取るスライス」）を同じPRで更新。mock/合成fixtureの成功を実機能・実歌唱精度の証拠にしない

## C. 整形とテスト（変更範囲のみ。全体は最後に1回）
| 言語 | 整形 | テスト/静的検査 |
|---|---|---|
| Python | `E:\projects\Open_karaoke\.venv\Scripts\ruff.exe format <files>` と `ruff check <files>`（ruff 0.17.2, line-length 100） | pytest（下記レシピ） |
| Rust | `cargo fmt` | `cargo clippy --offline -- -D warnings`、`cargo test --offline` |
| C# | `dotnet format <変更したプロジェクト.csproj> --include <変更ファイル>`（solution全体の一括整形はしない。検証は`--verify-no-changes`） | `dotnet run --project gui/OpenKaraoke.Client.Tests --configuration Release -- --unit`。実結合は引数に`<core.exe> <python.exe>`の絶対パス |
| Markdown/PS1 | 手動（相対リンクは`check_local`が検査） | `pwsh scripts/check_local.ps1` |

- **Python pytest（Windows worktreeレシピ）**: 基本Pythonは`E:\program_files\soft\IDE\compiler\python\python3.14\python.exe`、`PYTHONPATH=<worktree>\python\src;E:\program_files\soft\claude\tmp\open-karaoke-venv\Lib\site-packages`、`--basetemp E:/program_files/soft/claude/tmp/<名前>`。タイミング依存で落ちたテストは単独で再実行して判定
- **最後に1回**: `pwsh scripts/check_local.ps1 -Python 'E:\projects\Open_karaoke\.venv\Scripts\python.exe'`（選択規則は[Development](development.md#local-pre-pr-check)）。FAIL無し、SKIP/UNVERIFIEDはPRのUnverifiedへ
- `git diff --check`がCRLF警告で落ちる場合は作業ファイルをLFへ戻す。`Start-Sleep`は使わずMonitorで待つ

## D. レビュー（実装・整形・テスト・自己レビュー後）
- [ ] `E:/projects/REVIEW_OPERATIONS.md` を**実際に開いて**最新手順を確認（記憶で済ませない）
- [ ] 資料（12000文字以内、秘密なし。BASE/HEAD/差分/テスト結果/未確認範囲）を`E:\projects\.review-work\Open_karaoke\<topic>\packet-01.txt`に書く
- [ ] `& E:\projects\.review-tools\Invoke-AllReviews.ps1 -Packet <packet-01.txt> -Repo tomiya7688/Open_karaoke -Pr <番号>` → `evidence.md`とCodeRabbit依頼が自動生成される。Haiku/Copilotは依頼しない。拒否・無応答は省略として記録済み、再試行しない
- [ ] 指摘は原典と照合し、有効なものだけ修正→再整形→再テスト。無効な指摘は理由を記録

## E. PR・マージ
- [ ] コミット: `<type>(<scope>): <要約> (#N)`、`-F ファイル`で渡し末尾の`Co-Authored-By`は**実際に作業したエージェントの表記**（Claude Codeなら `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`。人・他ツールには付けない/各自の表記）。`main`へ直接pushしない
- [ ] PR本文は下記テンプレート。Claude Codeが作った場合のみ末尾に `🤖 Generated with [Claude Code](https://claude.com/claude-code)`
- [ ] 最新head SHAの**全CI成功**（pending/skippedは未成功）＋[自己レビュー](collaboration.md#自己レビュー)の両方で`gh pr merge <PR> --squash --delete-branch`。判断が要る場合は[人へ確認する条件](collaboration.md#5-マージ)に従い開いたまま報告
- [ ] 後片付け: worktree削除（`Filename too long`は`\?\`パス）、Issue更新（未達は`Refs`）

```text
Goal / Issue: Closes #N / Refs #N
Agent: Claude Code | Codex | ChatGPT | Copilot | human
Base -> Head SHA:
Design: docs/design/issue-<N>-<slug>.md の スライス <N.x>
Changed files / decision:
Verified: command, result
Unverified: skipped / unavailable / 実機・実歌唱・実モデルの未確認
Review: 6者+CodeRabbit の evidence.md の場所、採用/不採用の指摘
Self-review: (指摘と対処)

(Claude Codeの場合のみ) 🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

## F. 完了の定義
1. 設計書のスライスの**完了条件とテスト**が満たされ、受入条件の未達分は`Refs`でIssueに残した
2. 宣言コメント・整形・`check_local`・最新SHAのCIが通り、`Unverified`が明示されている
3. 設計書/`current_status.md`が実態と一致し、レビュー証跡（第D節）が揃っている
4. 「動く」「未完」「未評価」を区別した報告（合成成功を精度・実機の合格と書かない）
