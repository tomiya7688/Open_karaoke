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

### 設計書は実装前に作り、実装中に更新する（必須）

**設計は完了後の議事録ではない。** 最初に[実装者向け編集マップ](implementation_navigation.md)の該当Issue行を読み、編集する型/関数・触らない境界と最初のテストを確定する。追加する動作の責務・契約・状態遷移・失敗時の扱いを
決めながら`docs/`の該当設計書を編集する。Issue本文の箇条書きや、PRがCloseされたという事実を
設計書の代わりにしない。

実装系Issueは次の順で作業する。

1. **実装前**: 正本となる設計書のパスを決める。該当設計書がなければ新設し、同じ内容の文書を乱立させない。
   受け入れ条件のうち設計が必要な動作を**型・操作・イベント・データフロー・状態遷移・不変条件・失敗時・テスト**へ落とす。
   実装済み/提案/未決定を分け、先行仕様と違う決定は理由と影響範囲を記す。
2. **実装中**: 設計上の曖昧さが見つかったらコードを推測で固めず、まず設計書のその節を更新してから実装・テストを揃える。
   小さな差分は同じコミット、大きな設計変更は同じPR内の先行コミットで更新し、レビューで追える形にする。
3. **レビュー前**: 設計書の公開型・操作・API/保存形式・状態遷移・エラー・検証範囲と
   実際のsource/matching testsを突き合わせる。**「設計済み」だけで「実装済み」とは記さない**。
4. **マージ/Close時**: 実装済みの契約と残った制約、検証できた範囲が設計書に反映されているか確認する。
   未達ならIssueをCloseせず既存Issueに残すか、独立した後続Issueへ対象・依存・受け入れ条件を引き継ぐ。

設計書には最低限、**なぜこの責務を分けるか、似た役割の命名と委譲関係、
入力/出力/状態の型と所有者、通常/失敗/キャンセル経路、バージョン/互換性、対応テスト**を書き、
可能ならsource/testのパスへ直接リンクする。実装で使わない抽象基底クラスや不要な図表を増やさず、
**コードを書く人が迷わず実装できる具体度**を優先する。
実機・実モデルの計測が必要な決定は未確定のままにし、どう検証するかを示す。

純粋な誤字/整形等の**設計に影響しない変更**では新しい設計書を作らない。
その場合もPR本文で「設計変更なし」と理由を明示する。
既存の契約が変わる小さなバグ修正では、別文書を作らず対応する設計書の節を更新する。

**実例:** [歌唱セッションの実装詳細設計](karaoke_session_design.md#107-実装詳細設計オーディオ経路と状態所有)では、
`AudioIo`・`WavFeeder`・`Mixer`の現状の非接続を確認した上で、
新設するPCMのキュー所有者、委譲、epoch/seek、API、テストを定義する。


### 実装とコミット

- 1ブランチ・1PR・1つの実装単位。無関係なリファクタリングや別Issueの変更を混ぜない。
- [coding_rules.md](coding_rules.md)に従う。特に宣言コメント(JSON-like)を新規・変更する宣言へ必ず付ける。ローカル`check_local.ps1`とCIは差分宣言を自動検出し、既存コメント不足は[監査](comment_coverage.md)を参照して触る箇所から補う。境界検証、48kHz整数sample、テストも必須。
- 仕様・共有契約・テスト・必要な文書を一緒に更新する。コードと文書が食い違う状態でPRにしない。
- コミットは小さく、メッセージは`<type>(<scope>): <要約> (#Issue番号)`の形にする。
- 他のツール・人のブランチへpushしない。自分のブランチ以外で`--force`を使わない。自分の未統合ブランチを
  `main`へ追従させるときは`git fetch`後に`git rebase origin/main`し、pushは`--force-with-lease`だけを使う。
- 秘密情報、モデル重み、音源・データセット、生成物をコミットしない。ライセンスが不明な依存・データは追加しない。
- 実行環境の制約で検証できないものは、成功扱いにせずPRの`Unverified`へ書く。

## 3. 作業終了: PRを作る

作業が終わったら、必ずPRを作成する。放置したローカル変更やpush済みで未PRのブランチを残さない。

1. ローカルで `pwsh scripts/check_local.ps1` を実行し、FAILが無いことを確認する（[Local pre-PR check](development.md#local-pre-pr-check)）。要約のUNVERIFIED/SKIPはPRのUnverifiedへ書く。
2. `git fetch`し、`origin/main`が進んでいれば取り込み、再検証する。
3. pushしてPRを作る。タイトルはコミットと同じ形式。本文は次のテンプレートを埋める。
   完了したIssueには`Closes #N`、受け入れ条件が一部未達なら`Refs #N`を使う(未達のIssueを閉じない)。

```text
Goal / Issue: Closes #N / Refs #N
Agent: Claude Code | Codex | ChatGPT | Copilot | human
Base -> Head SHA:
Design (canonical docs path, changed sections/decisions):
Code/Tests (interfaces, state transitions, errors, source+matching test):
Design drift (none / proposed decisions changed during implementation):
Verified: command, result
Unverified: skipped / unavailable / real-data limitations
Follow-up: remaining contracts and linked Issue (if any)
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
1a. 設計書を実装前/実装中に作成・更新し、型/イベント/状態/エラー/テストとコードが一致しているか。設計提案を実装済みと誤表示していないか。
2. 受け入れ条件を満たしているか。満たさない部分は`Refs`にして`Unverified`へ書く。
3. 範囲外の変更、デバッグ用コード、不要なファイル、秘密情報、重み・データの混入がないか。
4. 公開API・保存形式・共有契約の変更で、利用側と互換性・移行を確認したか。
5. 文書(`AI_CONTEXT.md`、`docs/current_status.md`、仕様書)が実態と一致しているか。
6. 完了報告が実際に確認した範囲に限られているか(mock・合成fixtureの成功を実機能の証拠にしていないか)。

他のツールや人のレビューコメントがあれば、全て対応または理由を返信してからマージする。

## 5. マージ

次を**全て**満たすときだけ、マージする。マージするのはPR作成者に限らない。**他のAI・セッションが作成したPRも、
下の条件を全て満たせば、どのエージェントもマージしてよい**(リポジトリ所有者の許可済み。PRごとの再確認は不要)。
他者のPRをマージする場合は、そのPRの自己レビュー欄に加えて、マージするエージェントが自分のdiff確認結果をPRコメントへ1回残す。
他者のブランチへのpush・rebase・force pushは引き続き行わない(競合や修正が必要なら作成者へコメントして待つ)。

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
- Issueの着手宣言と設計書参照先を更新する。source/tests/設計書が同じ契約を示し、受け入れ条件を満たす場合だけ閉じる。未達は後続Issueへ具体的に引き継ぎ、元の設計書にもリンクする。
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
