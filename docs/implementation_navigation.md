# 実装者向け編集マップ

> 2026-11-04 / 確認元 `main` `907d166b2ca0bcfbaf739593a53004d9e0eef02e`。
> **ここは入口の索引であり、設計書やIssueの代わりではない。** PRに未統合の変更があるため、作業直前に対象Issue・PR・HEADを確認する。
>
> 目的：Issueから編集する型・ファイルへ直行する。**全docs・全sourceを読む運用をやめる。**

## まず処理全体の流れを理解する

```text
【曲を準備】（個別処理は実装あり、使いやすい一括導線は整備中）

ユーザー音源 ─→ Rust Core音源取込・48kHz変換
                        │
                        ▼
                 Python analysis.song
                 ┌────────────────────────────────────────┐
                 │ Stems → Lyrics → Alignment → Pitch     │
                 │                              ↓         │
                 │                          Vocal Events  │
                 │                              ↓         │
                 │                          Notes生成     │
                 └────────────────────────────────────────┘
                        │
                        ▼
                 SongBundle（版・入力hash・証拠）
                        │
               Rust song.import Job
                        │
                        ▼
                   SongLibrary
                        │
                        ▼
【歌唱】（以下の統合経路は設計済み・未実装）
                 KaraokeSessionController  #107
                   ├→ 保存済み伴奏 → Audio/Mixer → WASAPI
                   ├→ 生マイク入力 ─┬→ 音声監視とDSP → Mixer
                   │                └→ リアルタイムF0 → 採点
                   ├→ ClockSession（再生/入力/GUIの時刻調停）
                   └→ Coreセッション・採点イベント
                                    ↓
                              Avalonia GUI
                         歌詞・音程バー・歌唱線・結果
```

- **正本の境界**：GUIはRust Core公開APIだけを呼ぶ。Pythonは内部の解析APIを担当し、GUIから直接呼ばない。
- **内部時間**：すべて48kHz基準の整数sample。秒、MIDI tick、モデルのhopは入力/出力境界で変換する。
- **解析と歌唱を分ける**：重いPython推論を音声callbackに持ち込まない。採点用マイクはエフェクト前のraw PCM。
- **必ず区別**：`SongBundle`が作れたこと、`SongLibrary`へ登録したこと、実際に歌えること、採点精度が検証済みであることは別。

## Issueから編集箇所を選ぶ

下記のパスはリポジトリ直下基準。表の「入口」から**1〜3ファイル程度**を読み、
対象の型・関数だけを確認して始める。別の境界を変える必要が判明した場合だけ探索を広げる。
`新設`は2026-11-04のmainに存在しない予定のファイル・型であり、存在するように仮定しない。

### 基盤・Core・モデル

