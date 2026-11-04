# 現在の実装状況

> コード再確認: 2026-11-04 / `main` `907d166b2ca0bcfbaf739593a53004d9e0eef02e`。以前の曲登録スナップショットを含む。
>
> ソースとテスト構成のスナップショット。最新の統合・CI結果は対象PRで確認する。
> 既存解析Adapterの項目は以前の監査を引き継ぐ。「実装あり」は実機・配布・実歌唱品質の
> 受け入れ完了とは異なり、今回実曲精度を再評価したという意味ではない。

## コンポーネント別

| 領域 | 現在の状態 | 実装・詳細 |
|---|---|---|
| Rust Core / Job API | メモリ内Job、同時16件・保持512件・JSON64KiB | `core/src/lib.rs`、[Core API](core_api.md) |
| Coreセッション認証 | 起動元stdinパイプから秘密を受け取りBearer認証。単一信頼セッション | `session.rs`、[Desktop session](desktop_session.md) |
| Python子プロセス監視 | 起動・復旧・Core終了時のworker停止あり | `analysis_service.rs`、[Analysis Service](analysis_service.md) |
| 曲データJSON / migration | 実装あり | `song_data.rs`、[Song Format](song_format.md) |
| 入力変換 / 48kHz正規化 | 実装あり。FFmpeg必要、全曲Jobへの接続は未完 | `audio_import.rs`、[Audio Import](audio_import.md) |
| ボーカル分離 | UMX-HQ Adapterあり / 実録評価未完 | `stems.py`、[Stem Separation](stem_separation.md) |
| 歌詞候補生成 | Whisper Adapterあり / 実録評価未完 | `lyrics.py`、[Lyric Transcription](lyric_transcription.md) |
| 歌詞整列・参照歌詞 | 日本語CTC Adapterあり / 実録評価未完 | `alignment.py`、[Lyric Alignment](lyric_alignment.md) |
| オフラインF0 | YIN・NACFの切替/合議あり / 実録評価未完 | `pitch.py`、[Pitch Analysis](pitch_analysis.md) |
| Vocal Event | 境界evidence/fusionあり / 未校正 | `vocal_events.py`、[Vocal Events](vocal_events.md) |
| 採点用譜面生成 | 候補生成・DP・観測窓区間変換あり / 実録評価未完 | `notes.py`、[Score Generation](score_generation.md) |
| 楽曲全体の一括解析 | 正規化済みWAV→6段解析→SongBundle/asset一覧 | `song.py`、[Full-song analysis](full_song_analysis.md)、#36 |
| 音声込み曲登録・一覧 | 完了済み解析Jobから検査・一括公開、再登録確認、永続化、一覧/詳細APIあり | `song_api.rs` / `song_library/`、[SongStore import](song_import.md)、#36 |
| Rust音声入出力 | 48kHz WASAPI shared・raw mic・bounded ring/WAV feederあり。PCMにepochとsample位置を付与 / 実機未検証 | [Audio engine](audio_engine.md)、#12 |
| Clock/latency契約 | 整数変換・不連続観測・旧epoch PCM除外・drift/遅延補正の合成テストあり / 物理校正未完 | [Audio clock](audio_clock.md)、#40 |
| Mixer / DSP | 音量・エコー・リバーブ・bypass・飽和制限のサンプル処理あり。デバイス接続・API・GUI・物理遅延測定は未実装 | `mixer.rs`、[Audio engine](audio_engine.md)、#13 |
| リアルタイム採点 | 未実装。リアルタイムRust pitch detector / per-note scoring / GUIフィードバック未接続 | [Scoring](scoring.md)、[Karaoke session](karaoke_session_design.md)、#14 |
| MIDI/外部譜面変換 | PythonのSMF reader・score Importer API・合成fixtureテストあり。ただしGUI/曲ライブラリへ一括取込する完成フローは別 | `midi_import.py`、`score_import.py`、[MIDI import](midi_import.md)、#37 |
| Avalonia GUI | 認証付き起動・設定・接続・mock Job進捗/キャンセル/error | [Desktop client](desktop_client.md)、#15 |
| GUIの原曲取込・曲一覧操作 | 未接続。Core登録APIと画面の完成を区別 | #36/#15 |
| カラオケ再生・歌詞・音程バー画面 | 曲一覧・曲データ取得と、指定位置に同期した歌詞・目標音程バー表示あり。伴奏再生・Core再生位置への追従・マイク音程・採点結果は未実装 | [Desktop client](desktop_client.md)、#16 |
| モデルパッケージ検出 | `models/<id>/manifest.json` を読むだけで検出し、利用可否・不正を一覧（`GET /model-packages`）。roleごとの選択を `model_selection.json` に保存・解除（`GET`/`POST /model-selection`、選べないIDは拒否）、GUIに更新・選択・保存の操作あり。保存した選択は、`model_id` 未指定のJobで反映（明示指定が優先、使えない選択は既定へ戻し `model_source` で報告、packageはmanifestの`backend`へ対応）。songの各段への反映・Coreの新フィールド転送・ZIPインポート・Registryは未実装 | `model_packages.py`、`model_selection.py`、[Model Plugins](model_plugins.md)、#17 |
| ランタイム同梱・モデル追加UI | 一般ユーザー向けの自動導入と初回セットアップは未実装 | [Development](development.md)、[Model Plugins](model_plugins.md)、#39 |
| 評価データ・歌詞CER | manifest/権利参照/hash/split検証、歌詞CERの私有データ集計、合成CI回帰は実装あり。他タスクの実録精度ゲートは未完成 | [Dataset evaluation](dataset_evaluation.md)、[Testing](testing.md)、#42/#18 |

