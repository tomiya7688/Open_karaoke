import hashlib
import http.client
import io
import json
from threading import Event

import pytest

from open_karaoke_analysis import alignment_backend as backend
from open_karaoke_analysis.adapters import AnalysisCancelled, AnalysisContext
from open_karaoke_analysis.contracts import ServiceError


class Response(io.BytesIO):
    def __init__(self, data=b"", status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}
        self.closed_by_caller = False

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def close(self):
        self.closed_by_caller = True
        super().close()


class Transport:
    """Fake single-hop transport: maps URL -> Response and records requested URLs."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, timeout, headers):
        self.calls.append(url)
        return self.routes[url]


def redirect(location, status=302):
    return Response(status=status, headers={"Location": location})


def start_url(name="pytorch_model.bin"):
    return f"https://huggingface.co/{backend.MODEL_ID}/resolve/{backend.REVISION}/{name}"


@pytest.fixture
def context(tmp_path):
    return AnalysisContext(tmp_path, "jobs/fixture", None, Event(), lambda *_: None)


def test_verified_snapshot_and_corruption(context, monkeypatch):
    payload = b"fixture-only-checkpoint"
    monkeypatch.setattr(backend, "WEIGHT_SHA256", hashlib.sha256(payload).hexdigest())

    def download(ctx, directory, name, limit):
        data = payload if name == "pytorch_model.bin" else b"{}"
        (directory / name).write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    monkeypatch.setattr(backend, "download_file", download)
    directory, first = backend.model_snapshot(context, True)
    assert backend.model_snapshot(context, False)[1] == first
    (directory / "vocab.json").write_bytes(b"tampered")
    with pytest.raises(ServiceError, match="corrupt"):
        backend.model_snapshot(context, False)


def test_offline_and_incomplete_snapshot(context):
    with pytest.raises(ServiceError, match="downloads disabled"):
        backend.model_snapshot(context, False)
    directory = context.root / "models" / "alignment-ja" / backend.REVISION
    directory.mkdir(parents=True)
    (directory / "snapshot.json").write_text(
        json.dumps({"revision": backend.REVISION, "sha256": {}})
    )
    with pytest.raises(ServiceError, match="corrupt"):
        backend.model_snapshot(context, True)


@pytest.mark.parametrize("bad", ["hash", "size", "cancel"])
def test_download_failure_cleans_temporary(context, bad):
    transport = Transport({start_url(): Response(b"fixture")})
    if bad == "cancel":
        context.cancelled.set()
    with pytest.raises((ServiceError, AnalysisCancelled)):
        backend.download_file(
            context, context.root, "pytorch_model.bin", 1 if bad == "size" else 100, transport
        )
    assert not list(context.root.glob(".download-*"))
    assert not (context.root / "pytorch_model.bin").exists()


def test_download_follows_https_redirect_chain_and_publishes(context, monkeypatch):
    payload = b"weights"
    monkeypatch.setattr(backend, "WEIGHT_SHA256", hashlib.sha256(payload).hexdigest())
    first = redirect("https://cdn.example.test/a")
    transport = Transport(
        {
            start_url(): first,
            "https://cdn.example.test/a": redirect("/b", 307),
            "https://cdn.example.test/b": Response(payload),
        }
    )
    digest = backend.download_file(context, context.root, "pytorch_model.bin", 100, transport)
    assert digest == hashlib.sha256(payload).hexdigest()
    assert (context.root / "pytorch_model.bin").read_bytes() == payload
    assert transport.calls[-1] == "https://cdn.example.test/b"
    assert first.closed_by_caller


def test_redirect_chain_limit():
    routes = {
        f"https://h.example.test/{i}": redirect(f"https://h.example.test/{i + 1}")
        for i in range(20)
    }
    transport = Transport(routes)
    with pytest.raises(ServiceError, match="Too many"):
        backend.open_download(
            backend.DownloadPolicy(max_redirects=2), "https://h.example.test/0", transport
        )
    assert len(transport.calls) == 3


def test_redirect_chain_at_limit_succeeds():
    routes = {
        "https://h.example.test/0": redirect("https://h.example.test/1"),
        "https://h.example.test/1": redirect("https://h.example.test/2"),
        "https://h.example.test/2": Response(b"ok"),
    }
    response = backend.open_download(
        backend.DownloadPolicy(max_redirects=2), "https://h.example.test/0", Transport(routes)
    )
    assert response.read() == b"ok"


@pytest.mark.parametrize("target", ["http://evil.test/x", "ftp://evil.test/x", "file:///etc/x"])
def test_reject_downgrade_before_request(target):
    transport = Transport({start_url(): redirect(target)})
    with pytest.raises(ServiceError) as error:
        backend.open_download(backend.DownloadPolicy(), start_url(), transport)
    assert error.value.detail.code == "model_download"
    assert transport.calls == [start_url()]  # the downgraded URL was never requested


def test_reject_non_https_start_url():
    transport = Transport({})
    with pytest.raises(ServiceError, match="Insecure"):
        backend.open_download(backend.DownloadPolicy(), "http://huggingface.co/x", transport)
    assert transport.calls == []


def test_allowed_hosts_policy():
    policy = backend.DownloadPolicy(allowed_hosts=("huggingface.co",))
    policy.check_url("https://cdn.huggingface.co/x")
    for url in ("https://evilhuggingface.co/x", "https://huggingface.co.evil.test/x"):
        with pytest.raises(ServiceError, match="not allowed"):
            policy.check_url(url)
    transport = Transport({start_url(): redirect("https://evil.test/x")})
    with pytest.raises(ServiceError, match="not allowed"):
        backend.open_download(policy, start_url(), transport)
    assert transport.calls == [start_url()]


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(404, False), (403, False), (429, True), (503, True), (302, False), (204, False)],
)
def test_bad_response_status(status, retryable):
    response = Response(status=status)  # 302 here has no Location header
    with pytest.raises(ServiceError) as error:
        backend.open_download(
            backend.DownloadPolicy(), start_url(), Transport({start_url(): response})
        )
    assert error.value.detail.code == "model_download"
    assert bool(error.value.detail.retryable) is retryable
    assert response.closed_by_caller


@pytest.mark.parametrize("header", ["101", "abc", "-1"])
def test_declared_length_over_limit_or_malformed(context, header):
    response = Response(b"x" * 10, headers={"Content-Length": header})
    transport = Transport({start_url("config.json"): response})
    with pytest.raises(ServiceError) as error:
        backend.download_file(context, context.root, "config.json", 100, transport)
    assert error.value.detail.code == "model_integrity"
    assert response.closed_by_caller
    assert not list(context.root.glob(".download-*"))


@pytest.mark.parametrize(("size", "ok"), [(99, True), (100, True), (101, False)])
def test_size_boundary(context, size, ok):
    transport = Transport({start_url("config.json"): Response(b"x" * size)})
    if ok:
        backend.download_file(context, context.root, "config.json", 100, transport)
        assert (context.root / "config.json").stat().st_size == size
    else:
        with pytest.raises(ServiceError, match="too large"):
            backend.download_file(context, context.root, "config.json", 100, transport)
        assert not (context.root / "config.json").exists()
    assert not list(context.root.glob(".download-*"))


@pytest.mark.parametrize(
    "error", [TimeoutError(), ConnectionResetError(), http.client.IncompleteRead(b"")]
)
def test_network_errors_are_retryable_download_failures(context, error):
    def transport(url, timeout, headers):
        raise error

    with pytest.raises(ServiceError) as raised:
        backend.download_file(context, context.root, "config.json", 100, transport)
    assert raised.value.detail.code == "model_download"
    assert raised.value.detail.retryable
    assert not list(context.root.glob(".download-*"))


def test_policy_timeout_is_passed_to_transport():
    seen = []

    def transport(url, timeout, headers):
        seen.append(timeout)
        return Response(b"ok")

    backend.open_download(backend.DownloadPolicy(timeout=7), start_url(), transport)
    assert seen == [7]


def test_https_transport_single_request_and_close(monkeypatch):
    events = []

    class Connection:
        def __init__(self, host, port, timeout, context):
            events.append(("init", host, port, timeout))

        def request(self, method, path, headers):
            events.append(("request", method, path))

        def getresponse(self):
            return Response(status=200)

        def close(self):
            events.append(("close",))

    monkeypatch.setattr(backend.http.client, "HTTPSConnection", Connection)
    response = backend.HttpsTransport()("https://h.example.test/a/b?x=1", 5, {})
    assert events == [("init", "h.example.test", None, 5), ("request", "GET", "/a/b?x=1")]
    backend.close_response(response)
    assert events[-1] == ("close",)
