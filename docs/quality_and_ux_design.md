# 解析精度・カラオケの使いやすさの設計

> 状態：**実装前の設計案**。2026-11-04時点の`main` `907d166b2ca0bcfbaf739593a53004d9e0eef02e`を確認。Issue #106。
> ここに示す品質閾値、自動再解析、インストーラー、実機UXが実装済みという意味ではない。
>
> 根拠となる既存文書：[全曲解析](full_song_analysis.md)、[データセット評価](dataset_evaluation.md)、[テスト](testing.md)、[デスクトップクライアント](desktop_client.md)、[モデル拡張](model_plugins.md)。

## 製品として目指す体験と前提

目標とする通常操作は、**曲を選ぶ → 最小限の準備 → 歌う → 結果を理解する**、という流れである。歌詞・譜面の手動修正や、開発者向けのPython・モデル環境設定を通常利用の前提にしない。

次の3項目を混同しない。

1. **成果物の整合性:** 入力hash、時間範囲、Schema、言語、モデルIDが正しく、ファイルを安全に利用できるか。
2. **推定品質:** その区間の歌詞・音程・境界をどの程度信頼できるか。モデルのconfidenceやヒューリスティックな合議値が校正されているとは限らない。
3. **利用者が取れる操作:** 今すぐ歌えるか、問題のある区間を再解析できるか、結果に不満があるとき何をすればよいか。

これらを架空の「精度95%」などの単一値へまとめない。合成データの回帰テスト成功やモデル重みの導入成功は、**実歌唱の精度の証拠ではない**。

## 現行の解析経路と不足している品質改善ループ

現在の`analysis.song`は、以下の処理を1つのJob内で順に実行する。

```text
48kHzへ正規化したWAV
  → UMX-HQでボーカル分離
  → Whisperで歌詞候補を作成
  → 日本語CTCで歌詞の時刻を整列
  → YIN/NACFでオフラインF0を推定
  → Vocal Eventで音符・歌詞境界を融合
  → 音符候補を時系列で最適化
  → SongBundle
  → 別のsong.import JobでSongStoreへ登録
```

入力hash、形式、時間範囲、成果物の所有範囲は検査している。しかし、**低品質区間の自動再解析、confidenceの校正、既存MIDI・伴奏を使う代替経路の選択は未完成**。
必須段階が失敗すればJobは失敗する一方、「形式は正しいが歌詞や音程が間違っている」結果は構造検証に通る可能性がある。

今後は各解析段階と登録前に`QualityAssessor`・`RepairPlanner`を追加する。

```text
各段階の成果物（改変せず保持）
   │
   ├─ 整合性検査
   │     └─ 破損・契約違反 → 失敗／登録しない
   │
   └─ 品質判定
         ├─ 許容範囲 → 次段階
         └─ 不確実 → 制限付き再解析・別の証拠との照合
                       └─ 改善できない → 警告付きで利用可能にする
                                        （架空の高得点は付けない）
```

同じ成果物・設定に対し、RepairPlannerが同じ計画を生成できることを目指す。無制限にモデルを呼び直したり、未知のモデルを勝手に起動したりしない。音源・歌詞・譜面の証拠は別々に版管理し、文章として自然であるという理由だけで音響的にあり得る歌詞を上書きしない。

## 品質レポートの契約案

既存のv1 `song.json` / `notes.json` を暗黙に変更せず、**独立した版付き成果物**として品質情報を保存する。JSONのフィールド名は将来のAPI候補として保持する。

```json
{
  "quality_report_version": 1,
  "song_analysis_fingerprint": "<verified hash>",
  "model_snapshots": [
    {
      "role": "lyrics",
      "adapter_id": "whisper-large-v3",
      "version": "<pinned version>",
      "weight_sha256": "<verified hash>"
    }
  ],
  "segments": [
    {
      "start_sample": 48000,
      "end_sample": 144000,
      "dimension": "lyric_alignment",
      "status": "needs_review",
      "signals": {
        "alignment_coverage": 0.62
      },
      "signal_interpretation": "diagnostic_not_calibrated",
      "flags": ["ctc_fallback"],
      "attempts": 1
    }
  ],
  "overall": "playable_with_warnings"
}
```

