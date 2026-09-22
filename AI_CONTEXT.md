# Open Karaoke: AI entry

ローカル音源からカラオケを作るデスクトップアプリ。
Rust Core / Python Analysis Service / C#・Avalonia GUI のモノレポ。
このファイルは索引であり、仕様や実装済み機能の正本ではない。

## 着手する順序

1. 今回の依頼と対象Issueを確認する。「次」の場合もIssueの状態を取得する。
2. [Remote delta](docs/ai_workflow.md#remote-delta)でブランチ・差分・未統合PRを確認する。
3. [Task routes](docs/ai_workflow.md#task-routes)の該当行からsourceとtestsだけを選ぶ。
4. Goal / Required / Acceptance / Working setを短く決め、必要な根拠が揃ったら実装へ進む。
5. 対象検証から始め、共有契約の変更や不明な影響があれば検証範囲を広げる。

## 正本

- 今回の目的・完了条件: ユーザーの依頼とGitHubの対象Issue。
- 要件・責務: [requirements](docs/requirements.md)、[architecture](docs/architecture.md)。必要な節だけ読む。
- 実際の挙動・互換性: 作業対象SHAのsource、matching tests、API・保存形式の定義。
- 検証手順: [development](docs/development.md)、`scripts/check.ps1`、`.github/workflows/`。
- 現状メモ: [Snapshot](docs/ai_workflow.md#snapshot)。更新日時とSHAを必ず確認する。
- 歌詞抽出 #7: [Lyric transcription](docs/lyric_transcription.md)、`python/src/open_karaoke_analysis/lyrics.py`、`whisper_backend.py`、`lyric_evaluator.py`、`python/tests/test_lyric*.py`。実録歌唱の評価は別途確認する。

仕様・コード・Issueの不一致は明示する。要約で原典を上書きしない。

## 探索を増やさない

- `git diff --name-only` / `git grep -n` / ファイル名・見出しで絞ってから対象行を読む。
- 同じSHAの既読ファイルやツールスキーマを繰り返し取得しない。
- Requiredが揃えば「念のため」の履歴・隣接機能・全docs探索を止める。
- 未解決の互換性・ライセンス・安全性・テスト失敗があれば、その箇所だけ探索を再開する。
- 無関係なリファクタリングを混ぜない。索引が古ければ該当行だけ更新する。

## 守る境界

- 内部時間の正本は48 kHz基準の整数sample index。モデル固有の時間軸は境界で変換する。
- リアルタイム音声処理と重い解析を分離する。Python・モデル推論・I/Oを音声callbackへ持ち込まない。
- モデルはAdapter越しに接続する。コードと重みのライセンスは別々に確認する。
- 未実装処理・mock・合成fixtureでの成功を、本番機能や実曲精度の証拠にしない。

## 検証と報告

[Validation](docs/ai_workflow.md#validation)の該当手順を使う。
成功時はコマンド・結果・対象SHAを短く記録する。失敗時だけ関連ログを読む。
未実行・skip・実行中・確認できない領域は `Unverified` とし、成功と区別する。
局所テストの成功で既存CI、実モデル検証、配布物検証、必要なGUI目視を代替しない。

## 通常は読まないもの

`target/`、`**/bin/`、`**/obj/`、`.venv/`、`__pycache__/`、`.pytest_cache/`、
`.ruff_cache/`、`*.egg-info/`、`artifacts/`、モデル重み、音源・データセット、
実行時のjobs/cache/saves、無関係な履歴。対象自体を検証する場合だけ範囲を限定して読む。
依存変更時のlockfile、固定fixture、ライセンス記録は必要な根拠として確認する。
