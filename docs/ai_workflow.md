# AI development workflow

## Adoption

参照: [ai-context-reducer](https://github.com/tomiya7688/ai-context-reducer/tree/b4d556547b615d4c6b9231762ba1afdc1a2f26e4)
（確認日: 2026-09-22、MIT）。原則は「機械で確定できる情報を先に取得」「反復操作をまとめる」「必要箇所だけ読む」。
[基本](https://github.com/tomiya7688/ai-context-reducer/blob/b4d556547b615d4c6b9231762ba1afdc1a2f26e4/docs/context-reduction-basics.md)と
[導入判断](https://github.com/tomiya7688/ai-context-reducer/blob/b4d556547b615d4c6b9231762ba1afdc1a2f26e4/docs/adoption-priority.md)を参考に、このrepo用の小さい索引と手順を採用した。

採用: AI入口、Task/Responsibility/Validation routing、SHA付き現状メモ、Remote Delta First、探索停止条件、短い検証報告。
見送り: 大規模call graph、要約キャッシュ、ディレクトリごとの大量のAI文書、専用の影響解析器。
通常のテストはまだ小さく、重い実モデル検証だけ分ける。保守対象を増やす前に実際の反復コストを見る。
`acr-toolbox` は任意の補助であり、製品・ビルドの必須依存にしない。この変更ではbinaryを導入していない。

## Snapshot

2026-09-22時点。作業再開時にはremoteと対象Issue/PRを再確認する。

| 対象 | 確認した参照 | 状態・限界 |
|---|---|---|
| `main` | `0d2a01d4603346da41bd575ab93700c42040fb5c` | 下記の機能ブランチ全体は未統合 |
| Analysis Service | [PR #24](https://github.com/tomiya7688/Open_karaoke/pull/24)、`27122203fb4430a8a97064ac5e0cfe6bd83113da` | Core/Python基盤。Jobはメモリ内 |
| Stem separation | [PR #25](https://github.com/tomiya7688/Open_karaoke/pull/25)、`620601f6aced82e312247d28ebf35006848b8181` | UMX-HQ、キャッシュ、Evaluator。実曲品質・CUDA・GUI登録は未確認/未実装 |
| 次の機能 | [Issue #7](https://github.com/tomiya7688/Open_karaoke/issues/7) | Whisper歌詞抽出。まだ実装済みとして扱わない |

PR #24はmain向け、#25は#24のブランチ向け。この導入変更は#25の実装SHA上で作成している。
マージは別操作。依存PRをmainへ取り込んだ後、後続PRもmainへretargetして差分を確認する。
中間ブランチへのマージだけでmain反映済みとしない。上記SHAの古いCI結果を新しい変更の検証結果へ流用しない。

## Remote delta

ローカルcheckoutがある場合、未コミット変更を保持したまま次をまとめて確認する。
`BASE`は確認済みの取り込み先ref、`HEAD_REF`は作業ブランチのremote refへ置き換える。

```text
git status --short --branch
git fetch --no-tags origin
git rev-parse HEAD BASE HEAD_REF
git rev-list --left-right --count HEAD...HEAD_REF
git diff --stat BASE...HEAD
git diff --name-status BASE...HEAD
```

これは取得・比較だけで、checkout/pull/reset/rebaseはしない。fetch失敗やshallow historyによる比較失敗は
「差分なし」にせずUnverified。必要なref/historyだけ取得する。無条件の全履歴取得やforce pushはしない。
remote側だけに変更があれば、そのcommit summaryとファイル一覧から確認する。編集直前・push前にhead SHAを再確認し、競合を上書きしない。

GitHub connectorだけの環境では対象Issue、対象PRのhead/base SHA、変更ファイル一覧を先に取得する。
続いて必要なファイルを同じSHA・必要行だけ取得する。clone不能を理由に全sourceを読む必要はない。
複数の変更は可能なら1 tree / 1 commitへまとめ、書き込み後のblob SHA/差分で欠落や切り詰めを検査する。
毎回の実装を転送するための一時的な生成・自己commit用CI workflowは通常の手順にしない。

## Task routes

パスはrepo root基準。該当する行だけ読む。予定ファイルを既存ファイルと取り違えない。

| Task | 最初のsource / responsibility | Matching tests / 詳細仕様 |
|---|---|---|
| Job/API | `core/src/lib.rs` | `core/tests/job_api.rs`、`docs/core_api.md` |
| Python起動・復旧 | `core/src/analysis_service.rs`、`core/src/main.rs` | `core/tests/job_api.rs`、`python/tests/test_process.py`、`docs/analysis_service.md` |
| Song Data / migration | `core/src/song_data.rs` | 同ファイル内tests、`core/tests/fixtures/song.json`、`docs/song_format.md` |
| 音源入力・正規化 | `core/src/audio_import.rs` | 同ファイル内tests、`docs/audio_import.md` |
| Python API / Adapter契約 | `python/src/open_karaoke_analysis/contracts.py`、`adapters.py`、`service.py`（同ディレクトリ） | `python/tests/test_service.py`、`docs/analysis_service.md` |
| 音源分離 | `python/src/open_karaoke_analysis/stems.py`、`stem_backend.py`、`stem_audio.py`（同ディレクトリ） | `python/tests/test_stem*.py`、`python/tests/test_stems.py`、`docs/stem_separation.md` |
| 歌詞抽出 #7 | [Lyrics task](#lyrics-task) | `docs/analysis_pipeline.md`の歌詞節、`docs/song_format.md` |
| F0 / Pitch #9 | [Pitch task](#pitch-task) | `docs/pitch_analysis.md`、`python/tests/test_pitch*.py` |
| Vocal Event #10 | [Vocal Event task](#vocal-event-task) | `docs/vocal_events.md`、`python/tests/test_vocal_event*.py` |
| GUI | `gui/OpenKaraoke.Gui/`の対象view/code-behind | `gui/OpenKaraoke.sln`、`docs/architecture.md`。現在はbootstrap shell |
| Build / CI | 対象manifest、`scripts/check.ps1`、`.github/workflows/` | `docs/development.md`、`docs/testing.md` |
| 未着手の機能 | 対象Issueと`docs/implementation_plan.md`から担当領域を特定 | その領域の仕様節だけ確認。存在しないsourceを推測しない |

## Lyrics task

Issue #7を開始する場合の入口。Issueを再取得して以下との差分を確認する。

- Goal: 分離済みvocalsから、Whisper large-v3系で歌詞候補を生成する。
- Required: Adapter契約、LyricsDocumentとの互換性、48 kHz整数時刻への変換、日本語/多言語、モデル・重みの出典とライセンス。
- Acceptance: `lyrics.json`候補、モデル/version、raw ASRとconfidence/evidence、backend交換、明確なJob error。CER/WER、反復・幻覚回帰、日本語歌唱fixtureの根拠を確認する。
- Working set: 最初は`adapters.py`、`contracts.py`、`core/src/song_data.rs`のLyricsDocument周辺。登録時だけ`service.py`、テストは`test_service.py`のAdapter例を参照する。

新規の歌詞Adapter/backend/Evaluator/testファイルはこの時点では予定。実装後にこの行を実パスへ更新する。
音源分離の内部、GUI、採点、全履歴は初期探索から外す。ただし入力契約や共有APIの問題が出た場合は直接依存へ広げる。
歌唱データが不足している場合はfixtureの出典・権利・未評価範囲を残し、合成音声だけで歌唱精度を確認済みにしない。

## Pitch task

Issue #9の入口。Issueを再取得して以下との差分を確認する。

- Goal: 分離済みvocalsから48 kHz整数timelineへ正規化したF0を生成し、detector単体とensembleを切替可能にする。
- Required: `PitchDetector`共通契約、detector confidence、voiced probability、octave consistency、temporal continuity、evidence保存。
- Acceptance: `pitch.json`と`pitch_evidence.json`、single/ensemble設定、合成tone sweep・octave・voiced/unvoiced回帰、detector別/ensemble評価。
- Working set: `pitch.py`、`pitch_detectors.py`、`pitch_fusion.py`、`pitch_evaluator.py`、`test_pitch*.py`。registry変更時のみ`service.py`と`test_service.py`。
- Accuracy boundary: 合成fixtureはアルゴリズム回帰用。実録歌唱の精度確認として扱わない。

## Vocal Event task

Issue #10の入口。Issueを再取得し、Pitch #9のhead/merge状態も確認する。

- Goal: F0/VUV、音響onset、energy/spectral、歌詞alignment、将来のnote onsetを同一48 kHz timelineへ投影する。
- Required: 各特徴量の独立evidence、note/lyric境界score、single/ensemble比較、低品質・legato/melisma回帰。
- Working set: `vocal_events.py`、`vocal_event_features.py`、`vocal_event_fusion.py`、`vocal_event_evaluator.py`、`test_vocal_event*.py`。
- Accuracy boundary: scoreは0..1だが未校正。合成/注釈fixtureを実歌唱精度として扱わない。

## Validation

開発依存は[development.md](development.md)の手順で準備する。毎回の局所修正で再installしない。
依存の追加・更新時は必要なinstall/lockfile更新を行う。以下は作業中の入口であり、CI削減の許可ではない。

| 変更 | 最初に実行する検証 | 広げる条件 |
|---|---|---|
| 文書だけ | `git diff --check`、リンク・パス・見出し・SHA・ルーティング先の存在確認 | API例や実行コマンドを変更したら対応する実行検証も追加 |
| Song Data | `cargo test --locked -p open-karaoke-core song_data::tests` | 保存形式・共有契約ならRust全体と利用側 |
| Audio import | `cargo test --locked -p open-karaoke-core audio_import::tests` | コマンド/変換を変えたら実FFmpegと出力WAVの検証も必要 |
| Python局所 | `python -m pytest python/tests/<対象test>.py -q` | 共通契約/registry/依存/影響不明ならPython全体 |
| Stem pipeline | `python -m pytest python/tests -q -k stem` | 推論/重み/依存/変換変更なら実モデルworkflowも必須 |
| Core/Python共有契約 | `python -m pytest python/tests -q`、`cargo test --locked --workspace --all-targets` | 加えて`cargo test --locked --test job_api -- --ignored`で実プロセスを明示実行 |
| GUI | `dotnet build gui/OpenKaraoke.sln --configuration Release` | UI変更は起動・操作・必要な目視を追加。buildだけで見た目の成功を断定しない |

変更言語の整形・静的解析は既存設定に従う。Pythonは`python -m ruff check python`と
`python -m ruff format --check python`、Rustは`cargo fmt --all -- --check`と
`cargo clippy --locked --workspace --all-targets -- -D warnings`。
統合時は既存のWindows `./scripts/check.ps1`、Linux CI、および変更に必要な実モデル/配布物gateを維持する。
検証生成物はtmp/`artifacts/`へ分離し、source・正本の曲データ・fixtureを上書きしない。

CIはjob/step summaryを先に確認する。失敗時だけ該当jobの最初のerrorと前後を読む。
成功ログ全文、依存download一覧、同じ実行中snapshotを繰り返しコンテキストへ投入しない。
未完了のCIを放置して成功と報告せず、最後に確認した状態を明記する。

## Handoff

タスクごとの大量の恒久文書は作らない。Issue/PRへ次だけ残す。

```text
Goal / Issue:
Base -> Head SHA:
Changed files / decision:
Verified: command, result, relevant CI run
Unverified: skipped / unavailable / running / real-data limitations
Next entry: source/tests pointer or blocking issue
```

時間・tokenの削減率は未測定。今後の同種タスクで再読量・tool往復・検証の重複を比較し、根拠なしに高速化率を報告しない。
