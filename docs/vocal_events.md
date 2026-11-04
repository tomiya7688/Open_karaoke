# ボーカルイベント・境界融合設計（Issue #10）

## 目的

採点用譜面生成で共有する、**48kHz整数タイムラインのVocal Event情報**を生成する。
現在の歌詞Alignmentは**上流**にあり、その文字時刻情報をEvents側が任意入力として受け取る。
一方、Eventsの推定結果を最初のAlignmentへ戻して再整列する機能は**未実装**である。

境界情報と独立した証拠を保持し、`note_boundary_probability`と
`lyric_boundary_probability`という2つのヒューリスティック値を出す。
どちらも0〜1の範囲だが、**正解確率として校正された値ではない**。

## 入力

Coreは分離済み・正規化済み`vocals.wav`を`events`ロールへ渡す。
同じボーカル音源のhashに紐づくIssue #9の`pitch.json`を必須とする。
Issue #8の`alignment.json`と、将来の形式v1の音符候補artifactは任意入力とする。

Pitch成果物には48kHz、480サンプル間隔、同じ曲長と入力SHA-256を要求する。
別の曲由来の証拠は拒否する。

## 独立した特徴量

各証拠を、10ms hop・40ms解析窓のPitch時間グリッドへ投影する。

- `f0_transition`: 35centの不感帯を越える音高変化。小さなビブラートを強い境界と誤認しない。
- `voiced_transition`: 有声確率と有声/無声状態の変化。
- `spectral_flux`: 正規化したスペクトルの正方向変化。
- `energy_delta`: フレーム間の音響エネルギー変化。
- `acoustic_onset`: スペクトル変化とエネルギー立上りの組合せ。
- `lyric_timing`: 任意のAlignment由来の行・文字・単位・音素・音節時刻。現在の日本語Alignmentが提供するのは文字/単位の情報であり、音素と呼び替えない。
- `note_onset`: 将来の音符候補推定が提供する開始時刻。
- `silence_breath`: エネルギーの低い無声変化と息継ぎらしい信号。

ステレオの位相打消しが発生した場合は、音量の大きいチャンネルへ切り替え、
その事実をevidenceに残す。

## 特徴量融合と比較

`mode=ensemble`は利用可能な特徴量を、公開された固定重みで合議する。
任意入力のAlignmentや音符候補が欠ける場合は、存在する特徴量だけで重みを正規化する。

`mode=single`では指定した特徴量をちょうど1つだけ使い、
両境界スコアへその値をそのまま出力する。特徴量の寄与や回帰を比較するためのモードであり、
異なる特徴量が同じ意味を持つと主張するものではない。

出力する`vocal_events.json`は正規化済み特徴量、2種類の境界スコア、無音/息継ぎ指標、
近接するピークを抑制した音符/歌詞境界候補を保持する。
`vocal_event_evidence.json`はF0、有声確率、音響特徴の生値、特徴量別の寄与、
参照したartifact、位相打消しの記録、任意の言語・音符証拠を保持する。

候補の抑制間隔は既定30msとし、フレームごとの確率値そのものは変更しない。

## データフローの境界と今後の改善

現在の全曲Jobは`alignment → pitch → events → notes`の順に実行される。
`events`は任意の音符開始時刻を入力できるが、全曲の既定経路ではNotes段階が後なので、
音符候補artifactをEventsへ渡していない。

したがって、現状は**Events情報をNotesが参照できても、最初のAlignmentへ戻す双方向の改善はない**。
「境界情報による双方向の歌詞整列が実装済み」と説明しない。

将来は、最初のAlignmentとEventsの不変な成果物から、不確実な歌詞境界だけを
**回数上限付きの第2パス**で補正し、入力hash・版を記録しながら依存Notesを再生成する。
単純な循環依存は作らない。実装と実歌唱評価は
[#108](https://github.com/tomiya7688/Open_karaoke/issues/108)と
[Close済みIssue監査](closed_issue_coverage.md)で追跡する。

## 現時点の精度の限界

新しい学習済み重みは追加しない。初期実装はNumPy/SoundFileと本リポジトリ内のDSPを使う。

回帰データには合成した明瞭な境界、ノイズなど低品質のケース、
レガート・メリスマを含むケースがある。これは処理順・契約・決定性の検証であり、
**実歌唱での境界精度、正解確率の校正、息継ぎ分類の品質、最終譜面の音符分割精度を証明しない**。

Issue #11が曲全体の音符最適化を担当し、ビブラート・しゃくり・メリスマを
どこまで音符として扱うかを決定する。

## Evaluator

`open-karaoke-vocal-event-evaluator`は、許容時間差内で正解境界と推定境界を
1対1に対応させ、precision・recall・F1・対応境界の時刻誤差を算出する。

CIでは生成済み合成fixtureに対し、許容30ms・音符境界F1が0.90以上・歌詞境界F1が0.95以上を
回帰ゲートとして使う。**これらは実歌唱品質の合格基準ではない**。

## 検証方法

対象を絞った検証:

- `python -m pytest python/tests/test_vocal_event*.py -q`
- `python/tests/fixtures/vocal_event_boundaries.json`を使って`open-karaoke-vocal-event-evaluator`を実行。許容30ms、音符F1≧0.90、歌詞F1≧0.95。

共有契約を変更する場合は、通常のPythonテスト、Rust workspaceテスト、
Core/Pythonのプロセス統合テスト、Windows/Linux CIも確認する。
