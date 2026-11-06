# Close済みIssueの設計引き継ぎ監査

> 確認対象：2026-11-04 の `main` `907d166b2ca0bcfbaf739593a53004d9e0eef02e` と Close済みIssue（対象16件）。
>
> この文書は **Issueを再Openする判定ではありません**。当該Issueの基礎実装、製品統合、実データ精度評価の3つを分け、未完の条件を既存Open Issueへ引き継ぐための記録です。PR #95（Core再接続）・#77（Evaluator集計）・#111（設計）は作業時点で未マージです。

## 判定と対象範囲

| Close Issue | 実装・監査結果 | Close後の条件と引き継ぎ |
|---|---|---|
| [#1 Bootstrap](https://github.com/tomiya7688/Open_karaoke/issues/1) | Rust/Python/Avaloniaのmonorepo、build/CIは現存 | 一般利用者向けのセットアップ・配布はこのIssueのOut of scope相当。[#39](https://github.com/tomiya7688/Open_karaoke/issues/39) |
| [#5 Analysis Service](https://github.com/tomiya7688/Open_karaoke/issues/5) | モデルAdapter、Python worker、認証付き内部API、Job cancel/監視の土台は存在 | 公式モデルのruntime/重みと実推論品質、全曲UXは別。[#17](https://github.com/tomiya7688/Open_karaoke/issues/17) / [#36](https://github.com/tomiya7688/Open_karaoke/issues/36) |
| [#9 F0 detector](https://github.com/tomiya7688/Open_karaoke/issues/9) | YIN/NACFの切替・ensemble・evidenceと合成Evaluatorは存在 | 実歌唱のvoicing、cent、octave error、heuristic confidenceの校正は未完。[#42](https://github.com/tomiya7688/Open_karaoke/issues/42) / [#108](https://github.com/tomiya7688/Open_karaoke/issues/108) |
| [#10 Vocal Event](https://github.com/tomiya7688/Open_karaoke/issues/10) | acoustic/F0/lyric evidenceから境界を推定。単体/合議と合成回帰あり | **双方向の共有は未完成**。全曲解析はalignment→events→notesなので、eventsを使った再alignmentはない。notes候補をeventsへ戻す既定の経路もない。[#108](https://github.com/tomiya7688/Open_karaoke/issues/108) |
| [#11 Score generation](https://github.com/tomiya7688/Open_karaoke/issues/11) | F0候補・境界・キー・時系列DP・正規notes.jsonあり | **外部candidate契約はあるが全曲フローへ接続未完**。単体 `NotesAdapter`の`candidate_artifact`は、全曲 `SongAdapter` のユーザー指定`*_artifact`禁止と整合させた専用入口が必要。[#37](https://github.com/tomiya7688/Open_karaoke/issues/37)。手動正解譜面の実録評価・精度校正は[#42](https://github.com/tomiya7688/Open_karaoke/issues/42) |
| [#12 Rust Audio Engine](https://github.com/tomiya7688/Open_karaoke/issues/12) | CPAL/WASAPI shared、48kHz queue、raw mic、clock/epoch、WAV feeder、synthetic testsあり | **実機遅延・hotplug・全曲再生から採点までの制御は未完**。物理音響確認は[#40](https://github.com/tomiya7688/Open_karaoke/issues/40)/[#19](https://github.com/tomiya7688/Open_karaoke/issues/19)、モニタ混合は[#13](https://github.com/tomiya7688/Open_karaoke/issues/13)、Core歌唱セッションは[#107](https://github.com/tomiya7688/Open_karaoke/issues/107) |
| [#52 Artifact I/O delegation](https://github.com/tomiya7688/Open_karaoke/issues/52) | `artifact_io.py` と既存Adapterの委譲を確認。テストあり | 別の同等関数を作る前に共通関数へ寄せる。既存結果を未完扱いにしない |
| [#53 Redirect policy](https://github.com/tomiya7688/Open_karaoke/issues/53) | `DownloadPolicy` / `open_download` を使用。旧`HttpsRedirect`具象継承は現行コードに存在しない。HTTPSダウングレード拒否等のテストあり | 実上流との通信・重み配布条件検証はモデルCI/配布に依存 |
| [#70 Local check](https://github.com/tomiya7688/Open_karaoke/issues/70) | `scripts/check_local.ps1` の単一入口と関連CIが現存 | ローカル簡易検査は実歌唱精度・実機・製品配布ゲートの代替ではない |
| [#82 / #90](https://github.com/tomiya7688/Open_karaoke/issues/82) | Windows venv中継実行ファイルとPID照合に対するCore・Pythonテストコードが現存 | `#99` / `#101` は #82 の重複起票。新しい独立設計漏れとして数えない |
| [#83 / #100](https://github.com/tomiya7688/Open_karaoke/issues/100) | `lyric_evaluator`はdetail→summaryの順に別々に`os.replace`。相互SHA-256と`verify_detail_binding`・途中失敗テストあり | **2ファイルのOSレベル原子公開はない**。読み手がhashを検証した場合のみ不一致を発見できる。将来の両ファイルconsumerへ検証を必須化する。[#18](https://github.com/tomiya7688/Open_karaoke/issues/18)。#102は#100の重複としてClose |

Close済みIssueは**16件**。表では明確な重複群をまとめて表示している（#1,#5,#9,#10,#11,#12,#52,#53,#70,#82,#83,#90,#99,#100,#101,#102）。コードがあるだけで実録音品質の受け入れ条件まで満たしたことにはしない。

## 設計で特に漏れやすい依存関係

### 1. Alignment → Vocal Event → Alignment は同じパスにできない

現行の `SongAdapter` では `alignment` を先に実行し、`events` がその出力を消費する。`alignment.py`は `vocal_events.json` を読まない。

Closed #10の「歌詞alignmentと境界evidenceの共通利用」を完成させるなら、**二段階の別版**として定義する。

```text
alignment-pass-1 ──> events-pass-1 ──> optional alignment-pass-2
        └───────────────────────────────────────┐
                                       note generation
```

後続passの入力hashとversionを固定し、変更のあった下流artifactのみ再生成する。単純な循環参照や無制限の反復を導入しない。改善効果はメリスマ/レガートを含む人手注釈付き実歌唱で比較する。#108に受入条件を追記済み。

### 2. 外部譜面を全曲Jobの `stage_options` に直結できない

`NotesAdapter`は外部candidate artifactを読めるが、`SongAdapter`は全`*_artifact`指定をパイプラインが所有する配線として拒否する（改ざん対策として合理的）。

外部MIDI等は**別の検証済み入力**としてsnapshotし、入力音源との照合・offset/transpose・candidate有効範囲を確認してから Notes段へ渡すべき。`stage_options`の禁止を一律解除しない。#37に受入条件を追記済み。

### 3. 「高精度」よりも実測の境界を明確化

Closed #9/#10/#11のCIは、合成tone、注釈fixture、正規化・hash・時間軸などの機能の回帰には有効。**歌詞・譜面・音程の実歌唱精度を測ったものではない**。

実曲評価セット（#42）とEvaluator（#18）で、特に以下を階層別に測る:

- F0: voicing precision/recall、cent median/p95、octave error、声域・ファルセット・楽器混入
- Events: note/lyric boundary precision/recall/F1、レガート/メリスマ・非歌唱呼吸
- Notes: onset/offset、音符数の過分割/不足、note F1、人手正解譜面との差異

閾値を架空の合格率で決めず、権利処理済み開発/holdoutで校正する。

### 4. Playbackと採点の統合は別の責務

Closed #12の`AudioIo`は実装済みでも、GUIやSongStoreが`play`したら曲の終端まで追跡し、マイクのclockと結果を保存する**製品側所有者**は未実装。

[#107](https://github.com/tomiya7688/Open_karaoke/issues/107)の `KaraokeSessionController` へ集約し、seek・hotplug・device calibration未完時の評価除外・復旧を状態遷移で定義する。物理loopback p95は別の実機ゲート（#40/#19）。

### 5. 2ファイルの整合と原子性は別

Close済み #83/#100 の実装は**検証可能なペア**を提供する。検証を呼び出さない既存/将来の読み手が古いsummaryと新しいdetailをそのまま使えば失敗に気づけない。後続の`#18`で両方読む箇所は必ず`verify_detail_binding`を呼び、失敗時は集計を受理しない。真の全ファイル同時公開が必要になった場合のみ、世代ディレクトリと単一commit pointerを別途設計する。

## 次の作業に引き継いだ先

- [#108への追記事項](https://github.com/tomiya7688/Open_karaoke/issues/108): Closed #10の双方向利用を有界な2-passとして設計
- [#37への追記事項](https://github.com/tomiya7688/Open_karaoke/issues/37): Closed #11の外部candidateを安全な全曲入力へ接続
- [#18への追記事項](https://github.com/tomiya7688/Open_karaoke/issues/18): Close済み評価JSONペアの読込時hash検証

IssueをCloseしたままでも、未達条件に**現行Open Issueの所有者・受入条件**が付けば追跡できる。重複Issueを再起票・再Openしない。