上記は**形式を示す例**であり、`alignment_coverage: 0.62`は実測結果ではない。実際のレポートにはアルゴリズム・設定の版、入力・正解注釈の出典、成功/失敗した試行、実際に選択されたモデルの由来も記録する。`quality_report`は法律上・科学上の保証書ではない。

品質状態の候補:

- `blocked`: 必須の伴奏・譜面が欠ける、成果物が破損しているなど安全に利用できない。準備完了と公開しない。
- `needs_review`: ファイルは有効だが、一部の推定結果に不確実性がある。元の証拠を残し、再解析を検討する。
- `playable_with_warnings`: 再生・タイムラインは利用できるが、生成した歌詞や音符の一部は概算である。
- `ready`: 必須の構造契約と定義済みの製品品質ゲートに合格した。ただし完全な歌詞・採点を保証しない。
- `not_evaluated`: 品質を未評価。失敗とも誤差ゼロとも区別する。

`confidence`を正解確率と同一視しない。必要に応じて`signal_interpretation`で`heuristic`（経験則）、`calibrated`（データにより校正）、`measured`（実測）を区別する。

### 解析段階別の品質信号

| 段階 | 確認する診断情報 | 利用者への影響・再解析の候補 |
|---|---|---|
| ボーカル分離 | 伴奏の声への漏れ、マスキング、クリップ/非有限値、音の再構成誤差（構造検証） | 漏れはASRやマイク監視を妨げる。許諾された別モデルとの比較を検討 |
| Whisper歌詞認識 | 粗い時刻区間、無音判定、反復・幻覚候補、言語一致、参照歌詞との差 | 不確実な区間だけ前後文脈とともに再認識。文法の自然さだけで珍しい歌詞を消さない |
| 歌詞Alignment | 文字/行の整列率、欠落文字・辞書外文字、時刻と根拠の食い違い、区間重複 | 前後を広げて再整列、または行単位の概算時刻と明示 |
| F0推定 | YIN/NACF間の一致、オクターブ候補、有声判定の連続性、欠落と倍音の兆候 | 疑わしい区間だけ再推定し、元の観測と検出器別結果を保持 |
| Vocal Event | 音響・voicing・歌詞境界の一致、レガート・メリスマの分割の不確実性 | 隣接区間と歌詞の証拠を比較し、ビブラートをむやみに音符分割しない |
| 譜面生成 | 音符開始/終了の不確実性、極短音符の多発、スケール事前分布の矛盾、既存MIDIとの差 | 音符候補と局所最適化を再評価し、代替候補も保持 |

**hash不一致、PCM破損、Schema不整合**は低confidenceではなく整合性の失敗であり、安全側に拒否する。再解析で出典不一致を隠してはいけない。一方、内容品質の警告だけで歌唱開始を一律禁止しない。

## 制限付きの自動再解析

- 段階別・曲全体で、試行回数、解析窓の最大長、時間・CPU・VRAM、キャンセル確認点を制限する。「正解になるまで無限再試行」はしない。
- 各試行のIDと親成果物hashを記録し、元の観測を消さない。再解析区間が変更された場合、入力hashが変化した下流段階だけ再生成する。
- 候補を比較する指標は歌詞・時刻・音符ごとに分ける。歌詞が滑らかな文章であることを正確さとして過大評価しない。
- 安全なフォールバックを優先する。文字時刻が不明なら行時刻だけ示し、音符が不確かなら確かな箇所だけ音程バーを出し、採点根拠が弱ければ練習モードで再生する。
- 任意の既存歌詞・MIDIは強い参考情報として使えるが、必ずしも正解ではない。原曲の版・オフセット・テンポが一致しない場合は警告する。
- リアルタイム音声callbackへ重いPython推論を持ち込まない。#43で再生遅延へ悪影響があると判断した再解析は、歌唱後または再生停止中に実行する。

## モデル選択と再現性

現在のモデル画面は、役割ごとにモデル選択を保存して個別解析Jobへ反映できる。ただし全曲の`SongAdapter`は各段階の`options.models`またはRegistry既定値を使い、**保存済みの役割別選択はまだ反映しない**。

