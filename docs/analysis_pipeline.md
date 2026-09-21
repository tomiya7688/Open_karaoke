# 解析パイプライン

## 基本方針

単一モデルの出力を正解とみなさない。

Whisper を含む複数の認識器、音響解析、既存歌詞、譜面、曲構造などを統合し、曲全体として最も整合する結果を求める。

この中核を Song Global Optimizer / Orchestrator として独立させる。

## 全体構成

```text
Original Song
     |
     +-- Stem Separation
     |      +-- vocals
     |      +-- accompaniment
     |
     +-- ASR A: Whisper
     +-- ASR B: other ASR
     +-- Forced Alignment
     +-- Pitch / F0
     +-- Note Transcription
     +-- Onset / Silence
     +-- Song Structure Analysis
     |
Optional Reference Data
     +-- Imported Lyrics
     +-- Imported MIDI / Score
     |
     v
Orchestrator
     |
Transformer / Anomaly Validator
     |
Confidence Estimation
     |
Whole-song Temporal Optimization
     |
Canonical Song Data
```

## 複数パス解析

リアルタイム文字起こしとは異なり、曲生成はオフライン処理である。

そのため未来の区間も含めた曲全体を使って最適化する。

想定パス:

1. Pass 0: 全体特徴抽出
2. Pass 1: 初期認識
3. Pass 2: 曲構造・反復区間推定
4. Pass 3: 曲コンテキスト付き再認識
5. Pass 4: 時間方向・反復構造の整合
6. Pass 5: 複数モデル ensemble
7. Pass 6: 最終確定

## 曲全体コンテキスト

1回目の解析から、その曲専用のコンテキストを作る。

例:

- 言語
- 頻出語
- 固有名詞候補
- 高信頼歌詞
- サビ等の反復フレーズ
- Verse / Chorus / Bridge 等の構造
- テンポ
- キー
- 音域
- 類似区間

その情報を2回目以降のASRや再ランキングへ与える。

これはモデル重みを書き換えるLoRAそのものではないが、song-specific contextual adaptation としてLoRAに近い挙動を狙う。

## 本当のLoRA / Adapter

将来的な研究候補とする。

高信頼なpseudo-labelだけを教師として、1曲限定の小規模adapter / LoRAを短時間学習し、難しい区間を再認識する。

ただし以下の問題があるため、最初から必須にはしない。

- 学習時間
- VRAM
- 1曲では教師データが少ない
- 誤ったpseudo-labelの自己強化
- 過学習

まずは contextual prompting、ensemble、forced alignment、全曲最適化で精度を詰める。

## 歌詞解析

### 既存歌詞あり

既存歌詞を強い参考データとして利用する。

```text
Imported Lyrics
      +
Vocals Audio
      +
ASR Outputs
      +
Forced Alignment
      v
Final Lyrics + Timing
```

既存歌詞は絶対正解とはしない。

以下を考慮する。

- 誤植
- ライブ版
- TV size
- Remix
- 歌詞変更
- 省略
- 表記差

### 既存歌詞なし

複数ASRの候補を生成し、オーケストレーターで統合する。

単純多数決ではなく、候補ごとの証拠を評価する。

例:

- ASR confidence
- 音響との音素一致
- 他モデルとの一致
- 前後文脈
- 曲全体文脈
- 既存参考データ
- 反復区間
- anomaly score

## Transformer の役割

Transformer / LLM を自由な「歌詞修正器」として扱わない。

主用途:

- 破綻検出
- hallucination 検出
- 異常な反復
- 不自然な脱落
- 候補間の再ランキング
- 前後文脈との整合チェック

候補群から選ぶことを優先し、自由生成は最後の手段とする。

歌詞には造語、固有名詞、文法崩し、当て字、方言などがあるため、「自然な文章へ修正」すること自体を正解とはみなさない。

## 時間方向の最適化

楽曲では以下の強い制約が使える。

- 歌詞は基本的に時間方向へ進む
- 音符も時間方向へ並ぶ
- 音節と音符の境界には相関がある
- 同一サビ等の反復区間がある
- 前後の無音 / voiced 判定が使える

Dynamic Programming、Viterbi、CTC alignment 等の既存手法を利用できる。

局所最適ではなく、曲全体で矛盾の少ない解を求める。

## 反復区間の利用

サビが複数回ある場合、各区間を独立に扱わない。

例:

```text
Chorus 1: confidence 0.91
Chorus 2: confidence 0.98
Chorus 3: confidence 0.63
```

音声特徴、F0、リズム、音節数が近ければ、3区間を相互補完して低信頼結果を改善する。

同じ考え方を譜面生成にも利用する。

複数回のpitch contourを重ねることで、しゃくり、裏返り、分離ノイズなどの一時的な歌唱差から、採点に使う理想音程を推定する。

## 譜面生成

候補:

- CREPE 系 F0推定
- pYIN / YIN
- Basic Pitch 等のNote transcription
- onset detector
- imported MIDI

実歌唱のF0をそのまま採点譜面にはしない。

ビブラート、しゃくり、こぶし、ポルタメント等を考慮し、採点用の離散的なノート列へ正規化する。

## 信頼度

単一のconfidenceだけでなく、内部では分離して保持する。

例:

- text_confidence
- timing_confidence
- pitch_confidence
- segmentation_confidence
- alignment_confidence

各候補には可能な限りevidenceも保持する。

低信頼の場合はすぐ人間へ回さず、自動再解析を行う。

```text
Initial Analysis
      |
Low Confidence
      |
Auto Retry
 +-- another ASR
 +-- wider context
 +-- another stem separation
 +-- forced alignment
 +-- phoneme alignment
 +-- repeated-section evidence
      |
Best Effort Result
```

人間レビューは例外であり、通常経路ではない。
