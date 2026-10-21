# Implementation Plan

## この文書の役割

この文書は**実装順序とIssueへの導線**です。現在使える機能の判定は
[Current status](current_status.md)、具体的な実行手順は
[Development](development.md) を参照してください。

0.1.x系は小さくリリースしてよい一方、1.0.0は[Requirements](requirements.md)にある主要機能を一通り実装し、
権利・モデル重み・遅延・実録歌唱精度を含むRelease Evaluationを通過する必要があります。

## GitHub Issueの優先度（2026-10-14）

Issueには `priority:P1`・`priority:P2`・`priority:P3` のラベルを付ける。

| 優先度 | 意味 | 方針 |
|---|---|---|
| **P0（0件）** | 緊急／開発を止める級の問題用 | **予約。今回の計画では割り当てない** |
| **P1（9件）** | 先に定義しないと手戻りが大きい設計、E2E・評価基盤 | 原則先行して着手する。ただし依存Issueは先に確認 |
| **P2（12件）** | 1.0.0に必要な通常作業・既存実装の受け入れ確認 | P1の基盤に合わせて実装・検証する。必要なら並行可能 |
| **P3（2件）** | E2E後でよい編集UX・長期研究 | 順序を後ろへ回す。**#38は1.0.0要件**、#44は1.0.0の必須条件ではない |

**優先度は依存関係でも完了条件でもない。** P2であってもP1の前に契約を確認すべき作業がある。
ラベルは**未完了Issueのみ**に付け、closed Issue（#1、#5、#9〜#11等）は実装履歴として扱う。
Issueのopen/closedと実装コードの存在が一致しないケースは
[現在の実装状況](current_status.md) を参照する。

### P1 — 最優先：手戻りを避け、歌える縦断経路を成立させる