また、パッケージmanifestで既存Backendを指定できても、そのフォルダ内の異なる重みを実際に読み込むとは限らない。**「パッケージが利用可能」と「その重みで解析した」は別**。

全曲解析の開始時に、実効モデル計画を変更不能なスナップショットへ確定する。

```text
リクエストで明示したモデル
    ＞ 有効な保存済みモデル選択
    ＞ 製品の既定モデル
```

各段階で、要求したID、実際のAdapter、重みファイルhash、Backend・Runtimeの版、期待言語、デバイス、確定オプション、フォールバック理由を記録する。開始後に設定を解決し直さない。保存済みモデルが消えた場合、通常利用では通知付きの既定モデルへ切り替えてよいが、Evaluatorなど再現性が必要なモードでは**黙って別モデルで比較せず失敗**させる。

ユーザー導入MODはライセンスの種類だけを理由にローダーが拒否しない。これは公式配布物のライセンス審査とは別である。ただし外部のPythonコードには実行権限があり、manifestを読めることは安全な隔離や検証済み動作を意味しない。

## 精度評価のゲートと実データ

現在もデータセットmanifestの検証、合成CER回帰、非公開データ向け集計基盤が存在する。しかし多くの評価は**合成データ**のままである。`kind=lyrics`の注釈は先行しているが、複数タスク共通の実歌唱データと正解注釈は#42/#18で継続整備する。

必要な評価軸:

| 対象 | 指標 | 分けて測る条件 |
|---|---|---|
| 音源分離 | SI-SDR/SDRと人間の聴取評価 | 独唱/コーラス、伴奏密度、リバーブ、デュエット、声の漏れ |
| 歌詞 | CER、脱落・幻覚率、反復の誤り | 日本語/他言語、歌い回し、息継ぎ、速いフレーズ |
| 歌詞整列 | 境界誤差の中央値/p95、整列率、行欠落 | 長音、メリスマ、反復、伴奏漏れ |
| 音高 | cent誤差の中央値/p95、有声判定precision/recall、オクターブ誤り | 低音/高音、裏声、ノイズ、楽器混入 |
| 音符生成 | 音高/開始/終了F1、過分割・不足分割、人手注釈者間の一致 | レガート、ビブラート、しゃくり、ラップ、半音進行 |
| リアルタイム採点 | 得点の単調性、時刻補正の公平性、再現性、匿名の人間評価との相関 | マイク/オーディオ機器、USB遅延、歌手、ジャンル |
| 使いやすさ | 最初の1曲までの時間、取込成功率、初回失敗、機器復旧、必要な手修正回数 | CPUのみ・低メモリ、オフライン、高DPI、アクセシビリティ |

- 反復サビや近似音源が評価用と調整用へ混ざらないよう、**曲・録音・歌手・セッション単位**で分離する。短い音声窓を無作為に分けただけでは独立評価にならない。
- `development`と`holdout`を分け、holdoutへ繰り返し過学習しない。入力・注釈・重み・設定・閾値を版管理する。
- 権利処理済みの録音・歌詞・注釈のみ評価に使い、公開可能範囲を明示する。非公開の原曲・ボーカル・歌詞・個別識別子付きレポートは公開CI成果物へアップロードしない。
- 合成fixtureは高速・決定的な回帰に、実モデルsmokeは読み込み経路に、人手注釈付き実録は品質閾値の校正に、Windows実機は製品の最終確認に使う。
- ベースラインデータを収集する前に「本番品質を満たすCER/F1/遅延」の架空の閾値を決めない。

## 初回利用と普段の操作（設計案）

### 初回起動（開発者向け環境設定を要求しない）

