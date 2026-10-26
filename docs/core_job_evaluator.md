# Evaluator用Core接続と単一Job実行（#18.5 / #18.6a / #18.6b / P1）

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

POST後に通信・応答検証が失敗してもCore側でJobが作成されている可能性がある。自動再送はしない。`verify_session()` はJobを作成せずsessionだけを明示照合し、成功後は同じclient内で再照合しない。単一Jobの実行は下記#18.6b、複数trialの集計とCLIは#18.6cで段階導入する。

## 所有Coreの起動と終了（#18.6a）

Python側の `open_karaoke_analysis.owned_core.OwnedCore` がEvaluator専用のCoreを一つ起動する。C# desktop launcherとは別の利用者だが、同じstdin bootstrapと公開API契約を使う。実行前にCoreをビルドし、解析パッケージを導入したPythonと専用データ場所を用意する。

```python
with OwnedCore.start(core_executable, evaluator_data_directory, python_executable) as owned:
    job = owned.client.create_job({"kind": "framework-smoke", "steps": 1})
```

3パスは絶対パスで、CoreとPythonは存在するファイルに限定する。shellなし・Coreのフォルダーをcwdとして起動し、`OPEN_KARAOKE_BIND=127.0.0.1:0`、専用データ場所とworker Pythonを子の環境へ設定する。新しい64文字tokenをstdinへLF付きで一度だけ送り、以後stdinを保持する。tokenを受け取る公開プロパティはない。

readyはLF込み8KiB以内、重複キー不可でevent・整数API版・正規UUID・整数portを検査する。認証付きsession照合までを `startup_timeout`（既定45秒、0超120秒以下）で待つ。期限超過・不正ready・session不一致時は所有子を終了し、別の既存Coreへ接続しない。stdout/stderrは固定サイズで排出し、保存・表示せず、例外にも含めない。終了したownerからclientを再取得することはできない。取り出し済みclientをownerの外へ保持して再利用しない。

`close()` またはwith終了時はstdinを閉じ、`shutdown_timeout`（既定8秒、0超30秒以下）待つ。Windowsは期限超過時に所有PIDのtreeを強制終了し、POSIXは後述の手順で専用sessionのprocess groupを停止して、さらに最大5秒待つ。正常停止確認後のcloseは冪等。停止確認が失敗すれば `CoreLaunchError` を返し、再度closeできる。起動待ちの期限とcleanup予算は別であり、起動失敗の返却にはcleanup時間も加わる。Job全体の期限や起動中のOS障害を含むhard real-time保証ではない。

