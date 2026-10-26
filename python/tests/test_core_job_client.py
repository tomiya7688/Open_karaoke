import errno
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import Mock

import pytest

from open_karaoke_analysis import core_job_client as api

INSTANCE = "11111111-1111-4111-8111-111111111111"
JOB = "22222222-2222-4222-8222-222222222222"
TOKEN = "a" * 64  # Synthetic capability, never a real session.
SESSION = {"instance_id": INSTANCE, "api_version": 1}
SNAPSHOT = {"id": JOB, "kind": "framework-smoke", "status": "queued", "progress": 0}


@pytest.mark.parametrize("host,family", [("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)])
def test_real_loopback_http_messages(monkeypatch, host, family):
    """{
    責務: [loopback上で実HTTP送受信と認証・Host・API版を確認する]
    処理: [1: 有限のテストサーバーを起動, 2: 作成と取得, 3: messageを照合して停止]
    引数: [monkeypatch: proxy環境の設定, host: 数値loopback, family: IP版]
    戻り値: []
    }"""
    calls = []

    class Handler(BaseHTTPRequestHandler):
        """{
        責務: [固定応答だけを返すloopbackテスト境界]
        フィールド: [既存HTTP handlerのrequest情報。Coreや解析workerは起動しない]
        }"""

        def do_GET(self):
            """{
            責務: [sessionまたはJobの固定状態を返す]
            処理: [1: パスを選択, 2: JSON応答]
            引数: []
            戻り値: []
            }"""
            self._respond(SESSION if self.path == "/session" else SNAPSHOT, 200)

        def do_POST(self):
            """{
            責務: [作成要求の固定状態を返す]
            処理: [1: JSON応答を202で送信]
            引数: []
            戻り値: []
            }"""
            self._respond(SNAPSHOT, 202)

        def _respond(self, value, status):
            """{
            責務: [認証付き要求を記録し有限JSONを返す]
            処理: [1: 本文とheaderを保存, 2: 長さ付き応答]
            引数: [value: 固定JSON, status: HTTP状態]
            戻り値: []
            }"""
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append(
                (self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body)
            )
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format, *args):
            """{
            責務: [テスト要求を標準ログへ出さない]
            処理: [1: ログを抑止]
            引数: [format: 既存handler引数, args: 既存handler引数]
            戻り値: []
            }"""

    class Server(HTTPServer):
        """{
        責務: [指定IP版のloopbackだけにbindするテストサーバー]
        フィールド: [address_family: IPv4またはIPv6]
        }"""

        address_family = family

    if family == socket.AF_INET6 and not socket.has_ipv6:
        pytest.skip("IPv6 is not supported by this runtime")
    try:
        server = Server((host, 0), Handler)
    except OSError as error:
        if family == socket.AF_INET6 and error.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
            pytest.skip("IPv6 loopback is unavailable on this host")
        raise
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("HTTP_PROXY", "http://invalid.proxy:1")
        port = server.server_port
        authority = f"[{host}]:{port}" if family == socket.AF_INET6 else f"{host}:{port}"
        client = api.CoreJobClient(f"http://{authority}", TOKEN, INSTANCE)
        assert client.create_job({"kind": "framework-smoke"}) == client.get_job(JOB)
        assert [c[:2] for c in calls] == [
            ("GET", "/session"),
            ("POST", "/jobs"),
            ("GET", f"/jobs/{JOB}"),
        ]
        assert json.loads(calls[1][3]) == {"kind": "framework-smoke"}
        assert all(
            c[2]["authorization"] == f"Bearer {TOKEN}"
            and c[2]["host"] == authority
            and c[2]["x-open-karaoke-api-version"] == "1"
            for c in calls
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _response(value, status=200):
    """{
    責務: [有限のHTTP応答をテスト用に作る]
    処理: [1: JSONまたは生バイトを用意, 2: statusとreadを持つmockへ設定]
    引数: [value: 応答本文, status: HTTP状態]
    戻り値: [HTTP応答mock]
    }"""
    result = Mock(status=status)
    result.read.return_value = value if isinstance(value, bytes) else json.dumps(value).encode()
    return result


def _transport(monkeypatch, replies):
    """{
    責務: [通信messageを観測するtransportを差し込む]
    処理: [1: 応答列を設定, 2: HTTPConnection生成を置換]
    引数: [monkeypatch: 置換境界, replies: 応答列]
    戻り値: [接続mockとfactory]
    }"""
    connection = Mock()
    connection.getresponse.side_effect = replies
    factory = Mock(return_value=connection)
    monkeypatch.setattr(api, "HTTPConnection", factory)
    return connection, factory


@pytest.mark.parametrize("status", ["queued", "running", "completed", "failed", "cancelled"])
@pytest.mark.parametrize(
    "origin,host", [("http://127.0.0.1:12345", "127.0.0.1"), ("http://[::1]:12345/", "::1")]
)
def test_authenticated_messages_and_minimal_snapshots(monkeypatch, status, origin, host):
    """{
    責務: [sessionを一度照合し正しいJob・認証・API版だけを使うことを確認]
    処理: [1: 作成・取得応答を設定, 2: 公開message送信, 3: 状態と送信先を確認]
    引数: [monkeypatch: transport置換, status: 全状態, origin: 数値URL, host: 期待host]
    戻り値: []
    }"""
    snapshot = {**SNAPSHOT, "status": status, "artifacts": ["private/path"], "error": "private"}
    replies = [_response(SESSION), _response(snapshot, 202), _response(snapshot)]
    connection, factory = _transport(monkeypatch, replies)
    monkeypatch.setenv("HTTP_PROXY", "http://invalid.proxy:1")
    client = api.CoreJobClient(origin, TOKEN, INSTANCE)
    assert TOKEN not in repr(client)
    created = client.create_job({"kind": "framework-smoke", "steps": 1})
    assert created == client.get_job(JOB) == api.JobSnapshot(JOB, "framework-smoke", status, 0.0)
    assert not hasattr(created, "artifacts") and not hasattr(created, "error")
    assert factory.call_count == connection.close.call_count == 3
    for reply in replies:
        reply.read.assert_called_once_with(1_048_577)
    factory.assert_called_with(host, 12345, timeout=5)
    calls = connection.request.call_args_list
    assert [(c.args[0], c.args[1]) for c in calls] == [
        ("GET", "/session"),
        ("POST", "/jobs"),
        ("GET", f"/jobs/{JOB}"),
    ]
    assert json.loads(calls[1].kwargs["body"]) == {"kind": "framework-smoke", "steps": 1}
    assert all(
        c.kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"
        and c.kwargs["headers"]["X-Open-Karaoke-Api-Version"] == "1"
        for c in calls
    )


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:12",
        "http://localhost:12",
        "http://example.com:12",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:12/jobs",
        "http://u@127.0.0.1:12",
        "http://127.0.0.1:12?token=x",
        "http://127.0.0.1:12#x",
        "http://127.0.0.1:12\n",
    ],
)
def test_unsafe_origins_rejected_before_connection(monkeypatch, origin):
    """{
    責務: [名前解決・URL秘密・転送先変更の入口を拒否する]
    処理: [1: 不正originで生成, 2: 通信前の拒否を確認]
    引数: [monkeypatch: transport置換, origin: 不正URL]
    戻り値: []
    }"""
    _, factory = _transport(monkeypatch, [])
    with pytest.raises(ValueError):
        api.CoreJobClient(origin, TOKEN, INSTANCE)
    factory.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("token", "bad"),
        ("token", "g" * 64),
        ("token", "A" * 64),
        ("token", "a" * 63 + "\n"),
        ("instance_id", "../jobs"),
        ("instance_id", f"urn:uuid:{INSTANCE}"),
        ("instance_id", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
        ("timeout", True),
        ("timeout", 0),
        ("timeout", -1),
        ("timeout", float("nan")),
        ("timeout", float("inf")),
        ("timeout", 10**400),
    ],
)
def test_invalid_connection_settings(field, value):
    """{
    責務: [token・instance・socket予算の不正を拒否する]
    処理: [1: 設定を一つ変更, 2: 生成時エラーを確認]
    引数: [field: 設定名, value: 不正値]
    戻り値: []
    }"""
    options = {
        "base_url": "http://127.0.0.1:12345",
        "token": TOKEN,
        "instance_id": INSTANCE,
        field: value,
    }
    with pytest.raises(ValueError):
        api.CoreJobClient(**options)


