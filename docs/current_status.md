# 現在の実装状況

> 認証・GUIの更新確認: 2026-10-17 / PR #47、コード `f0c4ea0a56aad94ecdcb163b3496d4739a0e7216`
>
> これはソースとテスト構成のスナップショット。最新の統合・CI結果は対象PRで確認する。
> 解析Adapter等の既存項目は2026-10-14の監査を引き継ぎ、今回実曲精度を再評価したという意味ではない。
> 「実装あり」は実機・配布・実歌唱品質の受け入れ完了とは異なる。

## コンポーネント別

| 領域 | 現在の状態 | 実装・詳細 |
|---|---|---|
| Rust Core / Job API | 実装あり。メモリ内Job、同時16件・保持512件・JSON64KiB | `core/src/lib.rs`、[Core API](core_api.md) |
| Coreセッション認証 | 起動元のstdinパイプから秘密を受け取りBearer認証。単一信頼セッション | `core/src/session.rs`、[Desktop session](desktop_session.md) |
| Python子プロセス起動・監視 | 実装あり。Core終了でworkerを停止 | `core/src/analysis_service.rs`、[Analysis Service](analysis_service.md) |
| 曲データJSON / migration | 実装あり | `core/src/song_data.rs`、[Song Format](song_format.md) |
| 入力変換 / 48kHz正規化 | 実装あり。FFmpeg実行ファイルが必要 | `core/src/audio_import.rs`、[Audio Import](audio_import.md) |
| ボーカル分離 | UMX-HQ Adapterあり / 実録評価未完 | `stems.py`、[Stem Separation](stem_separation.md) |
| 歌詞候補生成 | Whisper Adapterあり / 実録評価未完 | `lyrics.py`、[Lyric Transcription](lyric_transcription.md) |
| 歌詞整列・参照歌詞 | 日本語CTC Adapterあり / 実録評価未完 | `alignment.py`、[Lyric Alignment](lyric_alignment.md) |
| オフラインF0 | YIN・NACFの切替/合議あり / 実録評価未完 | `pitch.py`、[Pitch Analysis](pitch_analysis.md) |
| Vocal Event | 境界evidence/fusionあり / 未校正 | `vocal_events.py`、[Vocal Events](vocal_events.md) |
| 採点用譜面生成 | 候補生成・時系列DP基礎あり / 実録評価未完 | `notes.py`、[Score Generation](score_generation.md) |
| 楽曲全体の一括解析 | `song`ロールのみ、既定Adapterなし | `service.py`、Issue #36 |
| Rust音声入出力 | 48kHz WASAPI shared、raw mic、bounded ring/WAV feederあり / 実機未検証 | [Audio engine](audio_engine.md)、Issue #12 |
| Clock/latency契約 | 整数変換・epoch・drift/遅延補正の合成テストあり / 物理校正未完 | [Audio clock](audio_clock.md)、Issue #40 |
| Mixer / DSP / リアルタイム採点 | 未実装 | [Scoring](scoring.md)、Issue #13/#14 |
| Avalonia GUI | 認証付き起動・設定・接続・mock Job進捗/キャンセル/errorあり | [Desktop client](desktop_client.md)、Issue #15 |
| カラオケ再生・歌詞・音程バー画面 | 未実装。接続テスト画面と区別 | Issue #16 |
| ランタイム同梱・モデル追加UI | 未実装 | [Development](development.md)、[Model Plugins](model_plugins.md)、Issue #39 |

`service.py`の既定Adapterは `mock`、`stems`、`lyrics`、`alignment`、`pitch`、`events`、`notes`。
`song` は受け付けるroleでも既定Adapterがなく、`not_implemented` になる。

**GUIの接続・mock Job成功と、原曲を読み込むだけで歌える製品の完成は別です。**
次の縦断経路は解析成果物の統合・SongStore登録、再生・採点とそのGUI接続。

## 検証の区別

既存CIはRust/Pythonの静的解析・テスト、Core–Python結合、Avaloniaビルドを行う。
別途モデル/合成回帰workflowがある。新しいdesktop workflowはC#クライアント→実Core→実Pythonと、
headless上の実XAML・操作イベント・描画を検証する。実行結果は各runで確認する。

- API/状態遷移/算術のテストと、実歌唱の精度評価を混同しない。
- Headlessでの画像・操作イベントと、OSの実ウィンドウ・IME・物理入力の検証を区別する。
- 物理音声、遅延、長時間稼働、配布物・ライセンスの受け入れ確認は別途必要。

詳細: [Desktop UI validation](desktop_ui_validation.md)、[Testing / CI](testing.md)。

## 次の入口

優先度と依存は [Implementation Plan](implementation_plan.md)。
全曲解析/登録#36、MIDI入力#37、採点#14、カラオケ画面#16、同梱配布#39をつなぐ。
API #41ではクライアント別ACL、購読/接続資源制限、再起動後の履歴等が残る。
Issueの開閉だけで実装有無や品質を判定せず、受け入れ条件と検証を照合する。
AI作業は [AI_CONTEXT.md](../AI_CONTEXT.md) から必要なファイルに絞る。
