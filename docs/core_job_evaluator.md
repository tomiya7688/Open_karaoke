# Evaluator用Core接続（#18.5 / #18.6a / P1）

`open_karaoke_analysis.core_job_client.CoreJobClient` はGUIやPython workerの内部APIに依存せず、製品Coreの公開HTTP APIへJob作成・取得のmessageを送る。

## 所有者との境界

launcherがCoreを起動し、bootstrap token・readyのportとinstance_idを所有する。クライアントへはメモリ内で渡す。tokenを引数、環境変数、ファイル、URL、ログへ出すCLIは追加しない。Core起動・停止やプロセス所有権はクライアントの責務ではない。[session契約](desktop_session.md) を維持する。

クライアントは明示portを持つ `http://127.0.0.1:port` または `http://[::1]:port` だけを受け入れる。最初のJob操作前に認証付き `/session` でAPI版とinstance_idを照合する。以降も全要求へBearerとAPI版1を付ける。名前解決、環境proxy、redirect、自動再送、別Coreへのfallbackは使用しない。

## 公開message

```python
client = CoreJobClient(core_url, session_token, ready_instance_id, timeout=5)
created = client.create_job({"kind": "framework-smoke", "steps": 1})
current = client.get_job(created.id)
```

この例の接続情報は起動元のメモリ内変数であり、実tokenをコピーして実行する手順ではない。`JobSnapshot` は不変の `id / kind / status / progress` のみを返す。queued、running、completed、failed、cancelledを区別し、Job自体のfailed/cancelledは通信例外に置き換えない。Core応答に含まれる時刻・error本文・artifactパス等の他項目は読み捨て、公開snapshotへ返さない。

POSTは202、GETは200だけを受理し、認証失敗・429・redirect・不正JSON・ID不一致等は `CoreJobError` にする。例外へtoken、URL、送信本文、サーバーのerror本文を埋め込まない。送信要求は64KiB、応答は1MiB上限。`timeout` は0超30秒以下のsocket I/O予算であり、Job全体の期限ではない。遅い本文の総受信時間や解析全体をこの値だけで制限できるとは扱わない。

通信前のURL・token・instance・timeout・作成要求JSON・要求サイズ・取得IDの不正は `ValueError` で拒否する。応答の契約違反と区別する。

POST後に通信・応答検証が失敗してもCore側でJobが作成されている可能性がある。自動再送はしない。`verify_session()` はJobを作成せずsessionだけを明示照合し、成功後は同じclient内で再照合しない。polling、Job全体の期限、キャンセル方針、benchmark集計は#18.6b以降とする。

## 所有Coreの起動と終了（#18.6a）

Python側の `open_karaoke_analysis.owned_core.OwnedCore` がEvaluator専用のCoreを一つ起動する。C# desktop launcherとは別の利用者だが、同じstdin bootstrapと公開API契約を使う。実行前にCoreをビルドし、解析パッケージを導入したPythonと専用データ場所を用意する。

```python
with OwnedCore.start(core_executable, evaluator_data_directory, python_executable) as owned:
    job = owned.client.create_job({"kind": "framework-smoke", "steps": 1})
```

3パスは絶対パスで、CoreとPythonは存在するファイルに限定する。shellなし・Coreのフォルダーをcwdとして起動し、`OPEN_KARAOKE_BIND=127.0.0.1:0`、専用データ場所とworker Pythonを子の環境へ設定する。新しい64文字tokenをstdinへLF付きで一度だけ送り、以後stdinを保持する。tokenを受け取る公開プロパティはない。

readyはLF込み8KiB以内、重複キー不可でevent・整数API版・正規UUID・整数portを検査する。認証付きsession照合までを `startup_timeout`（既定45秒、0超120秒以下）で待つ。期限超過・不正ready・session不一致時は所有子を終了し、別の既存Coreへ接続しない。stdout/stderrは固定サイズで排出し、保存・表示せず、例外にも含めない。終了したownerからclientを再取得することはできない。取り出し済みclientをownerの外へ保持して再利用しない。

`close()` またはwith終了時はstdinを閉じ、`shutdown_timeout`（既定8秒、0超30秒以下）待つ。超過時はWindowsで所有PIDのtree、POSIXで専用sessionのprocess groupだけを強制終了し、さらに最大5秒待つ。正常停止確認後のcloseは冪等。停止確認が失敗すれば `CoreLaunchError` を返し、再度closeできる。起動待ちの期限とcleanup予算は別であり、起動失敗の返却にはcleanup時間も加わる。Job全体の期限や起動中のOS障害を含むhard real-time保証ではない。

境界テストは偽のプロセスとHTTP clientを使う。`OPEN_KARAOKE_TEST_CORE` にビルド済みCoreの絶対パスを指定した場合だけ実Core・実Python workerのテストも行う。workerは既定でテスト実行Pythonを使い、`OPEN_KARAOKE_TEST_PYTHON` で別の絶対パスを指定できる。Windowsのvenv redirector等、起動PIDと実行PIDが変わる実行ファイルはCoreのPID照合を満たさないため、パッケージを利用できる直接のPython実行ファイルを指定する。開発用の `PYTHONPATH` を使う場合もCoreのcwdに依存しない絶対パスが必要。PID照合の緩和や別Pythonへの自動fallbackは行わない。

この結合テストの解析はmock-v1であり、実モデル精度・GUI・benchmarkの証拠ではない。CIのdesktop-integrationでは実Coreをビルド後、このテストを両OSで実行する。

## 整形と確認

Python開発依存を導入した環境で、リポジトリ直下から次を実行する。Ruff 0.17.2・Prettier 3.6.2を既存の固定依存から使う。

```text
npm run format:core-job-client
npm run format:check:core-job-client
python -m pytest python/tests/test_core_job_client.py python/tests/test_owned_core.py -q
```

対象はクライアント・launcherと対応テスト、この説明、npm設定、desktop-integration workflowのみ。生成物や担当外dirtyは整形しない。既存Markdown索引の追記は従来の表記を保持して確認する。既存CIの全Python検査にも同じクライアントとテストが含まれる。
