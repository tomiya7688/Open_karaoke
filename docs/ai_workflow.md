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

開発用の固定スナップショットは古くなるため、現在の実装状況は
[Current status](current_status.md) に集約する。そこに記載された確認日・main SHAは
**当時の記録**であり、作業前には必ず対象Issue/PRと最新のmain/headを再確認する。

過去のPR #24〜#34のマージ履歴は [GitHub Pull Requests](https://github.com/tomiya7688/Open_karaoke/pulls?q=is%3Apr) から追える。
2026-09-22当時の「歌詞は未実装」「Stem PR未統合」という情報を現在の前提として使わない。

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

この確認自体は取得・比較だけで、reset/rebaseはしない。作業開始時の`git pull --ff-only`とブランチ作成は
[共同作業ルール](collaboration.md#1-作業開始)に従う。fetch失敗やshallow historyによる比較失敗は
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
| Core API v1 guard #41 | `core/src/lib.rs`のversion・Origin・Job枠 | `core/tests/job_api.rs`、`docs/core_api.md`（session authは後続） |
| 実歌唱dataset #42 | `python/src/open_karaoke_analysis/dataset_manifest.py`、`lyric_dataset_report.py` | `docs/dataset_evaluation.md`、`python/tests/test_dataset_manifest.py`、`test_lyric_dataset_report.py`、`lyric_evaluator.py`、非公開manifestと音源はGit管理外 |
| Evaluator共通結果 #18 | `python/src/open_karaoke_analysis/evaluation_summary.py`、`lyric_evaluator.py` | `docs/testing.md`、`python/tests/test_lyric_evaluator.py`、`test_evaluation_summary.py`。共通形式は歌詞から段階導入 |
| Python起動・復旧 | `core/src/analysis_service.rs`、`core/src/main.rs` | `core/tests/job_api.rs`、`python/tests/test_process.py`、`docs/analysis_service.md` |
| Song Data / migration | `core/src/song_data.rs` | 同ファイル内tests、`core/tests/fixtures/song.json`、`docs/song_format.md` |
| 音源入力・正規化 | `core/src/audio_import.rs` | 同ファイル内tests、`docs/audio_import.md` |
| Python API / Adapter契約 | `python/src/open_karaoke_analysis/contracts.py`、`adapters.py`、`service.py`（同ディレクトリ） | `python/tests/test_service.py`、`docs/analysis_service.md` |
| 音源分離 | `python/src/open_karaoke_analysis/stems.py`、`stem_backend.py`、`stem_audio.py`（同ディレクトリ） | `python/tests/test_stem*.py`、`python/tests/test_stems.py`、`docs/stem_separation.md` |
| 歌詞抽出 #7 | [Lyrics task](#lyrics-task)、`python/src/open_karaoke_analysis/lyrics.py` | `docs/lyric_transcription.md`、`python/tests/test_lyric*.py` |
| F0 / Pitch #9 | [Pitch task](#pitch-task) | `docs/pitch_analysis.md`、`python/tests/test_pitch*.py` |
| Vocal Event #10 | [Vocal Event task](#vocal-event-task) | `docs/vocal_events.md`、`python/tests/test_vocal_event*.py` |
| Score generation #11 | [Score generation task](#score-generation-task) | `docs/score_generation.md`、`python/tests/test_note*.py` |
| Audio engine #12 | [Audio engine task](#audio-engine-task) | `docs/audio_engine.md`、`core/src/audio_*.rs`、`core/examples/wasapi_smoke.rs` |
| GUI | `gui/OpenKaraoke.Gui/`の対象view/code-behind | `gui/OpenKaraoke.sln`、`docs/architecture.md`。現在はbootstrap shell |
| Build / CI | 対象manifest、`scripts/check.ps1`、`.github/workflows/` | `docs/development.md`、`docs/testing.md` |
| 未着手の機能 | 対象Issueと`docs/implementation_plan.md`から担当領域を特定 | その領域の仕様節だけ確認。存在しないsourceを推測しない |

## Lyrics task

歌詞処理の修正を行う場合は、Issue #7と現在のコードを先に照合する。Issueのopen/closedだけで
実装有無を判定しない。

- Goal: Whisper候補生成、48 kHz整数タイムライン、未校正なconfidence/evidence保持、モデル差し替え可能なAdapter。
- Current implementation: `lyrics.py`、`whisper_backend.py`、`lyric_evaluator.py`、`python/tests/test_lyric*.py`。
- Contracts: `core/src/song_data.rs`、`core/tests/lyric_contract.rs`、`docs/lyric_transcription.md`。
- Validation: 合成fixturesとモデル起動は実録歌唱CERの証明にならない。実曲での性能・閾値は別途評価する。
- Scope: 変更対象のファイルと対応テストに絞り、音源分離・GUI・他の解析モデルへ不用意に広げない。

## Pitch task

Pitchコードの修正時の入口。Issue #9とコード/CIの現在状態を再確認する。

- Goal: 分離済みvocalsから48 kHz整数timelineへ正規化したF0を生成し、detector単体とensembleを切替可能にする。
- Required: `PitchDetector`共通契約、detector confidence、voiced probability、octave consistency、temporal continuity、evidence保存。
- Acceptance: `pitch.json`と`pitch_evidence.json`、single/ensemble設定、合成tone sweep・octave・voiced/unvoiced回帰、detector別/ensemble評価。
- Working set: `pitch.py`、`pitch_detectors.py`、`pitch_fusion.py`、`pitch_evaluator.py`、`test_pitch*.py`。registry変更時のみ`service.py`と`test_service.py`。
- Accuracy boundary: 合成fixtureはアルゴリズム回帰用。実録歌唱の精度確認として扱わない。

## Vocal Event task

Vocal Eventコードの修正時の入口。Issue #10とPitch #9のコード/CIの現在状態を確認する。

- Goal: F0/VUV、音響onset、energy/spectral、歌詞alignment、将来のnote onsetを同一48 kHz timelineへ投影する。
- Required: 各特徴量の独立evidence、note/lyric境界score、single/ensemble比較、低品質・legato/melisma回帰。
- Working set: `vocal_events.py`、`vocal_event_features.py`、`vocal_event_fusion.py`、`vocal_event_evaluator.py`、`test_vocal_event*.py`。
- Accuracy boundary: scoreは0..1だが未校正。合成/注釈fixtureを実歌唱精度として扱わない。

## Score generation task

譜面生成コードの修正時の入口。Issue #11を確認し、Pitch #9とVocal Event #10の現在の共有契約を確認する。

- Goal: F0とVocal Eventを統合し、48 kHz整数timelineの採点用notes.jsonを生成する。
- Required: F0/Eventの同一snapshot検証、交換可能なcandidate provider / optimizer、key/scale soft prior、歌詞・反復evidence hook、expressive motionの過分割抑制。
- Working set: `notes.py`、`note_candidates.py`、`note_optimizer.py`、`note_evaluator.py`、`test_note*.py`。registry変更時のみ`service.py`と共有契約test。
- Candidate boundary: repository-local baselineはF0/Event由来。Basic Pitchやimported MIDIはversioned candidate artifact越しに追加し、未審査のmodel/runtime/weightsを暗黙にbundleしない。
- Accuracy boundary: committed hand-authored fixtureは合成F0/Event回帰。実録歌唱・実譜面corpusでの精度は別途検証する。

## Audio engine task

Issue #12の入口。GUIは関与せず、`core/src/audio_engine.rs`のcallbackをmockで先に検証する。

- Goal: Windows/WASAPI sharedで48 kHz stereo再生とraw monoマイク入力、整数sample clock、ring buffers、underrun/overrunを提供。
- Required: callbackにロック・I/O・alloc・Pythonなし。Core公開API、デバイス列挙、明示的な再オープン境界、normalized WAV非RT feeder。
- Working set: `core/src/audio_engine.rs`、`audio_device.rs`、`audio_wav.rs`、`core/examples/wasapi_smoke.rs`、`.github/workflows/audio-engine-regression.yml`。API route変更時のみ`core/src/lib.rs`。
- Accuracy boundary: モックloopback/長時間反復は物理デバイスの同期や遅延を証明しない。実機での聴取・hotplug/latency計測をUnverifiedのまま残す。

## Validation

開発依存は[development.md](development.md)の手順で準備する。毎回の局所修正で再installしない。
依存の追加・更新時は必要なinstall/lockfile更新を行う。以下は作業中の入口であり、CI削減の許可ではない。

| 変更 | 最初に実行する検証 | 広げる条件 |
|---|---|---|
| 文書だけ | `git diff --check`、リンク・パス・見出し・SHA・ルーティング先の存在確認 | API例や実行コマンドを変更したら対応する実行検証も追加 |
| Song Data | `cargo test --locked -p open-karaoke-core song_data::tests` | 保存形式・共有契約ならRust全体と利用側 |
| Audio import | `cargo test --locked -p open-karaoke-core audio_import::tests` | コマンド/変換を変えたら実FFmpegと出力WAVの検証も必要 |
| Audio clock #40 | `cargo test --locked -p open-karaoke-core --test audio_clock` | #12実機loopbackと遅延p95は別途検証。Epochや公開APIを変えたらRust全体へ広げる |
| Audio engine | `cargo test --locked -p open-karaoke-core audio_engine::tests` と `audio_wav::tests` | WindowsのCPALコンパイル・実機起動は別途確認し、音声callbackの変更はfull CIまで広げる |
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
