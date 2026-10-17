# Rust Core API

## 起動と認証

Core実行ファイルはloopback専用。既定は `127.0.0.1:32145`、起動元は
`OPEN_KARAOKE_BIND=127.0.0.1:0` でOS割り当てポートを選べる。

**実行ファイルをネットワークへ公開する入口はセッション認証付きです。**
起動元が暗号学的乱数32バイトを生成し、64文字の小文字hexと改行をCoreの
リダイレクトしたstdinへ渡す。認証情報を引数・環境変数・URL・readyファイルへ置かない。
stdinを保持する起動元がセッションを所有し、閉じるとCoreとPython workerを停止する。
対話端末からの直接起動、不完全・不正なbootstrapは拒否する。
詳しいプロトコルと脅威モデルは [Desktop session](desktop_session.md) を参照。

`GET /health` だけは認証不要。それ以外は `Authorization: Bearer <token>` が必須。
`X-Open-Karaoke-Api-Version: 1` を付ける。版ヘッダー省略は移行用にv1として扱うが、
明示版不一致・重複は426。JobとSSEの既存フィールドは維持する。

## 公開エンドポイント

| Method | Path | 用途 |
|---|---|---|
| GET | `/health` | Coreの生存確認。`api_version: 1`、秘密情報なし |
| GET | `/session` | 認証済みの`instance_id`とAPI版の確認 |
| GET | `/devices` | Windows WASAPIのデバイス一覧（他OSは503） |
| GET | `/analysis/health` | 監視中Python workerのhealth |
| GET | `/models` | workerに登録済みのAdapter一覧 |
| POST | `/jobs` | Job作成（202） |
| GET | `/jobs/{id}` | Job状態・成果物 |
| POST | `/jobs/{id}/cancel` | Jobキャンセル要求 |
| GET | `/jobs/{id}/events` | 最新JobスナップショットのSSE配信 |

GUI・Evaluatorはこの境界のみ利用する。Python workerの `POST /analysis/{role}` は
別の認証情報を持つ内部APIであり、GUIから直接接続しない。

## 制限とエラー

- 認証欠落・不正・重複したAuthorizationは401。`WWW-Authenticate: Bearer` を返す。
- 認証対象ルートの `Origin` または `Sec-Fetch-Site` は403。CORSは許可しない。
- 認証済み要求には数値loopbackの `Host` が必要。不正・重複は400。DNS名への再解決はしない。
- JSON本文は64 KiBまで。超過413。音声の実体ではなく管理対象artifactを参照する。
- 未完了Jobは最大16件。超過429。保持履歴は最大512件で、最も古い終了済みJobから破棄する。
- 破棄済み・再起動前のJobは404。終了済みartifactを履歴の破棄に伴って削除することはない。
- SSEは再接続時に保持中Jobの最新snapshotを送る。Event-IDリプレイと永続Job復元は未実装。
- エラーは `{"error":"message"}`。レスポンスは `Cache-Control: no-store`。秘密値をエラーへ含めない。

このモデルは**1つの起動元と、その信頼されたGUI/テストクライアント**用。
同じtokenを共有するクライアント間にJob所有者の区別はない。artifact rootの既存相対パス制限は
維持するが、クライアント別ACL、SSE購読数の制限、永続化、別OSユーザー間の分離は後続の
Issue #41 / #2。メモリやパイプを読める同一権限のマルウェアに対する隔離機構ではない。

## Jobの種類

`kind: analysis.<role>` はCoreからPythonへ転送する。現在のroleは
`mock`, `stems`, `lyrics`, `alignment`, `pitch`, `events`, `notes`, `song`。
`song` の既定Adapterは未実装。モデル・I/Oは [Analysis Service](analysis_service.md) を参照。

起動元がメモリ内に保持したtokenを使う要求例（値を履歴へ直接入力しない）:

```powershell
$headers = @{ Authorization = "Bearer $sessionToken"; 'X-Open-Karaoke-Api-Version' = '1' }
$job = Invoke-RestMethod "$coreUrl/jobs" -Headers $headers -Method Post -ContentType application/json -Body @'
{"kind":"analysis.mock","analysis":{"model_id":"mock-v1","options":{"steps":5,"delay_ms":10}}}
'@
Invoke-RestMethod "$coreUrl/jobs/$($job.id)" -Headers $headers
```

ASR/分離の入力はCoreのartifact root配下へ置き、`analysis.input_artifact` に相対パスを渡す。
通常の絶対パスや外部URLは受け付けない。内部framework smokeは
`{"kind":"framework-smoke","steps":5,"fail_at_step":null}`。
`steps` / `fail_at_step` は解析オプションではない。

## ライフサイクルと保存

Jobは `queued` → `running` → `completed` / `failed` / `cancelled`。
進捗・stage・時刻・error・artifact参照を保持する。CoreとworkerのJob IDは別。
worker停止による失敗を完了扱いせず、旧Jobを新workerへ自動再送しない。

履歴はメモリ内のみ。生成物のartifact directoryと [SongStore](song_format.md) は別で、
Job完了だけで楽曲ライブラリへ自動登録されない。

実装: `core/src/main.rs` / `session.rs` がプロセス・認証境界、`core/src/lib.rs` がJobルーター、
`analysis_service.rs` がworker監視。ライブラリの低水準 `app()` はin-process契約テスト用にも
使うが、**これを直接listenして認証を省略しない**。製品は必ず`CoreSession::router`を経由する。