@pytest.mark.parametrize(
    "session",
    [
        {**SESSION, "api_version": True},
        {**SESSION, "api_version": 2},
        {**SESSION, "instance_id": JOB},
    ],
)
def test_session_mismatch_never_submits_job(monkeypatch, session):
    """{
    責務: [別CoreやAPI版へJobを送らない]
    処理: [1: 不一致sessionを返す, 2: POSTがないことを確認]
    引数: [monkeypatch: transport置換, session: 不一致の応答]
    戻り値: []
    }"""
    connection, _ = _transport(monkeypatch, [_response(session)])
    with pytest.raises(api.CoreJobError, match="identity"):
        api.CoreJobClient("http://127.0.0.1:12345", TOKEN, INSTANCE).create_job(
            {"kind": "framework-smoke"}
        )
    assert connection.request.call_count == connection.close.call_count == 1


@pytest.mark.parametrize(
    "reply",
    [
        _response({"error": TOKEN}, 302),
        _response({"error": TOKEN}, 401),
        _response({}, 429),
        TimeoutError("private"),
        _response(b"not JSON", 202),
        _response(b"[]", 202),
        _response(b" " * 1_048_577, 202),
        _response(b"[" * 2000 + b"0" + b"]" * 2000, 202),
        _response((json.dumps(SNAPSHOT)[:-1] + ', "status": "failed"}').encode(), 202),
        _response({**SNAPSHOT, "kind": "different-kind"}, 202),
    ],
)
def test_http_and_transport_failures_are_bounded_sanitized_and_not_retried(monkeypatch, reply):
    """{
    責務: [redirect・認証・HTTP・JSON失敗で再送せず秘密を表示しない]
    処理: [1: session成功後にPOST失敗, 2: 接続終了と再送なしを確認]
    引数: [monkeypatch: transport置換, reply: 失敗応答または例外]
    戻り値: []
    }"""
    connection, _ = _transport(monkeypatch, [_response(SESSION), reply])
    with pytest.raises(api.CoreJobError) as failure:
        api.CoreJobClient("http://127.0.0.1:12345", TOKEN, INSTANCE).create_job(
            {"kind": "framework-smoke"}
        )
    assert TOKEN not in str(failure.value) and "private" not in str(failure.value)
    assert connection.request.call_count == connection.close.call_count == 2