| Issue | 重要な理由・着手順 |
|---|---|
| [#40 Audio Clock/Latency](https://github.com/tomiya7688/Open_karaoke/issues/40) | **先行設計**。再生・録音・採点・GUIの時刻同期とclock drift |
| [#41 Local Core API](https://github.com/tomiya7688/Open_karaoke/issues/41) | **先行設計**。API契約、セッション認証、Jobアクセス制限 |
| [#42 Real-singing dataset](https://github.com/tomiya7688/Open_karaoke/issues/42) | **早期着手**。合成fixtureでは計れない実際の精度を評価する基準 |
| [#18 Evaluator/CI](https://github.com/tomiya7688/Open_karaoke/issues/18) | #42と並行。各実装の精度と回帰を継続測定 |
| [#36 Full-song orchestration](https://github.com/tomiya7688/Open_karaoke/issues/36) | **E2Eの中核**。個別解析Jobを繋いでSongStoreへ保存 |
| [#37 External MIDI import](https://github.com/tomiya7688/Open_karaoke/issues/37) | 譜面を直接取り込み、**自動生成を待たずに採点・UIをテスト**できる |
| [#12 Rust Audio Engine](https://github.com/tomiya7688/Open_karaoke/issues/12) | **リアルタイム経路の中核**。#40の設計を反映 |
| [#14 Realtime scoring](https://github.com/tomiya7688/Open_karaoke/issues/14) | #12・#40・譜面契約の後。音程とタイミング、遅延補正を確認 |
| [#15 Avalonia Core client](https://github.com/tomiya7688/Open_karaoke/issues/15) | #41のAPI契約に基づき並行実装。将来GUI変更も可能にする |

### #40の実装単位（P0は空けておく）

| 単位 | 優先度 | 依存 | 完了条件・対象 |
|---|---|---|---|
| #40.1 整数clock mapping契約 | P1 | Song Dataの48 kHz時刻 | #45でmerge済み。独立domain、epoch、未知/推定/実測の遅延区別 |
| #40.2 logical counterの不連続観測 | P1 | #40.1、#12 counter | `audio_clock.rs`で両anchor無効化、同一snapshotの重複処理防止、WASAPI診断例へ接続。`audio_clock_observation.rs`で制御遷移・欠落・error・resetを回帰検証 |
| #40.3 PCMのepochとtimestamp接続 | P1 | #40.2 | capture frameにepochと48 kHz位置を付与。出力PCMにepochとsong位置を付け、古いframeを再生・採点しない。seek/device変更時のfeeder再配置と実機挙動は別途確認 |
| #40.4 実機校正・drift評価 | P1 | #40.3、Windows実機 | [clock契約](audio_clock.md#regression-and-hardware-evaluation)の物理loopback、p95、30分driftと再接続試験 |

#40.2のcounter観測だけで、PCMのepoch分離や実機の校正が完了したとは扱わない。

### #42の実装単位（P0は空けておく）

| 単位 | 優先度 | 依存 | 完了条件・対象 |
|---|---|---|---|
| #42.1 非公開dataset manifest | P1 | #42の権利・分割方針 | [manifest契約](dataset_evaluation.md)とローカル検査CLIでhash・provenance参照・development/holdout漏洩を検査する |
| #42.2 注釈の共通ヘッダー | P1 | #42.1 | 注釈をsample ID・音源hash・48 kHz時刻軸へ結び付け、版・長さ・種別を検査する |

#42.1は実データの権利確認・収集・注釈品質、および#18のmetric報告を完了扱いにしない。

### P2 — 通常優先：1.0.0の主要機能と品質・配布

| Issue | 理由・実装タイミング |
|---|---|
| [#2 Job API](https://github.com/tomiya7688/Open_karaoke/issues/2)、[#3 Song Data](https://github.com/tomiya7688/Open_karaoke/issues/3)、[#4 Audio import](https://github.com/tomiya7688/Open_karaoke/issues/4) | 基礎実装があるため受け入れ条件・契約・結合を確認。**#36より前または並行して確認** |
| [#6 Stem](https://github.com/tomiya7688/Open_karaoke/issues/6)、[#7 Whisper](https://github.com/tomiya7688/Open_karaoke/issues/7)、[#8 Alignment](https://github.com/tomiya7688/Open_karaoke/issues/8) | 基礎Adapterはあるが実録音精度は未評価。#42と共に改善、#36へ供給 |
| [#13 Mixer/Echo/Reverb](https://github.com/tomiya7688/Open_karaoke/issues/13) | #12のオーディオ入出力を土台に実装 |
| [#43 Inference resource budget](https://github.com/tomiya7688/Open_karaoke/issues/43) | 重いAI推論中でも音声を優先。#12の基礎動作後に負荷計測 |
| [#16 Karaoke UI](https://github.com/tomiya7688/Open_karaoke/issues/16) | #12・#14・#15の境界が使える段階で譜面・音声と統合 |
| [#17 Model packages](https://github.com/tomiya7688/Open_karaoke/issues/17) | 非開発者向けモデル追加・選択。公式Registry高度化は後回しで可 |
| [#39 Windows package](https://github.com/tomiya7688/Open_karaoke/issues/39) | ランタイム同梱・商用ライセンス・初回導入。配布技術の調査は早期、完成パッケージは後半 |
| [#19 Release evaluation](https://github.com/tomiya7688/Open_karaoke/issues/19) | **1.0.0の最終ゲート**。準備は#42で早期着手、実行は統合後 |

### P3 — 後回しにできるもの

| Issue | 理由・1.0.0との関係 |
|---|---|
| [#38 Lyrics/Notes editor](https://github.com/tomiya7688/Open_karaoke/issues/38) | 必要時だけ使う編集UX。E2E・自動生成・採点が動いた後に実装してよい。ただし**現在の1.0.0要件に含まれるため正式版前に必要** |
| [#44 Advanced ASR consensus](https://github.com/tomiya7688/Open_karaoke/issues/44) | 複数ASR、Transformer検証、曲単位適応/LoRA等の研究。WhisperベースのE2E・実録評価が揃うまで必須経路に入れない |

## 推奨着手順（優先度に加え依存を考慮）

1. **設計と評価を先行**: #40、#41、#42、#18 を開始。#2/#3/#4の既存契約も確認。
2. **最短の歌えるE2Eを並行開発**: #36（解析連結）、#37（MIDIテスト入力）、#12（CPAL）、#15（GUI API）。
3. **歌唱機能を接続**: #14（採点）、#13（モニタリング/DSP）、#16（歌詞・音程バーUI）。#43でAI負荷中の安定性を確認。
4. **品質と利用可能性を仕上げる**: #6〜#8の実曲改善、#17（モデル管理）、#39（インストール/同梱）、#38（任意編集）。
5. **正式版の判定**: #19で実録音・機材・配布物・ライセンスの評価。合格したら1.0.0。
6. **後続研究**: #44の高度合議や曲固有適応を評価に基づき検討する。

開発者がいない端末での使用・商用利用は製品要件のため、#39とライセンス監査を**1.0.0より後へ延期しない**。

## 実装のまとまり

| 領域 | GitHub Issue | 開発の入口 |
|---|---|---|
| ワークスペース/CI土台 | [#1](https://github.com/tomiya7688/Open_karaoke/issues/1) | [Development](development.md) |
| Core Job API | [#2](https://github.com/tomiya7688/Open_karaoke/issues/2) | [Core API](core_api.md) |
| Song JSON | [#3](https://github.com/tomiya7688/Open_karaoke/issues/3) | [Song Format](song_format.md) |
| 音源読み込み/48kHz | [#4](https://github.com/tomiya7688/Open_karaoke/issues/4) | [Audio Import](audio_import.md) |
| Python Service | [#5](https://github.com/tomiya7688/Open_karaoke/issues/5) | [Analysis Service](analysis_service.md) |
| Stem / 歌詞 / Alignment | [#6](https://github.com/tomiya7688/Open_karaoke/issues/6)、[#7](https://github.com/tomiya7688/Open_karaoke/issues/7)、[#8](https://github.com/tomiya7688/Open_karaoke/issues/8) | [Stem](stem_separation.md)、[ASR](lyric_transcription.md)、[Alignment](lyric_alignment.md) |
| F0 / Vocal Events / 譜面生成 | [#9](https://github.com/tomiya7688/Open_karaoke/issues/9)、[#10](https://github.com/tomiya7688/Open_karaoke/issues/10)、[#11](https://github.com/tomiya7688/Open_karaoke/issues/11) | [Pitch](pitch_analysis.md)、[Vocal Events](vocal_events.md)、[Score Generation](score_generation.md) |
| Rust音声再生・DSP・採点 | [#12](https://github.com/tomiya7688/Open_karaoke/issues/12)〜[#14](https://github.com/tomiya7688/Open_karaoke/issues/14) | [Architecture](architecture.md)、[Scoring](scoring.md) |
| Avalonia GUI / カラオケ画面 | [#15](https://github.com/tomiya7688/Open_karaoke/issues/15)、[#16](https://github.com/tomiya7688/Open_karaoke/issues/16) | `gui/`、[Architecture](architecture.md) |
| モデル選択と拡張 | [#17](https://github.com/tomiya7688/Open_karaoke/issues/17) | [Model Plugins](model_plugins.md) |
| Evaluator / Release gates | [#18](https://github.com/tomiya7688/Open_karaoke/issues/18)、[#19](https://github.com/tomiya7688/Open_karaoke/issues/19) | [Testing / CI](testing.md)、[Licensing](licensing.md) |
| 個別解析から曲全体を登録 | [#36](https://github.com/tomiya7688/Open_karaoke/issues/36) | [Analysis Service](analysis_service.md)、[Song Format](song_format.md) |
| 外部MIDI / 既存譜面取込 | [#37](https://github.com/tomiya7688/Open_karaoke/issues/37) | [Requirements](requirements.md)、[Song Format](song_format.md) |
| 任意の歌詞・譜面編集 | [#38](https://github.com/tomiya7688/Open_karaoke/issues/38) | [Requirements](requirements.md)、[Song Format](song_format.md) |
| Windows同梱・インストーラー | [#39](https://github.com/tomiya7688/Open_karaoke/issues/39) | [Development](development.md)、[Licensing](licensing.md) |

**2026-10-14時点**: #1、#5、#9〜#11はclosed、#2〜#4と#6〜#8、#12〜#19はopen。#36〜#39は今回の文書監査で未起票だった1.0.0要件の抜けとして追加。
ただし、openのIssueにも実装済みのコードがある場合があります。実装が存在しても、
実データ評価やIssueの受け入れ条件が未完なら完了としません。最新状態は[Issues](https://github.com/tomiya7688/Open_karaoke/issues)を確認してください。

## 開発順序と依存

基本は基盤 → 解析 → 譜面 → 音声 → GUI → 統合評価。ただしCIやEvaluatorは最後にまとめず、**機能追加時に同時実装**します。

- Rust CoreのJob APIとSong Dataを安定させてから、解析Adapterが出すartifactを連結する。
- F0・Vocal Event・notesは同じ48kHz sample timelineと入力hashを共有する。
- GUIはRust Coreの公開APIへ接続し、Pythonの非公開ポートには接続しない。
- 全曲Job `analysis.song` とSongStoreへの登録は #36、外部譜面取込は #37、任意編集は #38、実配布物の構築は #39 をそれぞれ担当する。
- 店舗向けリアルタイム音声機能（#12〜#14）とGUI（#15〜#16）はその後の統合が必要。
- 実曲精度・商用配布ライセンス・Windows機材検証は1.0.0をブロックし得る品質ゲート。

## Issueの受け入れ条件

各Issueに目的、Scope/Out of scope、入力と出力、依存、Acceptance Criteria、Evaluator、
実行コマンドと検証結果を記録します。

合成fixtureによるAPI・回帰テスト成功、実モデルのロード成功、実録歌唱での精度確認、
製品としてのE2E/パッケージ検証は**別の証拠**として扱います。