`service.py` の既定roleはmock / stems / lyrics / alignment / pitch / events / notes / song。
全曲解析はsong-pipeline-v1で個別Adapterを同じworker内で実行する。
モデルが登録されていてもruntime・重みがなければ実解析は失敗する。

**解析Job完了、登録Job完了、実際に歌える製品の完成は別です。**
解析結果はworker artifact領域、登録曲は音声込みでsongs領域へ保存する。
登録曲は再起動後も読めるが、Job履歴は従来通り再起動で失われる。

## 検証の区別

通常CIはRust/Pythonの静的解析・テスト、Core–Python結合、Avaloniaビルドを行う。
desktop workflowはC#→実Core→実Pythonと、headless実XAML・操作イベント・描画を検査する。
モデル/合成回帰workflowも別にある。実行結果は各runで確認する。

Full-song regressionはpipelineの失敗・キャンセル・共有時刻/hash・実下流アルゴリズムを検査する。
SongStore import regressionはassetコピー/検査、キャンセル/確定境界、再登録、破損、一覧、保存復元と、
Python出力を本番Rust処理で音声込み登録する結合試験を行う。全モデル実歌唱評価とは別。

headless描画とOS実ウィンドウ/IME/物理入力、APIテストと物理音声/遅延/長時間稼働を混同しない。
正式配布物・ライセンスの受け入れ確認も別途必要。

## 2026-11-04 横断設計上の残課題

- 個々のAudioIo/WAV feeder/MixerとGUI描画はあるが、**再生から採点結果までを所有するCore歌唱セッション**が未実装。[Karaoke session design](karaoke_session_design.md)、#107。
- 解析結果は構造的に検証するが、低信頼区間の**限定的な自動再解析・信頼度の校正・best-effort表示**は未完成。[Accuracy & UX design](quality_and_ux_design.md)、#108。
- 保存したrole別モデル選択は、現時点で`analysis.song`内部の各解析段階へ適用されない。manifestが利用可能でも別重みを実行している証明にはならない。#109。
- 実機での音声遅延、デバイスの安定ID、初回起動時のモデル準備とマイクチェックは未完成。#40/#110。
- 語句・時刻・譜面の品質は実録歌唱ベースラインで未校正。通常のCIがgreenでも実歌唱精度を保証しない。
- PR [#95](https://github.com/tomiya7688/Open_karaoke/pull/95)（Core再接続/版）と [#77](https://github.com/tomiya7688/Open_karaoke/pull/77)（Evaluator集計）は、この確認時点で**未マージ**。mainの既存機能として記載しない。

## 次の入口

実装順序は [Implementation Plan](implementation_plan.md)。
#36の原音正規化・GUI取込、MIDI入力#37、採点#14、カラオケ画面#16、同梱配布#39をつなぐ。
#41ではクライアント別ACL、購読/接続資源制限、再起動後のJob状態などが残る。
AI作業は [AI_CONTEXT.md](../AI_CONTEXT.md) から対象仕様・source・matching testsへ絞る。
