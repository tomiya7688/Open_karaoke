# Evaluator用Core Jobクライアント（#18.5 / P1）

`open_karaoke_analysis.core_job_client.CoreJobClient` はGUIやPython workerの内部APIに依存せず、製品Coreの公開HTTP APIへJob作成・取得のmessageを送る。

## 所有者との境界

既存launcherがCoreを起動し、bootstrap token・readyのportとinstance_idを所有する。クライアントへはメモリ内で渡す。tokenを引数、環境変数、ファイル、URL、ログへ出すCLIは追加しない。Core起動・停止やプロセス所有権はクライアントの責務ではない。[session契約](desktop_session.md) を維持する。

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

POST後に通信・応答検証が失敗してもCore側でJobが作成されている可能性がある。自動再送はしない。polling、Job全体の期限、キャンセル方針、benchmark集計、実Core launcherとの結合は次の単位で実装する。今回のtransport mockとloopbackテストサーバーでの実HTTP確認は、実Core/Python/GUIのE2E証拠ではない。

## 整形と確認

Python開発依存を導入した環境で、リポジトリ直下から次を実行する。Ruff 0.17.2・Prettier 3.6.2を既存の固定依存から使う。

```text
npm run format:core-job-client
npm run format:check:core-job-client
python -m pytest python/tests/test_core_job_client.py -q
```

対象は新規クライアントとテスト、この説明、npm設定のみ。生成物や担当外dirtyは整形しない。既存Markdown索引の追記は従来の表記を保持して確認する。既存CIの全Python検査にも同じクライアントとテストが含まれる。
