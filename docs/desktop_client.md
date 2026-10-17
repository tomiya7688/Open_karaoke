# Avalonia desktop client

Issue #15の初期GUIシェル。Rust Core API以外へ接続せず、Python・音声callback実装をGUIへ持ち込まない。

## できること

- Core実行ファイル・Python・データ保存先を設定し、認証付きCoreを子プロセスとして起動する。
- 接続状態・セッションID・登録Adapter数を表示する。Adapter登録とモデル重み導入済みは区別する。
- 接続テスト用 `analysis.mock` Jobを発行し、状態・進捗・キャンセル・失敗を表示する。
- 切断や画面終了時は自分で起動したCoreを終了し、そのCoreがPython workerを停止する。
- 接続確認は2秒ごと。Job進捗は200ms間隔のsnapshot取得。今回はSSEクライアントではない。
- 起動中の切断、遅れて返った旧Job、キャンセル返答による終了状態の逆行を防ぐ。

**楽曲全体の解析・再生・採点を完成した画面ではない。** 全曲Jobは#36、カラオケ画面は#16、
採点は#14で実装する。接続テスト成功を実歌唱精度や可聴再生の証明にしない。

## 開発環境での起動

[Development](development.md)のRust/.NET8/Python環境を準備する。

```text
python -m pip install -e ./python[dev]
cargo build --locked -p open-karaoke-core
dotnet run --project gui/OpenKaraoke.Gui
```

設定タブのCoreパスに `target/debug/open-karaoke-core.exe`（Windows）または
`target/debug/open-karaoke-core`（Linux）の**絶対パス**を指定する。
Python仮想環境を使う場合は、そのpython実行ファイルの絶対パスも指定する。
空欄ならCore既存の同梱/既定Python選択を使う。設定は起動中のみ保持し、秘密tokenは入力・表示・保存しない。
`OPEN_KARAOKE_CORE` / `OPEN_KARAOKE_PYTHON` で初期パスを指定することもできる。

Coreの直接起動は [Desktop session](desktop_session.md) のパイプbootstrapが必要になった。
従来の認証なしHTTP例をそのまま実行して401を無視しない。
一般ユーザー向けランタイム同梱は#39の後続作業。

## 責務

`gui/OpenKaraoke.Client/` はGUIライブラリに依存しない.NET8クライアント。
`CoreApiClient` は固定loopbackルート・JSON上限・認証/版・応答検証、`CoreConnection` は
秘密のパイプ受け渡し・readiness照合・プロセス所有、`ShellViewModel` は接続/Job状態を担当する。
UIの同期コンテキストからViewModelを呼ぶ。GUIのMainWindowは画面とイベント配線だけを持つ。
通信errorでは古いJobを新セッションへ自動送信しない。

## 検証

```text
dotnet run --project gui/OpenKaraoke.Client.Tests --configuration Release -- --unit
dotnet run --project gui/OpenKaraoke.Client.Tests --configuration Release -- <absolute-core-path> <absolute-python-path>
```

前者は13個のHTTP/状態遷移シナリオ、後者は加えて実Core→Pythonの起動・認証・完了・失敗・
キャンセル・パイプ終了・再起動・旧Job404を検証する。専用CIはWindows/Linuxの両方で後者を実行する。
これは依存を増やさないconsole型contract runnerであり、失敗時は非0終了する。

OSの実画面操作・マイク・スピーカー・長時間稼働は別途検証が必要。
APIの脅威モデルと未対応ACL・購読上限は [Core API](core_api.md) を参照する。
