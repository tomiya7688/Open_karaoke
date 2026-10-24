# Open Karaoke: AI entry

ローカル音源からカラオケを作るデスクトップアプリ。
Rust Core / Python Analysis Service / C#・Avalonia GUI のモノレポ。
このファイルは索引であり、仕様や実装済み機能の正本ではない。

## 着手する順序

0. 共通規則([共同作業ルール](docs/collaboration.md)): 作業前に必ず`git pull --ff-only`、終了時は必ずPR、CIと自己レビューが問題なければマージ。
1. 今回の依頼と対象Issueを確認する。「次」の場合もIssueの状態を取得する。
2. [Remote delta](docs/ai_workflow.md#remote-delta)でブランチ・差分・未統合PRを確認し、統合済み・他者作業中の実装と重複しないことを確かめる。
3. [Task routes](docs/ai_workflow.md#task-routes)の該当行からsourceとtestsだけを選ぶ。
4. Goal / Required / Acceptance / Working setを短く決め、必要な根拠が揃ったら実装へ進む。
5. 対象検証から始め、共有契約の変更や不明な影響があれば検証範囲を広げる。
6. 終了時はPRを作り、最新SHAのCIと自己レビューを確認してからマージする。

## 正本

- 今回の目的・完了条件: ユーザーの依頼とGitHubの対象Issue。
- 要件・責務: [requirements](docs/requirements.md)、[architecture](docs/architecture.md)。必要な節だけ読む。
- 実際の挙動・互換性: 作業対象SHAのsource、matching tests、API・保存形式の定義。
- 共同作業の手順(pull・ブランチ・PR・CI・自己レビュー・マージ): [collaboration](docs/collaboration.md)。
- コーディングルール: [coding rules](docs/coding_rules.md)（JSON-like宣言コメントを含む。「必須」は必ず守る）。設計背景は[architecture](docs/architecture.md)を参照する。
- 検証手順: [development](docs/development.md)、`scripts/check.ps1`、`.github/workflows/`。
- 現在の実装と未評価範囲: [Current status](docs/current_status.md)。記載日のmain SHAと作業対象のSHAを区別する。
- 歌詞抽出 #7（実装あり・Issue受け入れ条件は要確認）: [Lyric transcription](docs/lyric_transcription.md)、`python/src/open_karaoke_analysis/lyrics.py`、`whisper_backend.py`、`lyric_evaluator.py`、`python/tests/test_lyric*.py`。実録歌唱の評価は別途確認する。
- 歌詞整列 #8: [Lyric alignment](docs/lyric_alignment.md)、`python/src/open_karaoke_analysis/alignment*.py`、`ctc_alignment.py`、`python/tests/test_alignment*.py`、`core/tests/alignment_contract.rs`。日本語文字単位の整列と参照歌詞の差分保持。実録歌唱の時刻精度は未評価。
- F0解析 #9: [Pitch / F0 analysis](docs/pitch_analysis.md)、`python/src/open_karaoke_analysis/pitch.py`、`pitch_detectors.py`、`pitch_fusion.py`、`pitch_evaluator.py`、`python/tests/test_pitch*.py`。single/ensemble、voiced確率、octave/時間連続性と合成Evaluator。実録歌唱精度は未評価。
- Vocal Event #10: [Vocal Event fusion](docs/vocal_events.md)、`vocal_events.py`、`vocal_event_features.py`、`vocal_event_fusion.py`、`vocal_event_evaluator.py`、`python/tests/test_vocal_event*.py`。note/lyric境界scoreは未校正。
- 採点譜面生成 #11: [Score generation](docs/score_generation.md)、`notes.py`、`note_candidates.py`、`note_optimizer.py`、`note_evaluator.py`、`python/tests/test_note*.py`。合成fixture回帰のみで実録歌唱精度は未評価。
- Rust Audio Engine #12: [Audio engine](docs/audio_engine.md)、`core/src/audio_engine.rs`、`audio_device.rs`、`audio_wav.rs`、`core/examples/wasapi_smoke.rs`。CIはmockとWindowsビルド、物理デバイスの音声・遅延・hot-plugは未検証。

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