@pytest.mark.parametrize(
    "change",
    [
        {"id": INSTANCE},
        {"status": "unknown"},
        {"progress": True},
        {"progress": float("nan")},
        {"progress": 2},
        {"kind": ""},
    ],
)
def test_invalid_snapshots_and_requested_id_mismatch(monkeypatch, change):
    """{
    責務: [未知状態・不正進捗・別Jobの応答を成功状態にしない]
    処理: [1: 不正snapshotを返す, 2: 取得時の契約違反を確認]
    引数: [monkeypatch: transport置換, change: 応答の改変]
    戻り値: []
    }"""
    _transport(monkeypatch, [_response(SESSION), _response({**SNAPSHOT, **change})])
    with pytest.raises(api.CoreJobError):
        api.CoreJobClient("http://127.0.0.1:12345", TOKEN, INSTANCE).get_job(JOB)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"kind": ""},
        {"kind": "x", "options": float("nan")},
        {"kind": "x", "options": "x" * 65_536},
    ],
)
def test_invalid_requests_and_ids_do_not_start_session(monkeypatch, payload):
    """{
    責務: [不正要求とパス注入を通信前に拒否する]
    処理: [1: 不正JSONやIDを渡す, 2: 接続未生成を確認]
    引数: [monkeypatch: transport置換, payload: 不正な作成要求]
    戻り値: []
    }"""
    _, factory = _transport(monkeypatch, [])
    client = api.CoreJobClient("http://127.0.0.1:12345", TOKEN, INSTANCE)
    with pytest.raises(ValueError):
        client.create_job(payload)
    for identifier in (
        "../session",
        f"urn:uuid:{JOB}",
        "{" + JOB + "}",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
    ):
        with pytest.raises(ValueError):
            client.get_job(identifier)
    factory.assert_not_called()


def test_exact_request_size_limit(monkeypatch):
    """{
    責務: [UTF-8 JSONの64KiBちょうどは送信し1byte超過は送らない]
    処理: [1: JSON全体の長さを構築, 2: 境界を送信, 3: 超過時の通信なしを確認]
    引数: [monkeypatch: transport置換]
    戻り値: []
    }"""
    payload = {"kind": "analysis.mock", "analysis": {"options": {"padding": ""}}}
    payload["analysis"]["options"]["padding"] = "x" * (65_536 - len(json.dumps(payload).encode()))
    connection, _ = _transport(
        monkeypatch, [_response(SESSION), _response({**SNAPSHOT, "kind": "analysis.mock"}, 202)]
    )
    client = api.CoreJobClient("http://127.0.0.1:12345", TOKEN, INSTANCE)
    client.create_job(payload)
    assert len(connection.request.call_args.kwargs["body"]) == 65_536
    payload["analysis"]["options"]["padding"] += "x"
    with pytest.raises(ValueError):
        client.create_job(payload)
    assert connection.request.call_count == 2


@pytest.mark.parametrize(
    "raw",
    [
        b"bad JSON",
        b"[]",
        b" " * 1_048_577,
        b"[" * 2000 + b"0" + b"]" * 2000,
        (json.dumps(SESSION)[:-1] + ', "api_version": 2}').encode(),
    ],
    ids=["invalid-json", "non-object", "oversize", "deep", "duplicate"],
)
def test_invalid_session_json_never_submits_job(monkeypatch, raw):
    """{
    責務: [sessionのJSON破損・上限・深さ・重複キーでPOSTへ進まない]
    処理: [1: sessionの異常応答, 2: 秘密なし例外と接続終了・POSTなしを確認]
    引数: [monkeypatch: transport置換, raw: 不正なsession応答]
    戻り値: []
    }"""
    connection, _ = _transport(monkeypatch, [_response(raw)])
    with pytest.raises(api.CoreJobError) as failure:
        api.CoreJobClient("http://127.0.0.1:12345", TOKEN, INSTANCE).create_job(
            {"kind": "framework-smoke"}
        )
    assert TOKEN not in str(failure.value)
    assert connection.request.call_count == connection.close.call_count == 1
