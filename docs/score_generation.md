# 採点用譜面の生成設計（Issue #11）

## 目的

オフラインF0とVocal Eventの時間列から、採点で参照する正規の音符列を生成する。
出力はRustのSong Dataと互換のある形式v1の`notes.json`である。

初期実装では責務を次の3段階へ分離する。

1. 音符候補の生成
2. 曲全体の音高・キー・時間的整合の最適化
3. 小さく安定した`notes.json`契約への公開

これにより、Song Data形式を変更せずに音符推定器や最適化器を入れ替えられる。

## 入力

必須の成果物:

- F0合議の`pitch.json`
- Vocal Event/境界融合の`vocal_events.json`

両方に同じ入力SHA-256、曲長、48kHzサンプルタイムライン、480サンプルの解析間隔を要求する。

任意の証拠:

- 歌詞区間を対応づける`alignment.json`
- 別の音符推定器から受け取る候補artifact
- 反復区間を記録したartifact

Notesロールでは、検証済みの上流成果物を使用し、原音源を再度開く必要はない。

## 音符候補層

初期ベースラインでは、有声音と判定されたF0の連続区間と、採用されたVocal Event境界から
音符候補を作る。候補ごとに以下を保持する。

- 整数サンプルの開始・終了位置
- 外れ値に強い浮動小数点MIDI音高推定値
- 音高のconfidence
- 音高のばらつき
- 境界を支持する証拠
- 複数のMIDI音高候補
- 生成元と出典情報
- 任意の歌詞区間ID

`note_candidates.json`は解析途中の形式であり、Song Dataの正規形式ではない。

### 外部の音符候補を受け取る契約

将来のBasic Pitch Adapter、外部MIDI、別の音符推定器も
`candidate_artifact`経由で同じ候補情報を渡せる。
形式v1の例:

```json
{
  "format_version": 1,
  "sample_rate": 48000,
  "duration_samples": 96000,
  "input_sha256": "...",
  "provider": {"id": "basic-pitch", "version": "..."},
  "candidates": [
    {
      "id": "candidate-1",
      "start_sample": 12000,
      "end_sample": 36000,
      "midi_note": 64,
      "confidence": 0.91
    }
  ]
}
```

初期ベースラインはBasic Pitchのコード・重みを同梱しない。
候補生成側と最適化器・`notes.json`を疎結合にし、独立にライセンスを確認した外部の推定器を
後から追加できる契約だけを定める。

**現在の全曲解析との接続制約:** `NotesAdapter`単体は`candidate_artifact`を読めるが、
全曲`SongAdapter`は自らが所有する配線を守るため、
`stage_options.*_artifact`の上書きを拒否する。
したがって、ユーザー指定のMIDIや外部音符候補は**既定の全曲Jobへまだ接続されていない**。
セキュリティ上の配線検証を解除するのではなく、入力を検証してスナップショットへ固定する
専用の経路が必要。[Issue #37](https://github.com/tomiya7688/Open_karaoke/issues/37)と
[Close済みIssue監査](closed_issue_coverage.md)で追跡する。

## 音符境界と歌唱表現の正規化

採点用譜面は、歌唱者の細かな揺れを一つずつ音符にするのではなく、**意図した音符**を表す必要がある。

候補生成では次を行う。

- 極めて短い無声区間を橋渡しする
- 持続した音高変化・無音・息継ぎを伴わない弱い境界ピークを抑制する
- 強い音符開始や境界の証拠を残す
- メリスマで持続する音高の段差を残す
- 音符の定常音高を推定する前に遷移フレームを除く
- 瞬間F0ではなくconfidence重み付きの外れ値に強い中央値を使う

時系列最適化器は、弱い同音高の分割や、境界が弱く極短い断片を統合する。
これがビブラート・しゃくり・こぶし状の短い音高変化・ポルタメントを正規化する初期処理となる。
規則は再現可能で差し替え可能だが、**あらゆる歌唱技法を完全に分類する仕組みではない**。

## キー・スケールの推定

音高クラスを重み付けして集計し、長調・短調の各12主音を比較する。
候補の長さと音高confidenceを観測値の重みに用いる。

推定キーは**弱い事前情報**としてのみ使う。推定スケール外の音符も認める。
間違ったキー推定で半音進行を無理にスケール内へ丸めないためである。

## 反復区間の証拠

任意の反復区間artifactには、元区間と反復区間の組を記録する。

```json
{
  "format_version": 1,
  "duration_samples": 192000,
  "pairs": [
    {
      "id": "chorus-repeat",
      "source_start_sample": 0,
      "source_end_sample": 96000,
      "repeat_start_sample": 96000,
      "repeat_end_sample": 192000,
      "confidence": 0.8
    }
  ]
}
```

初期実装では、反復した候補と対応する元区間候補との間に、小さな音高一致の事前重みを与える。
これは将来の改善のための入口であり、**反復区間の音符を強制的に同一にする制約ではない**。

## 時系列最適化器

`note_optimizer.py`は`NoteOptimizer` Protocolを定義する。
標準実装は`TemporalDynamicProgrammingOptimizer`（`temporal-dp-v1`）である。

各時刻順候補の採用音高を状態とし、以下を目的関数へ加える。

- 音符候補のconfidence
- 小さなキー・スケール事前情報
- 近隣音程・オクターブ遷移への小さなペナルティ
- 任意の反復区間の事前情報

音高の経路を決めてから正規化処理を行う。将来の最適化器は、
候補入力と正規出力の契約を維持して差し替えられる。

## 正規の出力形式

`notes.json`は意図的に小さく保ち、`core/src/song_data.rs`と一致させる。

```json
{
  "format_version": 1,
  "analysis_version": 1,
  "notes": [
    {
      "id": "note-0001",
      "start_sample": 12000,
      "end_sample": 36000,
      "midi_note": 64,
      "pitch_confidence": 0.88,
      "lyric_segment_id": "line-0001"
    }
  ]
}
```

細かな判断の根拠は`note_evidence.json`へ保存する。
モデル固有の情報を安定したSong Data契約へ混入させない。

## Evaluator

`open-karaoke-note-evaluator`は以下を報告する。

- 音高が一致した音符のprecision / recall / F1
- 開始時刻（onset）の平均・最大誤差
- 終了時刻（offset）の平均・最大誤差
- オクターブ誤りの件数・割合

登録済み`score_notes_reference.json`は手作業で注釈した**合成回帰データ**であり、
ビブラートなどの揺れ、実際の音高の段差、無音で分かれた同音高音符、
短いこぶし状の音高変化を含む。

検証できるのは処理の決定性とアルゴリズムの意図した振る舞いまでである。
`real_singing_accuracy_verified`は`false`。
実歌唱の人手正解譜面コーパスによる評価なしに、本番品質の音符推定精度を主張しない。

## 精度とライセンスの境界

このベースラインで新しい学習済みモデル・重み・外部推論Runtimeは追加しない。
外部の音符候補は任意入力とし、公式配布物へ同梱する場合はプロジェクトのライセンス方針に従って審査する。
