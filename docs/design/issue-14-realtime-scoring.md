# #14 リアルタイム音程検出と採点エンジン 設計

> 状態: コアの採点エンジン（`core/src/scoring.rs`）とYIN推定器（`core/src/scoring/estimator.rs`）は実装済み。歌唱セッションへの結線（#107）と実機での遅延・精度の検証は未実施（Unverified）。最終確認日: 2026-11-08
> 既存のオフライン `python/src/open_karaoke_analysis/pitch*.py` はリアルタイム採点の代替ではない。
> 上位の契約は [Scoring](../scoring.md)。歌唱セッションへの結線は #107（[karaoke_session_design.md](../karaoke_session_design.md)）で定義される。

## 目的

再生中のraw（エフェクト前）マイクPCMから時刻付きF0を作り、採点譜面（`notes.json`）と比較して、pitch / timing / coverage / stability を**別指標**で保持するスコアを出す。

## 受入条件（[Issue #14](https://github.com/tomiya7688/Open_karaoke/issues/14) から転記）

- 再生中に現在pitchとtarget noteを比較できる
- pitch/timing/coverage/stabilityを別指標として保持
- 最終重みを後で校正可能
- score resultを保存可能
- テスト: 人工的な±cent offset、timing offset、無音/ノイズ、単調性、決定的なscore fixture

## 非目標

- マイク入力デバイス・キュー・Mixer接続（#12/#13/#107）
- 歌唱中のPython実行・モデル推論（禁止）
- 人間評価との相関による重み校正（#19/#42。本Issueは「後で校正可能」な構造まで）
- long tone / vibrato / dynamics 指標（[Scoring](../scoring.md)の「将来」）

## 設計

### 責務

- `RealtimePitchDetector`（trait, 新設 `core/src/pitch_tracker.rs`）: 時刻付きraw monoフレーム列→`PitchObservation`。差し替え可能。
- `ScoringEngine`（新設 `core/src/scoring.rs`）: 観測を譜面ノートへ対応づけ、ノート別指標と集計を作る。副作用なし（I/O・時計取得なし）。
- 時刻写像は既存 [`ClockSession::resolve(ClockDomain::Capture, epoch, stream_sample)`](../../core/src/audio_clock.rs) に委譲し、自前の遅延補正を作らない。

### データ契約（案、フィールド名は実装PRで確定し本書を更新）

- 入力: `TimestampedMicrophoneSample { epoch, stream_sample, sample }`（既存、`audio_engine.rs`）。48kHz mono・effects前。
- `PitchObservation { epoch, start_sample, end_sample, f0_hz: Option<f64>, voiced: bool, confidence, reject_reason: Option<_>, detector_version }`。`confidence`は診断値で**校正済み確率ではない**。無声/拒否時に架空F0を入れない。
- cent誤差: `1200 * log2(f0_hz / target_hz)`。`f0_hz <= 0`・非有限は拒否（観測を無効扱い）。
- `NoteResult { note_id, median_abs_cent, in_tune_ratio, voiced_ratio, gross_error_ratio, onset_error_samples, coverage, stability, unscored_reason }`。
- `ScoreResult { scoring_version, detector_version, note_chart_version, latency_calibration (Unknown|Estimated|Measured), notes[], aggregate{pitch,timing,coverage,stability 各0..1}, unscored_reason_counts }`。総合点の重みは設定値として`scoring_version`に紐付け、固定しない。

**エラー**: stale epoch・`UnanchoredDomain`・`BeforeSongStart`（`ClockError`）の観測は採点から除外して件数を`unscored_reason_counts`に記録する。「歌わなかった」と扱わない。遅延`Unknown`は練習表示のみ可、確定得点は暫定/利用不可を明示する。

**スレッド/リアルタイム制約**: detectorとscoringはaudio callbackではなく専用workerで動く（callbackは`AudioIo`の固定容量キューに積むだけ）。worker内も有界遅延・事前確保バッファ。timing補正のDTWは曲終了後の集計のみ、warp量に上限を設ける。

## 実装スライス

| # | 内容 | 依存 | 完了条件・テスト |
|---|---|---|---|
| 14.1 | `ScoringEngine`の純関数: cent誤差・ノート別指標・集計、`ScoreResult`型とserde | なし | `core/tests/scoring.rs`: ±cent単調、timing単調、無音/無声、gross error（オクターブ）、決定的fixture |
| 14.2 | `RealtimePitchDetector` trait + 自前実装1つ（依存追加なら[ライセンス確認](../licensing.md)） | 14.1の型 | 合成正弦/ノイズ/無音/オクターブ誤りfixture、フレームごと有界時間、割当なし（事前確保）の確認 |
| 14.3 | 時刻写像: `PitchObservation`→曲sample、epoch不一致除外、遅延品質の伝播 | 14.1。`ClockSession`は実装済み（`core/src/audio_clock.rs`、物理校正のみ#40で未完。`Unknown`を伝播すれば先行可） | 疑似ドリフト・負補正・シーク/旧epoch混入なしのテスト |
| 14.4 | 結果保存形式（`ScoreResult`のJSON、保存先と版） | 14.1 | round-trip、版違い読込拒否。保存先は未決事項 |
| 14.5 | #107のworker組成へ接続（練習=暫定/全曲=確定の区別、シーク後の重複加点禁止） | 14.1-14.3, #107 | **#107側のスライスの受入項目として扱う**（#14のIssueは14.4までで閉じられる）。テストは#107側`karaoke_session` |

## 触るファイル

新設: `core/src/pitch_tracker.rs`、`core/src/scoring.rs`、`core/tests/scoring.rs`、（`core/src/lib.rs`の`mod`宣言のみ）。
読むだけ: `core/src/audio_engine.rs`（`read_microphone_timestamped`）、`core/src/audio_clock.rs`、`core/src/song_data.rs`（`Note`）。
`core/src/lib.rs`と`ShellViewModel`は他セッションと競合しやすい: マージ前に`origin/main`へrebase。

## 守る規則

- [宣言コメント（JSON-like）必須](../coding_rules.md#5-ソースコードコメント)、[責務分離](../coding_rules.md#2-ui--process--dataの責務)、[継承禁止の規約](../coding_rules.md#継承の共通規約必須レビュー基準)（traitは小さく、具象起点の連続継承を作らない）
- 内部時刻は48kHz整数sample index（[architecture](../architecture.md#オーディオ内部仕様)）。f64秒を正本にしない
- 採点入力はraw mic。エコー/リバーブ後の監視信号を流さない（[audio_engine §Mixer](../audio_engine.md#mixer-and-monitoring-effects--issue-13-first-slice)）
- realtime分離: callbackにロック/確保/I/O/Python無し
- 合成fixtureの成功を実歌唱精度の証拠にしない（`Unverified`に書く）

## 未決事項

- F0アルゴリズム（YIN/pYIN/MPM等）と依存の可否（ライセンス・性能）
- 結果の保存先（`SongStore`配下か別領域か）と`calibration_version`の出所（#40/#110）
- 最終得点の初期重み（仮重みを置くか、指標のみ出すか）
- ノート境界で許すずれ（前後何sample）の既定値
