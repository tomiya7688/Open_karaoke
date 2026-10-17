# Desktop / Core session

## 所有者と秘密の受け渡し

Issue #41のCoreセッション認証と、Issue #15のネイティブクライアント接続の契約。
1つのlauncherが1つのCoreを所有し、信頼されたクライアントが同じセッション能力を使う。

1. OSの暗号学的乱数32バイトから64文字の小文字hex tokenを作る。毎回生成し、保存・再利用しない。
2. Coreをshellなしで起動し、stdin/stdout/stderrをそれぞれパイプへリダイレクトする。
3. stdinへ `token + LF` を1回だけ書きflushする。CRLFも許可。tokenは最大64文字で固定。
4. stdinは以後書かず、Coreの所有期間中保持する。EOFまたは追加入力でCoreは停止する。
5. stdoutの1行JSON `{"event":"core_ready","api_version":1,"instance_id":"UUID","port":12345}` を読む。
6. 数値loopbackのURLへ認証付き`GET /session`を送り、版・instance_idを照合する。

ready情報は秘密ではない。tokenはready/stdout/stderr、コマンドライン、環境変数、設定、ファイル、
URLクエリに書かない。既存Python workerの認証情報とも共有しない。
接続クライアントはHTTP proxyとredirectを無効にし、別hostへtokenを送らない。

## 起動・終了・再接続

Coreのbootstrapはlistenとworker起動より前に検証する。launcherは起動タイムアウトを設け、
不正ready・API版違い・認証失敗を明示する。任意の既存localhostサービスへフォールバックしない。

正常終了ではstdinを閉じ、Coreが`AnalysisService::shutdown()`を完了するのを待つ。
launcher異常終了でもパイプEOFが停止を知らせる。期限内に終了しなければlauncherが
**自分で起動したプロセスツリーだけ**を終了する。別のCoreや任意PIDへ操作しない。

Core再起動は新しいtokenとinstance_idを使う。古いtokenは新Coreで401。
Jobはメモリ内なので古いIDは404。新セッションへ旧Jobを自動再送しない。
同じセッション内のSSE再接続は最新snapshotを返し、時系列イベントの全履歴再生ではない。

## 脅威モデルと非対象

認証なしの他プロセス、ブラウザOrigin、DNS名Host、不正token/版を拒否する。
全ネットワークルートは認証middlewareを通り、GET /healthだけを公開する。
URL中のtokenを認証に使わず、Authorizationの重複も拒否する。
固定長比較は秘密依存の早期returnを避けるが、物理サイドチャネル監査を行ったという意味ではない。

OSユーザーが同じでメモリや継承ハンドルを読める相手、管理者、改変されたlauncher/Core、
tokenを共有したクライアント同士の分離はこの能力モデルの対象外。
マルチユーザーACL、クライアント別artifact認可、SSE/HTTP接続総数とtimeout制御は後続。
インターネット公開サービスとして使用しない。

## 検証

`cargo test --locked --workspace --all-targets` はbinaryのsession単体テストも実行する。
bootstrapの欠落・不正・長すぎる値、LF/CRLF、全ルートの401、Origin/Host/version、
重複Authorization、クエリtoken、旧token、正常Job作成を確認する。
既存`job_api`のin-processテストは低水準契約を別途維持する。
実launcher/Core/Python間のパイプと終了はdesktop integrationで別に検証する。
