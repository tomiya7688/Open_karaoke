# Desktop UI validation

`gui/OpenKaraoke.Gui.Smoke` は製品の `App` と `MainWindow.axaml` をそのままロードし、
テスト専用Avalonia.Headless 11.3.4とSkiaで描画する。HeadlessはAvaloniaのMITライセンスの
テスト用パッケージであり、本番GUIの依存に追加しない。

引数: `<output-directory> <absolute-core-path> <absolute-python-path>`。
全引数は必須。mockだけへ黙って切り替えるモードはない。

実行時は以下を検査し、6枚のPNGを成果物へ保存する。

1. 実XAMLの起動、未接続時のボタン無効化。
2. 設定タブへの移動とTwoWay binding、不正実行ファイルのerror、780×580の最小画面。
3. 実Core/Pythonをパイプ認証で起動し、接続状態を表示。
4. 画面のClickイベントからJobを作り、進捗とキャンセル可能状態を表示。
5. 実workerのmock Job完了を表示。
6. 次のJobをキャンセルし、画面終了で所有Core/workerを停止。

描画の偽backendは使わず、`UseHeadlessDrawing=false` による画像を保存する。
PNGの成功だけでレイアウト品質が自動承認されるわけではなく、成果物の目視確認も行う。
表示されるsession IDとJob IDは秘密tokenではない。tokenを画面へ表示しない。
CIの画像に使うデータは新規temporary test directoryだけで、ユーザー曲や録音は扱わない。

これは**headless上の実GUIコード・実Core/Python結合**であり、
Windows/Linuxの実ウィンドウ管理、物理マウス/IME、実マイク/スピーカーを操作した証拠ではない。
ネイティブ環境での対話操作・機材品質・長時間稼働は別に検証する。

開発起動は [Desktop client](desktop_client.md)、認証契約は [Desktop session](desktop_session.md)。