POSIXでは生存照会も `waitid(WNOWAIT)` を使い、PIDを回収せず保持する。Coreが先に異常終了した場合を含め、正常終了観測または期限後に専用groupへSIGKILLを送り、その後にCoreをwaitして回収する。回収済みPIDへ再送しないよう、pipe cleanupの再実行ではgroup操作とwaitを繰り返さない。この所有者以外のSIGCHLD handler等で子を回収しないこと。WNOWAITを利用できないPOSIX環境は起動前に拒否する。[Pythonのwaitid/WNOWAIT契約](https://docs.python.org/3.12/library/os.html#os.WNOWAIT)に従う。Linuxではleader異常終了後に子孫だけが残る実プロセスの回帰テストも実行する。

境界テストは偽のプロセスとHTTP clientを使う。`OPEN_KARAOKE_TEST_CORE` にビルド済みCoreの絶対パスを指定した場合だけ実Core・実Python workerのテストも行う。workerは既定でテスト実行Pythonを使い、`OPEN_KARAOKE_TEST_PYTHON` で別の絶対パスを指定できる。Windowsのvenv redirector等、起動PIDと実行PIDが変わる実行ファイルはCoreのPID照合を満たさないため、パッケージを利用できる直接のPython実行ファイルを指定する。開発用の `PYTHONPATH` を使う場合もCoreのcwdに依存しない絶対パスが必要。PID照合の緩和や別Pythonへの自動fallbackは行わない。

この結合テストの解析はmock-v1であり、実モデル精度・GUI・benchmarkの証拠ではない。CIのdesktop-integrationでは実Coreをビルド後、このテストを両OSで実行する。

## 一回のJob実行（#18.6b）

`open_karaoke_analysis.job_runner.JobRunner` は一つの所有済みCoreを消費する。設定生成はI/Oなしで、`timeout` は0超3600秒以下、`poll_interval` は0.01以上10秒以下。既定は30秒と0.2秒。

```python
runner = JobRunner(timeout=30, poll_interval=0.2)
owner = OwnedCore.start(core_executable, evaluator_data_directory, python_executable)
result = runner.run(owner, {"kind": "framework-smoke", "steps": 1})
```

`run` は作成を一度だけ依頼し、queued/runningの間、同じID・kindの状態を有限間隔で取得する。自動再送・別sessionへの再作成・Core再起動はしない。作成応答が終端ならpollingは不要。requestはCore公開要求で、実行中に他の呼び出し元から変更しない。詳細な入力検証は既存client/Coreの契約を使う。

`JobRunResult` は不変の3項目のみを持つ。入力・token・Job ID・エラー本文・artifactパスは含まない。

作成応答が終端の場合と、最初のGET前の通信失敗では`poll_attempts`は0になる。GETを試みた通信失敗はその試行も数える。

| 項目              | 契約                                                                                                  |
| ----------------- | ----------------------------------------------------------------------------------------------------- |
| `outcome`         | `completed`、`failed`、`cancelled`、`timed_out`、`transport_error`。Job失敗・取消と通信失敗を区別する |
| `elapsed_seconds` | 作成要求前から呼び出し側の判定までの単調時計の秒。Core起動とcleanupは除く                             |
| `poll_attempts`   | 状態取得messageの試行数。HTTP到達や成功の件数ではない。期限超過時は観測が未確定のため`None`           |

全体期限はPOST・GETの通信待機とpolling待機を含む。socket timeoutだけに依存せず、observerを別threadで実行して呼び出し側も期限を監視する。期限内に結果を受け取れなければ`timed_out`とし、遅い完了応答を成功にしない。受け取った入力拒否・予期しないobserver例外は、foregroundの判定時刻が期限を跨いでも例外として伝える。polling待機は残り予算で切り詰め、期限後の追加GETを避ける。POSTが届いたか不確定な場合も再送しない。

正常・失敗・例外・期限超過のいずれでもownerを一度closeし、そのCoreを以後使わない。期限超過時もownerを閉じ、Coreのshutdown契約で未完了Jobを停止する。個別cancel endpointを送った、またはJobのcancelled終端を確認したという意味ではない。cleanupは別予算のため、`run`の返却時間はJob全体期限より長くなることがある。OS停止やPython実行停止を含むhard real-time保証はしない。

不正要求は本文なしの`ValueError`、予期しないobserver失敗は`JobRunError`を返す。`run`内で不正要求を拒否した場合もownerはcloseされる。runner生成時の設定検証はownerを受け取る前に行う。Core終了失敗の`CoreLaunchError`は通常結果や観測エラーより優先する。Coreを閉じた後もobserverが1秒以内に停止しなければ、停止未確認の`JobRunError`にし、正常に片付いたとは扱わない。優先順位はCore終了失敗、observer停止未確認、通常の観測結果・エラーの順になる。runnerが期限超過を返しても、解析結果や成功率を推定しない。

実Core/workerの結合テストはmock-v1の完了・失敗・長いJobの期限超過とruntime cleanupを確認する。取消の終端、通信待機中の期限、observer異常は境界テストで別途検証する。複数trialの統計、JSON保存、CLI、精度回帰ゲートへの接続は#18.6c以降であり、今回の計測値は実モデル精度の指標ではない。

## 整形と確認

Python開発依存を導入した環境で、リポジトリ直下から次を実行する。Ruff 0.17.2・Prettier 3.6.2を既存の固定依存から使う。

```text
npm run format:core-job-client
npm run format:check:core-job-client
python -m pytest python/tests/test_core_job_client.py python/tests/test_owned_core.py python/tests/test_job_runner.py -q
```

対象はクライアント・launcher・runnerと対応テスト、この説明、npm設定、desktop-integration workflowのみ。生成物や担当外dirtyは整形しない。既存Markdown索引の追記は従来の表記を保持して確認する。既存CIの全Python検査にも同じ実装とテストが含まれる。
