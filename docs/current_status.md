# 現在の実装状況

> 最終確認: 2026-10-14 / `main` `ae11c63175a6bc35245957bb7b13fa36ec2b5ffb`
>
> これは **ソースコードと登録処理、テスト構成を読んだ時点のスナップショット** です。最新状態は必ず対象ブランチ・Issue・CIを再確認してください。「実装あり」は実機での動作、商用配布、実際の歌唱精度が検証済みという意味ではありません。

## 状態の読み方

- **実装あり**: データ処理・API・Adapter等のコードとテストが存在する。完全な製品機能とは限らない。
- **土台のみ**: API/画面/契約はあるが、利用者が一連の機能を使うには追加実装が必要。
- **未実装**: 対象処理を担う実装が確認できない。設計・Issueがあっても実装扱いにしない。
- **実録評価未完**: 合成fixtureやモデルの起動テストはあるが、権利処理済み実歌唱データによる品質保証は未完。

## コンポーネント別

| 領域 | 現在の状態 | 実装・詳細 |
|---|---|---|
| Rust Core / Job API | 実装あり（メモリ内Job） | `core/src/lib.rs`、[Core API](core_api.md) |
| Python子プロセス起動・監視 | 実装あり | `core/src/analysis_service.rs`、[Analysis Service](analysis_service.md) |
| 曲データJSON / migration | 実装あり | `core/src/song_data.rs`、[Song Format](song_format.md) |
| 入力変換 / 48kHz正規化 | 実装あり（FFmpeg実行ファイルが必要） | `core/src/audio_import.rs`、[Audio Import](audio_import.md) |
| ボーカル分離 | UMX-HQ Adapter実装あり / 実録評価未完 | `stems.py`、[Stem Separation](stem_separation.md) |
| 歌詞候補生成 | Whisper Adapter実装あり / 実録評価未完 | `lyrics.py`、[Lyric Transcription](lyric_transcription.md) |
| 歌詞整列・参照歌詞 | 日本語CTC Adapter実装あり / 実録評価未完 | `alignment.py`、[Lyric Alignment](lyric_alignment.md) |
| オフラインF0 | YIN・NACFの切替/合議実装あり / 実録評価未完 | `pitch.py`、[Pitch Analysis](pitch_analysis.md) |
| Vocal Event | 境界evidence/fusion実装あり / 未校正 | `vocal_events.py`、[Vocal Events](vocal_events.md) |
| 採点用譜面生成 | 候補生成・時系列DP基礎実装あり / 実録評価未完 | `notes.py`、[Score Generation](score_generation.md) |
| 楽曲全体の一括解析 | API上の`song`ロールのみ、既定Adapterなし | `service.py` |
| Python以外のリアルタイム音声 / マイク / DSP / 採点 | 未実装（設計とIssueあり） | [Architecture](architecture.md)、[Scoring](scoring.md) |
| Avalonia GUI | 起動可能なbootstrap shellのみ | `gui/OpenKaraoke.Gui/`、[Issue #15](https://github.com/tomiya7688/Open_karaoke/issues/15) |
| 一般ユーザー向けランタイム同梱・モデル追加UI | 未実装 | [Development](development.md)、[Model Plugins](model_plugins.md) |

`python/src/open_karaoke_analysis/service.py` は現在、`mock`、`stems`、`lyrics`、`alignment`、`pitch`、`events`、`notes` のAdapterを登録します。`song` はロールとして受け付けても既定Adapterがなく、`not_implemented` になります。

**個別の解析Jobが動くことと、原曲を読み込むだけで歌えるカラオケが完成していることは別です。** 解析成果物の統合、再生・採点・GUI操作、配布が今後の主な作業です。

## テストが証明している範囲

`.github/workflows/ci.yml` には、Rust/Pythonの静的解析・テスト、Core–Pythonのプロセス統合テスト、Avaloniaビルド等があります。別にstem、lyric、alignment、pitch、Vocal Event、noteのモデル/合成回帰用workflowがあります。CIの実行結果・成功可否は各runで確認してください。

- **合成fixture / 音響モデルの起動テスト**: API契約、算術、境界、リグレッションを確認するもの。
- **実モデルsmoke**: 対象重みのロードと推論経路を確認するもの。対象音源が生成トーンなら実歌唱精度の証明ではない。
- **実録歌唱の精度評価**: 権利処理済みの正解付き音源・譜面と比較して評価する必要があり、現在のドキュメント上では完了していない。
- **1.0.0配布評価**: Windows機材、マイク遅延、長時間稼働、正式パッケージと依存ライセンスも別途検証が必要。

詳細は [Testing / CI](testing.md) と各機能のEvaluatorを参照してください。

## 次の入口

実装・残タスクは [Implementation Plan](implementation_plan.md) と [GitHub Issues](https://github.com/tomiya7688/Open_karaoke/issues) を参照してください。Issueの開閉とコードが存在することは一致しない場合があります。Issueを閉じる判断は実装済みかどうかだけでなく、そのIssueの受け入れ条件・実モデル評価等に基づきます。

AI/Agentで作業する場合は [AI_CONTEXT.md](../AI_CONTEXT.md) から、対象Issueに必要なファイルだけを読む手順を使ってください。
