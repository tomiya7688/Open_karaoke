# Scoring Design

## 方針

採点は1つのヒューリスティックへ依存せず、独立した評価軸を算出して最後に統合する。

初期の中心:

1. Pitch accuracy
2. Timing accuracy
3. Note coverage
4. Stability

将来:

5. Long tone
6. Vibrato
7. Dynamics / expression

## Pitch Accuracy

採点用譜面のtarget pitchと歌唱F0との差をcentで評価する。

瞬間値を直接点数化せず、短時間median / robust smoothingを通した値を使う。

ビブラートやしゃくりを完全な誤りとして扱わないため、ノート中央部と遷移部を区別できる設計にする。

ノート単位で以下を保持する。

- median absolute cent error
- in-tune frame ratio
- voiced frame ratio
- gross pitch error ratio

閾値はEvaluatorで校正する。

## Timing Accuracy

ノート開始・終了の時間差だけでなく、歌唱列と正解列全体の時間ずれを扱う。

方法:

- latency compensation
- onset / offset error
- local DTW alignment

DTWは歌唱テンポの微小な揺れを吸収する目的で利用し、極端な時間ずれを無条件に正解へ寄せないようwarp量へ上限を設ける。

## Note Coverage

正解ノート区間のうち、ユーザーが有声音として歌えている割合を評価する。

休符・間奏は対象外。

Pitchが外れていてもCoverage自体は別指標として保持する。

## Stability

ノートのsteady-state領域におけるpitch varianceやjitterを測る。

音符遷移部分、しゃくり、意図的なビブラートは別扱いにする。

## 総合得点

内部では各指標を0..1で保持する。

例:

```text
pitch_score
timing_score
coverage_score
stability_score
```

最終スコアの重みは固定設計値ではなく、人間評価付きベンチマークで校正する。

初期仮重みを置く場合も、後で設定・モデルバージョンとして変更可能にする。

## Score Versioning

採点結果には以下を保存する。

- scoring_version
- pitch_detector_version
- score_data_version
- calibration_version

将来採点アルゴリズムを改善しても、過去結果の意味を追跡できるようにする。

## Evaluator

Scoring Evaluatorは少なくとも以下を測る。

- 同一入力に対する再現性
- 正解ピッチからの人工centずれに対する単調性
- 人工タイミングずれに対する単調性
- 無音 / ノイズ / オクターブ誤りへの耐性
- latency compensationの正しさ
- 人間評価との相関

1.0.0前には複数曲・複数歌唱者の評価セットで校正する。
