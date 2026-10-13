# Quality Evaluation and CI

## この文書の読み方

機能を実装する際は、その機能の**再現可能な自動テスト/Evaluator**も用意する。
ただし、CIのgreen表示には複数の意味があるため、下記の段階を混同しない。

1. **Contract / static checks**: コード、API、保存形式、キャンセルなどが仕様通りか。
2. **Synthetic regression**: 手製の波形・タイムラインで数値処理が期待通りか。
3. **Real-model smoke**: 実際のモデル重みを読み、推論と入出力が動くか。
4. **Real-singing accuracy**: 権利処理済みの人声/合成歌唱・正解歌詞・正解譜面に対する精度。
5. **Release evaluation**: 店舗利用を想定したWindows実機、遅延、長時間稼働、配布物・依存ライセンスの検査。

**現時点で1〜3の検証コード・workflowがあることは、4〜5の合格を意味しない。**
実装ごとの未検証範囲は [現在の実装状況](current_status.md) に集約する。

## CIの入り口

| Workflow | 対象 | 確認するもの |
|---|---|---|
| [CI](../.github/workflows/ci.yml) | push/PR等 | Rust/Python lint・format・テスト、Core/Pythonプロセス統合、Avaloniaビルド、依存inventory |
| [Stem model integration](../.github/workflows/stem-model.yml) | 該当PR/手動 | UMX-HQの重み・推論・cacheとsynthetic smoke |
| [Lyric transcription](../.github/workflows/lyric-model.yml) | 該当PR/手動 | Whisper関連contract・実モデルsmoke |
| [Lyric alignment](../.github/workflows/alignment-model.yml) | 該当PR/手動 | 日本語CTCのcontract・実モデルsmoke |
| [Pitch regression](../.github/workflows/pitch-regression.yml) | 該当PR/手動 | YIN/NACFの合成F0精度・合議 |
| [Vocal Event regression](../.github/workflows/vocal-event-regression.yml) | 該当PR/手動 | 合成注釈境界のprecision/recall/F1 |
| [Note generation regression](../.github/workflows/note-regression.yml) | 該当PR/手動 | 合成採点ノートのpitch F1・onset/offset等 |

Path filterがあるworkflowは**全変更で必ず走るわけではない**。変更したファイルと各workflowのトリガー、対象コミットの実行結果を確認する。
この文書はCIの実行成功を報告するものではない。

## 機能別指標

| 機能 | 主な指標 | 実データ評価の前提 |
|---|---|---|
| Stem Separation | SI-SDR、分離後SDR等 | 正解vocals/accompanimentのある音源 |
| 歌詞認識 | CER/WER、幻覚/反復 | 正解歌詞・音声・言語/ジャンル情報 |
| Alignment | mean/p95境界誤差、coverage | 人手で付与した時間境界 |
| Pitch | cent誤差、voicing、octave error | 正解F0/voicing |
| Vocal Event | 境界precision/recall/F1 | 注釈済みnote・lyric境界 |
| Score Generation | note F1、onset/offset誤差、octave error | 人手検証した理想譜面 |
| Realtime Scoring | cent・タイミングずれに対する単調性、人手評価相関 | マイク歌唱・機材遅延・基準譜面 |
| Commercial release | 依存ライセンス・同梱重み・長時間稼働 | 実配布バイナリ、実機、権利処理済みデータ |

CI上の合成fixture閾値はアルゴリズム回帰防止用であり、店頭利用できる品質の合格閾値ではない。
機能ごとの確定済みコマンドと個別制約は[Stem](stem_separation.md)、
[ASR](lyric_transcription.md)、[Alignment](lyric_alignment.md)、
[Pitch](pitch_analysis.md)、[Vocal Events](vocal_events.md)、
[Score Generation](score_generation.md)に記載する。

## 開発者向けの実行

Windows PowerShell 7.4+、依存インストール後にリポジトリ直下から:

```powershell
./scripts/check.ps1
./scripts/dependency-inventory.ps1
```

Linux用のcontract checksや実モデルworkflowは対応するGitHub Actionsを参照する。
依存inventoryはライセンス審査の**入力情報**であり、生成しただけで全依存の利用・再配布条件を法的に確認できたことにはならない。

## Evaluatorの出力契約

各Evaluatorは結果メトリクス、利用データ/モデルのバージョンまたは識別情報、
品質閾値が適用されたかどうか、pass/failを追跡できる形で保存する。

現在、個別EvaluatorのJSONはそれぞれの仕様で定義されており、**全Evaluatorで同一JSON schemaが強制されているわけではない**。
共通フォーマットとベースライン比較の統合は [Issue #18](https://github.com/tomiya7688/Open_karaoke/issues/18) の対象。

GUIなしでのCI実行はJob APIまたは既存のEvaluator CLIによって可能にする。現在の評価器の多くは保存済みartifact/fixtureを直接読み込むCLIであり、必ずJob APIを呼ぶ実装ではない。

## 1.0.0 Release Evaluation（未完了）

正式リリース前に以下を評価する:

- 実際の音源から歌詞/譜面を生成したときの精度と再現性
- 入力音質・ジャンル・歌手・合成歌唱による差
- マイク入力、再生と表示、音声同期、遅延補正
- USB機材抜き差し、長時間連続稼働、復旧
- Windows配布物の同梱ランタイム/モデル/ライセンス・NOTICE
- ユーザーによる手直しなしでの利用率

合格基準値は権利処理済み実評価セットで校正し、計測結果と判定根拠をリリースに紐付ける。
未評価項目を「合格」と記載しない。詳細は [Issue #19](https://github.com/tomiya7688/Open_karaoke/issues/19)。