| Issue | 最初に読む設計書 | 最初に調べる実装箇所 | 変更する責務と触らない境界 | 最初の検証 |
|---|---|---|---|---|
| [#2](https://github.com/tomiya7688/Open_karaoke/issues/2) Job API | [Core API](core_api.md) | `core/src/lib.rs` の`AppState`・`create_job`・`run_job`・`job_events` | 状態遷移/Job限度。Python AdapterやGUIへJob状態の正本を複製しない | `core/tests/job_api.rs` |
| [#3](https://github.com/tomiya7688/Open_karaoke/issues/3) Song Data | [保存形式](song_format.md) | `core/src/song_data.rs` の`SongBundle`・バリデーション | 保存契約とmigration。API/UI依存の独自形式を作らない | `core/tests/song_bundle_contract.rs` |
| [#4](https://github.com/tomiya7688/Open_karaoke/issues/4) 音源入力 | [Audio Import](audio_import.md) | `core/src/audio_import.rs` | FFmpeg正規化・入力検証。SongStoreの保存方式は変更しない | 同ファイルのテスト、`scripts/check.ps1` |
| [#6](https://github.com/tomiya7688/Open_karaoke/issues/6) Stem | [Stem分離](stem_separation.md) | `python/src/open_karaoke_analysis/stems.py`・`stem_backend.py` | モデルAdapter/重み/分離。Core APIにはモデル専用処理を入れない | `python/tests/test_stems.py`・`test_stem_api.py` |
| [#7](https://github.com/tomiya7688/Open_karaoke/issues/7) 歌詞認識 | [歌詞認識](lyric_transcription.md) | `python/src/open_karaoke_analysis/lyrics.py`・`whisper_backend.py` | 歌詞候補/evidence。注釈済み時刻や最終譜面を捏造しない | `python/tests/test_lyrics.py`・`test_lyric_api.py` |
| [#8](https://github.com/tomiya7688/Open_karaoke/issues/8) 歌詞整列 | [Alignment](lyric_alignment.md) | `python/src/open_karaoke_analysis/alignment.py`・`ctc_alignment.py` | 参照歌詞と音声時刻の整列。Vocal Eventは現行では下流 | `python/tests/test_alignment.py`・`test_alignment_reference.py` |
| [#17](https://github.com/tomiya7688/Open_karaoke/issues/17) モデル選択 | [モデル拡張](model_plugins.md) | `python/src/open_karaoke_analysis/model_packages.py`・`model_selection.py` | manifest発見、ロールごとの選択。manifestを読んだだけで未知のPythonコードを実行しない | `python/tests/test_model_packages.py`・`test_model_selection.py` |
| [#36](https://github.com/tomiya7688/Open_karaoke/issues/36) 全曲解析/登録 | [全曲解析](full_song_analysis.md)、[登録](song_import.md) | `python/src/open_karaoke_analysis/song.py`の`SongAdapter`、`core/src/song_library/mod.rs`の`SongLibrary::import` | 解析の結線と登録の2段階。片方を完了しただけで曲を利用可能と扱わない | `python/tests/test_song.py`・`core/tests/song_import.rs` |
| [#37](https://github.com/tomiya7688/Open_karaoke/issues/37) 外部MIDI | [MIDI](midi_import.md)、[音符生成](score_generation.md) | `python/src/open_karaoke_analysis/midi_import.py`・`score_import.py`、`song.py`の入力契約 | SMFと外部候補の検証。全曲`stage_options.*_artifact`の所有権保護を外さない | `python/tests/test_midi_import.py`・`test_song.py` |
| [#41](https://github.com/tomiya7688/Open_karaoke/issues/41) Core API保護 | [Core API](core_api.md)、[Desktop session](desktop_session.md) | `core/src/session.rs`の`CoreSession`、`core/src/lib.rs`の公開Router | 認証・版管理・再接続。Python workerの内部認証と混同しない。**未統合PR #95の差分を先に確認** | `core/tests/job_api.rs`・`python/tests/test_core_job_client.py` |
| [#109](https://github.com/tomiya7688/Open_karaoke/issues/109) 実効モデル固定 | [解析品質・UX](quality_and_ux_design.md)、[モデル選択](model_plugins.md) | `song.py`の`SongAdapter.analyze`・`model_selection.py`の`resolve_selection` | 保存済み選択を全曲の各段階へ適用、モデル実体のhashを記録。model loaderへライセンス硬制限を作らない | `python/tests/test_model_selection_jobs.py`・`test_song.py` |

### 音声・歌唱・画面

| Issue | 設計書 | 編集入口 | 変更する責務／禁止する変更 | 最初の検証 |
|---|---|---|---|---|
| [#13](https://github.com/tomiya7688/Open_karaoke/issues/13) 監視/DSP | [音声エンジン](audio_engine.md)、[歌唱セッション](karaoke_session_design.md) | `core/src/mixer.rs`の`Mixer`と`core/src/audio_engine.rs`の`AudioIo` | 音量・エコー・リバーブ。採点側へエフェクト後音声を流さない | `core/tests/mixer.rs` |
| [#14](https://github.com/tomiya7688/Open_karaoke/issues/14) リアルタイム採点 | [採点](scoring.md)、[歌唱セッション](karaoke_session_design.md) | `core/src/audio_engine.rs`の`read_microphone_timestamped`。**RealtimePitchDetector/ScoringEngineは新設** | raw入力→時刻付きF0→音符評価。オフラインPython F0と混ぜない | 新設`core/tests/realtime_scoring.rs`等（実装PRで確定） |
| [#15](https://github.com/tomiya7688/Open_karaoke/issues/15) GUI/Core接続 | [デスクトップ](desktop_client.md)、[Core API](core_api.md) | `gui/OpenKaraoke.Client/CoreApiClient.cs`・`CoreConnection.cs` | API接続・状態表示。UIからPythonやSongStoreのファイルへ直接アクセスしない | `gui/OpenKaraoke.Client.Tests/NativeTests.cs` |
| [#16](https://github.com/tomiya7688/Open_karaoke/issues/16) 歌唱画面 | [歌唱セッション](karaoke_session_design.md)、[画面検証](desktop_ui_validation.md) | `gui/OpenKaraoke.Client/KaraokeViewModel.cs`・`SongTimeline.cs` | 現在/次の歌詞、目標音程・歌唱線。GUIが自分で再生clock/採点を決めない | `gui/OpenKaraoke.Client.Tests/SongTests.cs`・`gui/OpenKaraoke.Gui.Smoke/Program.cs` |
| [#40](https://github.com/tomiya7688/Open_karaoke/issues/40) 時刻同期 | [クロック](audio_clock.md) | `core/src/audio_clock.rs`の`ClockSession`、`audio_engine.rs`の各時刻付きframe | 出力/録音別epochと補正。キュー投入済みsampleを可聴位置と誤認しない | `core/tests/audio_clock.rs`・`audio_clock_observation.rs` |
| [#43](https://github.com/tomiya7688/Open_karaoke/issues/43) AI負荷分離 | [歌唱セッション](karaoke_session_design.md)、[Analysis Service](analysis_service.md) | `core/src/analysis_service.rs`、`python/src/open_karaoke_analysis/service.py` | 解析の並列度・再生中制御。callback内部でモデルを実行しない | `core/tests/job_api.rs`・`python/tests/test_service.py` |
| [#107](https://github.com/tomiya7688/Open_karaoke/issues/107) 歌唱Session | [#107詳細設計](karaoke_session_design.md#107-実装詳細設計オーディオ経路と状態所有) | `core/src/audio_wav.rs`の`WavFeeder`、`audio_engine.rs`の`AudioIo`、`audio_clock.rs`の`ClockSession`。**Controller等は新設** | 唯一のcapture consumerとPCM経路、状態機械。従来WavFeederとMixerの2経路出力を禁止 | 新設`core/tests/karaoke_session.rs`・`karaoke_audio_graph.rs` |
| [#110](https://github.com/tomiya7688/Open_karaoke/issues/110) 初回UX | [解析品質・UX](quality_and_ux_design.md)、[Desktop client](desktop_client.md) | `gui/OpenKaraoke.Client/ShellViewModel.cs`、`CoreApiClient.cs`、必要な独立画面Model | モデル/機器準備と失敗復旧。初期設定をGUIの巨大1クラスへ追加しない | `gui/OpenKaraoke.Client.Tests/NativeTests.cs`・GUI Smoke |
| [#38](https://github.com/tomiya7688/Open_karaoke/issues/38) 任意編集 | [Song Data](song_format.md)、[歌詞整列](lyric_alignment.md) | `core/src/song_data.rs`・`gui/OpenKaraoke.Client/SongTimeline.cs`（新たなEditor API/ViewModel） | 元の解析evidenceと手編集版の分離。既存原曲・音符を破壊的に上書きしない | 新設Editor/保存・Undo/Redo contract tests |

### 品質・配布・その他

| Issue | 設計の正本 | 最初の実装箇所・触らない境界 | 検証の入口 |
|---|---|---|---|
| [#18](https://github.com/tomiya7688/Open_karaoke/issues/18) Evaluator | [Testing](testing.md)、[Core Job Evaluator](core_job_evaluator.md) | `python/src/open_karaoke_analysis/evaluation_summary.py`・`job_runner.py`。実験データを公開CIへ混入させない。PR #77を確認 | `python/tests/test_evaluation_summary.py`・`test_job_runner.py` |
| [#19](https://github.com/tomiya7688/Open_karaoke/issues/19) 正式版評価 | [Testing](testing.md)、[実録評価](dataset_evaluation.md) | 各Evaluator/Windows実機手順の品質ゲート。合成評価成功を実機合格に読み替えない | 各Regression workflowと実機評価 |
| [#42](https://github.com/tomiya7688/Open_karaoke/issues/42) 実歌唱データ | [データセット評価](dataset_evaluation.md) | `python/src/open_karaoke_analysis/dataset_manifest.py`・`lyric_dataset_report.py`。非公開音源をGit管理しない | `python/tests/test_dataset_manifest.py` |
| [#108](https://github.com/tomiya7688/Open_karaoke/issues/108) 品質判定/再解析 | [#108詳細設計](quality_and_ux_design.md#108-実装詳細設計品質判定と限定再解析) | `python/src/open_karaoke_analysis/song.py`を接続点とし、QualityAssessor/RepairPlanner等を**独立モジュールで新設**。無制限の再実行は禁止 | 新設`python/tests/test_repair_planner.py`等 |
| [#39](https://github.com/tomiya7688/Open_karaoke/issues/39) Windows配布 | [Development](development.md)、[Licensing](licensing.md) | `scripts/build.ps1`・`.github/workflows/distribution.yml`。公式配布物だけ依存審査し、ユーザーMODローダーへのNCブロックを作らない | Distribution provenance workflow |
| [#66](https://github.com/tomiya7688/Open_karaoke/issues/66) Python CA | [Python trust](development.md) | `.github/actions/python-trust/action.yml`・`scripts/check_python_trust.py`。既存venvの実CAストア検証を省略しない | `python/tests/test_python_trust.py` |
| [#67](https://github.com/tomiya7688/Open_karaoke/issues/67) 配布署名 | [Licensing](licensing.md)、[Development](development.md) | `.github/workflows/distribution.yml`。秘密鍵をコミットせず、最終配布成果物そのものを検証 | リリース成果物の署名/Attestation確認 |
| [#44](https://github.com/tomiya7688/Open_karaoke/issues/44) 高度ASR研究 | [解析品質・UX](quality_and_ux_design.md) | まず`lyric_evaluator.py`/実録評価で差を測る。新モデルをいきなり公式必須経路へ入れない | `python/tests/test_lyric_evaluator.py`・実録holdout |

Issue #58（PR整理）はコード設計の実装Issueではなく、PR/CIと依存関係の棚卸しで完了判定する。
Issue #106は本索引・詳細設計・次の実装担当への引き継ぎを担当する。

## 実装者がやる順番：読むのを増やさない

1. **Issueと重複PR**：番号とAcceptance、変更対象が他者のブランチにないかを確認。
2. **本書の該当1行**で設計正本と実装入口、**触らない責務**を把握する。
3. **設計書の該当節を先に編集**する。必要な型・操作・データ契約・失敗/キャンセル・テストの変更を決める。
4. **sourceはシンボル名で検索してその近傍だけ読む**。モデル重み、全Issue、全docs、無関係な経路へ最初から入らない。
5. コードを編集しながら設計書を更新し、**同じ契約を担うテスト**を追加する。
6. `pwsh scripts/check_local.ps1`で対象検証を行い、最新HEADの該当CIへ進む。実機/実モデル未検証を成功扱いしない。

**探索を増やしてよい条件**：実際の契約が正本と食い違う、変更の影響がAPI/保存形式/他コンポーネントへ及ぶ、セキュリティ・ライセンス・テスト失敗が発生した場合だけ。理由と追加ファイルをPRへ記録する。

## コメントと設計書の対応

新規・変更した**クラス・メソッド・関数**には [コーディングルール](coding_rules.md#5-ソースコードコメント)の
JSON-like宣言コメントを記載し、`責務`、`フィールド`（状態を持つ型）、`処理`、`引数`、`戻り値`を
実コードと一致させる。コメントは設計書の代わりではない。
既存のコメント不足は[コメント準拠監査](comment_coverage.md)を参照する。既存の負債を、
対象に無関係な大規模書換えとして混ぜず、**触る宣言から修正**する。