1. 同梱したRust/Core/Pythonの実行環境とモデルを確認し、**導入・検証済み**、**ダウンロード可能**、**Runtime不足**、**非対応**、**ユーザー導入・未検証**を区別して表示する。
2. マイクとスピーカーを選択できるようにし、安全な小音量で再生テストを行ってから入力レベルメーターを見せる。遅延測定が可能なら実施し、測定できなければ`uncalibrated`のまま練習モードを案内する。測定していない0msを表示しない。
3. ダウンロード前に空き容量、manifestに記載されたモデルサイズ、ネット接続条件、進捗とキャンセルを示す。オフライン/手動モデル導入も選択肢として残す。
4. 設定は機器の安定識別子とBackend/サンプルレート/Buffer条件に紐づけて保持する。表示名だけでは同名デバイスを区別できない。機器変更時に旧遅延校正を無効化する。
5. 任意の細かな設定はスキップできるようにする。既存の伴奏とMIDIがあるのに初回設定のため歌えなくなる状態を避ける。

### 曲の取込・解析・歌唱

```text
原曲・伴奏ファイルを選ぶ
    → 任意で既存歌詞・MIDIを指定
    → 音源・機器・モデル・空き容量を事前確認
    → 段階別進捗とキャンセル付きで準備
    → 準備完了／警告付き完了／対応が必要
    → 1操作で歌唱開始
    → 理由が分かる音符別の結果を表示
    → 必要な区間だけ編集・再解析（任意）
```

- **AI自動生成**と**既存伴奏＋MIDI/歌詞**の2経路を用意する。既存のMIDI Importer・SongStoreを再利用し、GUIの中で変換処理を作り直さない。
- 長い解析では段階名・モデル・入力の問題・最後に保存できた中間成果物を示す。残り時間は実測に基づくときだけ提示し、進捗率だけから根拠のないETAを生成しない。
- 曲を利用できない場合は**理由と、次に押す操作**を示す（Runtime不足、重み未導入、不正音源、マイクなし等）。通信リトライで同じ曲を二重登録しない。
- 内容品質の警告と利用不可のエラーを区別する。ユーザーの歌詞・譜面編集を別版として保存し、再解析で勝手に上書きしない。
- 歌唱画面は現在/次の歌詞、見やすい目標音程バー、歌唱者の音程線、再生/停止・マイク/伴奏/エフェクト音量・シークを中心とする。診断値を大量に並べない。
- クリップ・入力不足・オーバーランは対処可能な機器状態として示し、歌唱ミス扱いしない。譜面品質や校正が未検証なら採点結果を**暫定**と明示する。
- 高DPI、大画面/小画面、キーボード操作、ダークモード、高コントラストへ対応する。headless画像テストだけではWindows実機のIMEやアクセシビリティは保証できない。

### 異常・キャンセル時の利用者向け動作

| 状況 | GUI/Coreの期待動作 |
|---|---|
| モデルが未導入・利用不可 | 対応するモデルの導入方法を案内。再現性を求める評価モードでは別モデルへ黙って切替えない |
| 解析の段階で失敗 | 検証済みの不変cacheがあれば残す。ただし未完成のSongBundleを完成曲として登録しない |
| 歌詞・音符の一部が低信頼 | 対象区間の再解析、または警告付きでの継続を案内し、曲全体を不用意に禁止しない |
| デバイス切断 | 理由付きで一時停止し、再選択・クロック再基準化・校正無効化を行う |
| 連続シーク・Core再起動 | 旧epochの音程/採点を捨て、Coreの正しい位置へ表示を戻す |
| 正しい採点ができない | 有効な音程バーは使って練習できるようにし、架空の得点を付けない |
| 容量不足・曲登録中断 | SongStoreを破損させず、再試行/再開できる条件を示す |
| Core再起動 | メモリ内Jobを完了扱いにせず、#41の契約で再接続・再認証する。保存済み曲は残す |

## 受け入れ条件と実装順序

**P1:** WAV/MP3/MP4からの全曲登録を結合する#36、Core歌唱セッション#107、リアルタイム採点#14、実歌唱データセットと品質ゲート#42/#18、実測遅延#40。

**P2:** Mixerとデバイス設定#13、歌唱GUI#16、実効モデル計画・選択状況#17/#109、資源競合の分離#43、配布#39、初回機器確認#110。任意編集#38は通常操作の後工程だが1.0.0の要件からは外さない。

**P3:** 複数ASR・全曲合議・LoRA研究#44は、基礎モデルを実歌唱で評価して改善すべき箇所が判明してから着手する。

