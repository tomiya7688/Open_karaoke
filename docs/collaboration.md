# 共同作業ルール

Claude Code、Codex、ChatGPT、GitHub Copilotと人間が同じリポジトリで作業するための規則。
どのツールでも同じ手順に従う。ツール固有の入口は[AGENTS.md](../AGENTS.md)を参照する。
コーディングルールは[coding_rules.md](coding_rules.md)で、**必須**の規則には必ず従う。

## 基本方針

- 取り込み先は`main`。作業は必ずブランチとPRで行い、`main`へ直接pushしない。
- 状態の正本はGitHub(Issue / PR / CI)。チャットや個々のツールの記憶を正本にしない。
- 他のツールが作業中かもしれないので、始める前に重複を確認し、終わったら必ず統合する。
- 検証していないことを検証済みと書かない。未検証は`Unverified`として残す([Handoff](ai_workflow.md#handoff))。

## 1. 作業開始

毎回、作業を始める前に次を行う。前回の状態やキャッシュを信用しない。

```text
git status --short --branch
git fetch --no-tags origin
git switch main
git pull --ff-only origin main
```

- 未コミット変更がある場合は、先にコミットまたはstashして内容を失わないようにする。他者の変更を破棄しない。
- `git pull --ff-only`が失敗したら、ローカルの`main`に独自のコミットがある。原因を調べ、`main`へ積まずに作業を止めて報告する。
- 対象Issueを取得し、実装単位と受け入れ条件を確認する。
- **重複確認:** `gh pr list --state open`と`git branch -r`で、同じIssue・同じファイルを扱う未統合のPR/ブランチがないか確認する。
  すでに`main`へ統合済みか、他者が作業中なら、重複して作らず報告する。
- **着手宣言:** 他者の作業と重ならないと確認できたら、Issueへ「担当ツール・ブランチ名・対象の実装単位」をコメントする。
  同じ実装単位を2つのツールが同時に持たない。宣言済みなら別の単位を選ぶ。
- 最新の`main`から作業ブランチを作る: `git switch -c <type>/issue-<番号>-<要約> origin/main`
  (`type`は`feat` / `fix` / `docs` / `test` / `ci` / `refactor`)。
- 作業の途中でも、編集前とpush前に`git fetch`して`main`の更新と競合を確認する。

## 2. 作業中

- 1ブランチ・1PR・1つの実装単位。無関係なリファクタリングや別Issueの変更を混ぜない。
- [coding_rules.md](coding_rules.md)に従う。特に、宣言コメント(JSON-like)、境界での検証、48 kHz整数sample、テストの用意は必須。
- 仕様・共有契約・テスト・必要な文書を一緒に更新する。コードと文書が食い違う状態でPRにしない。
- コミットは小さく、メッセージは`<type>(<scope>): <要約> (#Issue番号)`の形にする。
- 他のツール・人のブランチへpushしない。自分のブランチ以外で`--force`を使わない。自分の未統合ブランチを
  `main`へ追従させるときは`git fetch`後に`git rebase origin/main`し、pushは`--force-with-lease`だけを使う。
- 秘密情報、モデル重み、音源・データセット、生成物をコミットしない。ライセンスが不明な依存・データは追加しない。
- 実行環境の制約で検証できないものは、成功扱いにせずPRの`Unverified`へ書く。

## 3. 作業終了: PRを作る

作業が終わったら、必ずPRを作成する。放置したローカル変更やpush済みで未PRのブランチを残さない。

1. ローカルで[Validation](ai_workflow.md#validation)の該当手順(format / lint / テスト)を実行して通す。
2. `git fetch`し、`origin/main`が進んでいれば取り込み、再検証する。
3. pushしてPRを作る。タイトルはコミットと同じ形式。本文は次のテンプレートを埋める。
   完了したIssueには`Closes #N`、受け入れ条件が一部未達なら`Refs #N`を使う(未達のIssueを閉じない)。

```text
Goal / Issue: Closes #N / Refs #N
Agent: Claude Code | Codex | ChatGPT | Copilot | human
Base -> Head SHA:
Changed files / decision:
Verified: command, result
Unverified: skipped / unavailable / real-data limitations
Self-review: (4. の結果。指摘と対処)
```

## 4. CIと自己レビュー

CIの結果と自己レビューの**両方**が問題なしのときだけ、次へ進む。

### CI

- `gh pr checks <PR>`で、最新のhead SHAに対する全てのcheck(`linux-contracts`、`windows-build-and-check`、
  変更範囲に該当する`.github/workflows/`のモデル系workflow)が成功していることを確認する。
- 失敗は同じブランチで直して再pushする。原因を調べずに再実行を繰り返さない。flakyと判断するなら根拠をPRへ書く。
- pending / skipped / cancelledは成功ではない。未完了のままマージしない。
- CIが対象外の範囲(実モデル、実録音、GUI目視、実機)は`Unverified`のまま扱い、CIの成功で代替しない。
- 古いSHAのCI結果を流用しない。pushし直したら最新SHAで確認し直す。

### 自己レビュー

自分のdiff全体を、PR画面または`git diff origin/main...HEAD`で読み直す。次を確認し、結果をPRへ残す。

1. [coding_rules.md](coding_rules.md)の「実装時の確認」6項目に適合しているか(責務、状態の所有、UI/Process/Data境界、時刻・保存形式の契約、宣言コメント、テスト)。
2. 受け入れ条件を満たしているか。満たさない部分は`Refs`にして`Unverified`へ書く。
3. 範囲外の変更、デバッグ用コード、不要なファイル、秘密情報、重み・データの混入がないか。
4. 公開API・保存形式・共有契約の変更で、利用側と互換性・移行を確認したか。
5. 文書(`AI_CONTEXT.md`、`docs/current_status.md`、仕様書)が実態と一致しているか。
6. 完了報告が実際に確認した範囲に限られているか(mock・合成fixtureの成功を実機能の証拠にしていないか)。

他のツールや人のレビューコメントがあれば、全て対応または理由を返信してからマージする。

## 5. マージ

次を**全て**満たすときだけ、PR作成者がマージする。

- CIが最新head SHAで全て成功している。
- 自己レビューで未解決の問題がない。レビューコメントが全て解決している。
- `main`と競合しておらず、PRがdraftではない。
- 次の「人へ確認する条件」に該当しない。

```text
gh pr merge <PR> --squash --delete-branch
```

履歴は1PR 1コミットの`<type>(<scope>): <要約> (#PR番号)`に揃える(既存の`main`と同じ)。管理者権限でのcheck回避、
`--admin`、required checkの無効化、auto-mergeの有効化は行わない。

**人へ確認する条件(マージせずPRを開いたまま報告する):**

- ライセンス、モデル・重みの取得元、依存の追加・更新に判断が要る。
- 保存形式・公開APIの破壊的変更、データ削除・移行を含む。
- CIや検証手順を弱める変更(checkの削除・skip化・閾値の緩和)。
- 受け入れ条件の解釈が不明、または自己レビューで判断できない問題が残る。
- CIが赤いまま、または原因を説明できない失敗がある。

## 6. マージ後

```text
git switch main
git pull --ff-only origin main
```

- 作業ブランチを削除する(`--delete-branch`で消えていなければ`git branch -d`)。
- Issueの着手宣言を更新する。完了なら閉じる。未達の受け入れ条件があれば残りをIssueへ書く。
- 次の作業は、また[1. 作業開始](#1-作業開始)から始める。

## 7. 衝突・例外

- 他のツールのPR・ブランチと同じファイルを触る必要がある場合は、先に相手のPRの統合を待つか、PRにコメントして順序を決める。
- 統合済みの内容と自分の変更が重複していたと分かったら、重複分を捨てて報告する。同じ機能を二重に実装して残さない。
- ルールと実態が食い違ったら、ルールをその場で破らず、この文書を直すPRを作る。

## 8. ツール別の入口

| ツール | 読むファイル |
|---|---|
| Codex、ChatGPT(agent) | [AGENTS.md](../AGENTS.md) |
| Claude Code | `CLAUDE.md`(`AGENTS.md`を取り込む) |
| GitHub Copilot | `.github/copilot-instructions.md` |

入口ファイルは本文を複製せず、この文書へ誘導する。ルールの変更はこの文書だけを更新する。
リポジトリを読めない環境(ChatGPTのチャットなど)で使う場合は、[AGENTS.md](../AGENTS.md)と本書を貼って渡す。

## 9. 推奨するリポジトリ設定(未設定)

ルールを機械的に支えるため、リポジトリ管理者が`main`のブランチ保護を設定することを推奨する。
現時点では`main`は未保護で、上記は運用ルールとしてのみ効力を持つ。

- PR必須(直接pushを禁止)、`linux-contracts`と`windows-build-and-check`をrequired checkにする。
- マージ前に最新の`main`への追従を要求する。
- force pushと`main`の削除を禁止する。
- squash mergeのみを許可し、マージ後のブランチ削除を自動にする。
