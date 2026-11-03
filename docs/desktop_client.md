# Avalonia desktop client

Issue #15の初期GUIシェル。Rust Core API以外へ接続せず、Python・音声callback実装をGUIへ持ち込まない。

## できること

- Core実行ファイル・Python・データ保存先を設定し、認証付きCoreを子プロセスとして起動する。
- 接続状態・セッションID・登録Adapter数を表示する。Adapter登録とモデル重み導入済みは区別する。
- 接続テスト用 `analysis.mock` Jobを発行し、状態・進捗・キャンセル・失敗を表示する。
- 切断や画面終了時は自分で起動したCoreを終了し、そのCoreがPython workerを停止する。
- 接続確認は2秒ごと。Job進捗は200ms間隔のsnapshot取得。今回はSSEクライアントではない。
- 起動中の切断、遅れて返った旧Job、キャンセル返答による終了状態の逆行を防ぐ。
- 「曲・歌詞」タブで取り込み済みの曲一覧(`GET /songs`、全ページ)を表示し、選んだ曲の歌詞と目標ノート
  (`GET /songs/{id}`)を読み込む。破損した曲は一覧に理由付きで出し、表示はできない。
  曲の取得失敗はエラー表示だけで、Core接続は切らない(前の曲の表示は消す)。読み込み中は選択を変えられない。
  一覧は最大50ページ(5000曲)で止め、続きが残るときは「先頭の…曲」と表示する。同じページ指定を繰り返す応答は拒否する。

## 歌詞・音程バー表示(#16の最初の段階)

位置はすべてCoreと同じ48kHzの整数sampleで扱う(`SongTimeline`)。秒への変換は表示とスライダー操作のためだけに行う。

- 現在の歌詞: 開始sampleを含み、終了sampleを含まない区間。重なる場合は後から始まった行を出し、
  その行が終わると、まだ続いている前の行へ戻る。Coreの保存形式は並び順を保証しないため、読み込み時に開始順へ並べる。
- 次の歌詞: 現在位置より後に始まる最初の行。
- 音程バー: 現在位置の1秒前から5秒後までと重なるノートを、左から1/6の位置の線に合わせて描く。
  ノートも開始を含み終了を含まない区間として扱い、現在位置を含むノートを強調して最前面に描く。
  縦軸はMIDIノート番号(0〜127)で、その曲の最低音・最高音の前後2半音を表示範囲にする。
- 計算は `KaraokeViewModel`(GUIライブラリ非依存)が行い、`PitchBarView` は渡された比率を描くだけにする。

**ここまでは表示位置をスライダーで指定する確認用画面で、カラオケ再生は未完成。** [Audio clock](audio_clock.md)の契約どおり、
GUIは独自の再生時計を作らない。伴奏の再生とCoreが報告する再生位置への追従は#12、マイクの音程表示と採点結果は#14と組み合わせて追加する。
`GET /songs/{id}` の応答は、Coreが文書ごとに許す16MiBの4文書分に1MiBを足した65MiBまで読む。
曲一覧を含む他のAPIの応答は従来どおり1MiBまで。どちらもContent-Lengthがなくても読みながら打ち切る。
ヘッダー待ちも本文の受信が止まった応答も、要求から受信完了までの期限(曲データ60秒、他は10秒)で打ち切る。

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

前者は23個のHTTP/状態遷移・曲時間軸シナリオ、後者は加えて実Core→Pythonの起動・認証・完了・失敗・
キャンセル・パイプ終了・再起動・旧Job404を検証する。専用CIはWindows/Linuxの両方で後者を実行する。
これは依存を増やさないconsole型contract runnerであり、失敗時は非0終了する。

OSの実画面操作・マイク・スピーカー・長時間稼働は別途検証が必要。
APIの脅威モデルと未対応ACL・購読上限は [Core API](core_api.md) を参照する。