その他の設計上の注意：Whisperが多言語でも初期のCTC Alignerは日本語向けであり、全対応言語を同じように整列できるとは限らない。対応不可の場合に架空の文字時刻を生成しない。WASAPIデバイスを名前だけで保存すると抜き差し後の同名機器を識別できない。confidenceは校正前に正解確率とみなさず、UI/採点が使う注釈・解析版を一致させる。

## #108 実装詳細設計：品質判定と限定再解析

この節は#108の**実装前の正本案**。既存の全曲 `SongAdapter`、個別Adapter、
`song.import`の契約を参照しながら設計を確定する。数値閾値や候補モデルの有効性は
実録データで校正するまで`Unverified`。実装がこの契約と異なった場合は
**その差分を同じPRで本節へ反映**し、IssueのClose記録で代用しない。

### 解析成果物の依存DAG

現在の既定実行順は`stems → lyrics → alignment → pitch → events → notes`だが、
再計算の**依存関係**は実行順そのものと異なる。

```text
original (48 kHz WAV)
       │
     stems ──────── vocals ───────────────────┐
       │             ├── lyrics → alignment ─┤
       │             └── pitch ────────────┐  │
       │                                   v  v
       └── accompaniment                events
                                          │
                    alignment ────────────┼──> notes
                    pitch ────────────────┘
```

`notes`のオプション/参照候補も依存キーへ含める。実際には`events`も
音声そのものを特徴量抽出に使用するので、`vocals`を必須の依存とする。
全stageの再実行を毎回行うのではなく、**入力artifactの内容hashが変わった下流ノードだけ無効化**する。

| 変更されたevidence | 再実行対象の最小集合（候補） |
|---|---|
| stemsのvocals | lyrics, alignment, pitch, events, notes |
| ASR lyrics | alignment, events, notes。pitchの入力が同じなら再実行しない |
| alignment | events, notes |
| pitch | events, notes |
| events | notes |
| note candidates/参照MIDI | notesのみ（候補がevents入力へも供給される設計ならeventsも） |

同じ曲の連続する二つの区間で再解析するときは、境界の共有部分と前後文脈を
明示した`local_sample → song_sample`写像を用い、隣区間を不意に上書きしない。
パスの名前だけで同一性判定をせず、
`{source_sha256, model_id, model_version, weight_sha256, options_digest, parent_artifact_hashes}`
をevidence identityの材料とする。digestはキー順など正規化方式を版で固定する。

### 型・関数・責務（新設する設計契約）

| 型/コンポーネント | 入力 | 出力 | 責務 |
|---|---|---|---|
| `ArtifactDependencyGraph` | stage role、入力sha、model/config snapshot | immutable node IDと下流依存 | 再解析後の無効化対象を一意に決定 |
| `QualityAssessor` | 検証済みstage artifactと観測evidence | `QualityFinding[]` | **純粋判定**。データの読込・モデル推論・再実行はしない |
| `RepairPlanner` | findings、dependency graph、model plan、budget | `RepairPlan` | 限定再実行するロール・区間・入力・budgetを決定 |
| `RepairExecutor` | immutable plan、既存AdapterRegistry、Job Cancellation | 新しい版のartifactとattempt結果 | 非callbackのPython worker内で実行 |
| `QualityPublisher` | 完全性検査済みartifact graphと最終findings | `quality_report.json` | 監査可能な結果を一括公開。壊れた曲はreadyにしない |

**継承ではなく委譲**。既存の`ModelAdapter` Protocolは変更しない。
各モデルが品質判定まで継承して抱える形にはせず、`QualityAssessor`から
独立したdetector（`LyricRepeatDetector`, `PitchDisagreementDetector`等）の
関数へ処理を委譲する。すべての役割に同じ「confidence」の意味を強制しない。

必要最小のデータ契約案:

```text
QualityFinding:
  artifact_id, stage, span_start_sample, span_end_sample,
  diagnostic_code, severity, signal_name, signal_value,
  calibration: "heuristic" | "calibrated" | "measured" | "unknown",
  evidence_artifact_ids[]

RepairPlan:
  plan_version, input_fingerprint, selected_nodes[],
  windows[{start_sample,end_sample,context_before,context_after}],
  model_plan_snapshot, max_attempts, max_total_attempts,
  downstream_invalidation[], cancellation_policy

QualityReport:
  report_version, song_fingerprint, parent_artifact_hashes[],
  stage_findings[], applied_repairs[], skipped_repairs[],
  output_readiness, unverified_reasons[]
```

