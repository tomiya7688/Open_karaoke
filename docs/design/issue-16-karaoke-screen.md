# #16 カラオケ再生画面（歌詞・音程バー・歌唱線・結果） 設計

> 状態: 16.1（表示のみ）実装済み。再生連動・歌唱線・結果は未実装。
> 現状の画面は表示位置をスライダーで指定する確認用（[Desktop client](../desktop_client.md#歌詞音程バー表示16の最初の段階)）。

## 目的
実際に歌える最小のカラオケ画面。**GUIは再生時計も採点も所有せず**、Coreが公開する状態（再生提示位置・採点イベント）を描くだけにする。

## 受入条件（[Issue #16](https://github.com/tomiya7688/Open_karaoke/issues/16) から転記）
- 再生位置と歌詞/譜面表示が同期する
- singer pitchがリアルタイム表示される
- GUI描画がAudio callbackをブロックしない
- 曲終了後に採点結果へ遷移できる
- テスト: timeline synchronization、UI rendering smoke、playback seeking regression

## 非目標
- 音声再生・マイク入力・DSP（#12/#13）、F0/採点計算（#14）、セッション状態遷移の所有（#107）
- 歌詞・音符の編集（#38）、初回機材チェック画面（#110）
- 物理デバイスでの音声との目視同期の保証（#40/#19の実機ゲート）

## 設計
**既存（変更しない契約）**: `SongTimeline`（`gui/OpenKaraoke.Client/SongTimeline.cs`）が48kHz整数sampleで歌詞行・ノートを検索し、`KaraokeViewModel`が現在/次の歌詞と`PitchBar`列を計算、`PitchBarView`は描画のみ。

**追加する責務**
- `PlaybackFollower`（Client側・新設）: Coreのセッションスナップショット `{ revision, epoch, presentation_song_sample, clock_quality }`（#107で定義、**現時点APIは未確定**）を受け、`KaraokeViewModel.Seek`相当へ渡す。`revision`が古い、または`epoch`が**現行より古い**更新は捨てる。新しいepoch（シーク・デバイス切替で増える）は受理して現行epochを更新し、歌唱線・暫定採点の表示状態を破棄して再基準化する。
- 表示補間: スナップショット間のみ、単調・有界（例: 最後の値から経過分だけ。上限超過で停止）。補間値を採点や位置の正本にしない。
- `SingerPitchTrack`（新設）: Coreが送る**曲sampleへ写像済み**の観測（#14の14.3後。capture streamの生sampleをGUIで変換しない）を直近N秒のリングで保持し、音程バー座標に変換。無声/拒否区間は線を描かない。epoch切替で破棄。
- `ScoreFeedbackModel` / `ResultViewModel`（新設）: 暫定得点（練習/暫定ラベル付き）と曲終了後の結果。`unscored_reason_counts`と遅延品質（Unknown/Estimated/Measured）を必ず表示し、「精度保証」と読める文言を出さない。

**エラー**: Core切断・stale epoch・状態`interrupted`は画面に明示し、旧位置で再生中に見せない。Core再接続後に旧セッションを自動再開しない。
**スレッド**: UI同期コンテキストでのみViewModelを更新。SSE/購読の受信はバックグラウンド、UIへは有界キュー経由。描画は音声callbackと無関係（Coreと別プロセス）。

## 実装スライス
| # | 内容 | 依存 | 完了条件・テスト |
|---|---|---|---|
| 16.1 | 歌詞・音程バー表示（実装済み） | - | `gui/OpenKaraoke.Client.Tests/SongTests.cs`、`gui/OpenKaraoke.Gui.Smoke` |
| 16.2 | `PlaybackFollower`+再生/一時停止/シーク操作（Core API呼出し） | #107のtransport API, #41の版管理 | モックCoreでstale revision/epoch破棄、シーク往復、Core切断表示（`NativeTests.cs`系） |
| 16.3 | 歌唱線オーバーレイ（`SingerPitchTrack`） | 16.2, #14のイベント | 合成観測列→座標変換、無声で線が途切れる、epoch切替で消える |
| 16.4 | 採点フィードバック+結果画面遷移 | 16.2, #14のScoreResult | 暫定/確定ラベル、未採点理由の表示、曲終了イベントで遷移 |
| 16.5 | headless描画smoke拡張とキーボード操作 | 16.2-16.4 | `Gui.Smoke`で再生中画面が描画され、操作でシークできる |
曲選択→練習（MIDI/既存伴奏）の最短導線を16.2で先に通し、自動解析曲との統合は後。

## 触るファイル
`gui/OpenKaraoke.Client/KaraokeViewModel.cs`・`SongTimeline.cs`（既存、契約維持）、`CoreApiClient.cs`（`ICoreApi`に再生API追加）、新設ファイルは`gui/OpenKaraoke.Client/`配下に分割（`ShellViewModel`へ巨大追加しない）、`gui/OpenKaraoke.Gui/MainWindow.axaml`・`PitchBarView.cs`、`gui/OpenKaraoke.Client.Tests/`、`gui/OpenKaraoke.Gui.Smoke/Program.cs`。`ShellViewModel`は他セッションと競合しやすい。

## 守る規則
- [宣言コメント（JSON-like）必須](../coding_rules.md#5-ソースコードコメント)（C#も`/// { 責務/処理/引数/戻り値 }`）、[UI/Process/Dataの責務分離](../coding_rules.md#2-ui--process--dataの責務)
- GUIはRust Core公開APIのみ。Python・SongStoreファイルへ直接アクセスしない（[architecture](../architecture.md#c--avalonia-gui)）
- 位置はすべて48kHz整数sample。秒は表示とスライダーのみ（[audio_clock](../audio_clock.md)）
- 整形: `dotnet format`（[実装チェックリスト](../implementation_checklist.md)）。Windows実機の目視/IME/高DPIは`Unverified`

## 未決事項
- 再生/購読APIの形（#107/#41で確定。URL例は未確定のため本書では固定しない）
- 補間の上限値と、`clock_quality: unknown`時の表示方針
- 結果画面の保存・履歴（#14のScoreResult保存先に依存）