内部は型付きモデルとして検証し、JSONへの変換時は`NaN`/`Infinity`・
重複キー・不正sample span・未知の親artifactを拒否する。
JSONファイルのパスだけを新しいモデルへ自由入力として渡さない。
`report_version`は、既存の`song.json`/`notes.json`の`format_version`と独立にする。

### 審査→計画→実行→公開の状態機械

```text
StageValidating
  ├── structural failure → Failed (公開なし)
  └── valid → QualityAssessing
                   ├── no finding / no calibrated gate → Publishing
                   └── findings → Planning
                                      ├── no compatible repair / budget depleted → PublishingWithWarnings
                                      └── plan → Executing → ValidatingRepaired
                                                       ├── invalid artifact → Failed (新成果物は不採用)
                                                       └── valid → Reassess → Publishing
```

- `quality: not_evaluated`は`ready`とも`blocked`とも異なる。
  heuristicの閾値が低いだけで`blocked`にして曲を消さない。
- 壊れたhash/Schema/PCMは**hard integrity failure**であり、
  「気になる歌詞」を再解析する品質警告と混ぜない。
- 1回の`analysis.song`実行で許可する再解析回数・区間長・CPU/VRAM・
  経過時間の上限は`RepairBudget`へ設定する。値は実データで調整し、未測定の最適値を装わない。
  制限到達は`skipped_repairs(reason=budget_exhausted)`として残す。
- 計画は`input_fingerprint`と設定が同じなら決定的であるべきだが、
  実行時間制限により終了した場合は同一結果になると保証しない。
  比較評価モードでは任意のモデル自動fallbackを禁止する。
- 再解析で前段を更新しても**SongStoreに既に確定した曲を上書きしない**。
  一時Job領域へ新しい候補を作り、全下流の検査に成功したときだけ
  `song.import`経由で明示的な登録/版管理を行う。
- ユーザーの歌詞編集は`base analysis`とは独立したlayerとし、
  再解析でユーザーが修正した内容を自動で消さない（#38と契約を合わせる）。

### Vocal Event と Alignmentの二段階利用

現在の初回`alignment`は`events`が生成される前に動くため、
Closed #10が目指した「境界を歌詞整列へ戻す」接続がない。
一度に相互参照する循環依存を作らず、**pass別の新しいartifact**を使う。

```text
alignment/pass1 → events/pass1
                      │
           optional alignment/pass2
                      │
                events/pass2 → notes/pass2
```

`pass2`実施条件は`pass1`の診断が一定条件を満たす区間に限定する。
無限にpassを重ねず、開始sampleと終了sample、検出器versionと親hashを保存する。
歌詞本文を改変しない整列修正と、ASRテキストを変更する再解析は**別のrepair**とする。
音素・文字単位の正解境界がない実歌唱では、改善を証明したことにしない。

### 実装と同時に追加するテスト

| テスト（新設予定） | 確認内容 |
|---|---|
| `python/tests/test_quality_assessor.py` | 無音/反復/低voicing、機能別signalの意味、非有限値拒否 |
| `python/tests/test_repair_planner.py` | 上限、区間重なり、モデル変更、同一入力時の決定性、未対応repairの明示 |
| `python/tests/test_artifact_dependency_graph.py` | parent hash変更と下流無効化、境界マージ、別版の識別 |
| `python/tests/test_song.py`追加 | 最終成果物の不可変性、キャンセル、段階失敗、二段階passとno fake success |
| `python/tests/test_dataset_manifest.py`周辺 | development/holdout混在防止、権利参照のある実歌唱検証 |
| `core/tests/song_import.rs`追加 | 構造不正なquality reportを歌える曲として確定させない |

実装IssueとPRはここに書いた関数・データ・例外を**実コードとテストへ対応づける**。
修正の必要が判明したら、本節を実装中に更新する。

