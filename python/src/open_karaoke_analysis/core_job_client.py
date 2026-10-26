"""Authenticated Core Job messages for headless evaluators; no worker API access."""

import json
import math
import re
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
from urllib.parse import urlsplit
from uuid import UUID


class CoreJobError(RuntimeError):
    """{
    責務: [Core Job通信・応答契約の失敗を秘密や本文なしで示す]
    フィールド: []
    }"""


@dataclass(frozen=True)
class JobSnapshot:
    """{
    責務: [Evaluatorが利用する最小のJob状態を不変値として保持する]
    フィールド: [id: Job UUID, kind: 処理種別, status: 状態, progress: 0から1の進捗]
    }"""

    id: str
    kind: str
    status: str
    progress: float


def _uuid(value: str) -> str:
    """{
    責務: [パスや応答の識別子を正規表記のUUIDに限定する]
    処理: [1: UUIDを解釈する, 2: 正規文字列と比較する]
    引数: [value: 識別子]
    戻り値: [検証済み識別子。不正ならValueError]
    }"""
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Expected a canonical UUID")
    return value


def _object(pairs: list[tuple]) -> dict:
    """{
    責務: [応答JSONの重複キーを拒否する]
    処理: [1: 出現済みキーを調べる, 2: objectを構築]
    引数: [pairs: decoderからのキーと値]
    戻り値: [重複のないdict。不正はValueError]
    }"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


class CoreJobClient:
    """{
    責務: [所有済みCoreのsessionを照合しJob作成・状態取得のmessageを送る]
    フィールド: [_host・_port: 数値loopback, _token: メモリ内能力,
      _instance_id: 起動元の識別子, _timeout: socket I/O秒, _verified: session照合済み]
    }"""

    def __init__(self, base_url: str, token: str, instance_id: str, *, timeout: float = 5):
        """{
        責務: [起動元の接続情報を検証して保持する。ここでは通信しない]
        処理: [1: URL・token・instance・timeoutを検証する, 2: 接続設定を保持する]
        引数: [base_url: readyから得たHTTP loopback URL, token: メモリ内token,
          instance_id: readyのUUID, timeout: 0超30以下のsocket I/O秒]
        戻り値: []
        }"""
        try:
            url = urlsplit(base_url)
            host, port = url.hostname, url.port
            expected = f"[{host}]:{port}" if host == "::1" else f"{host}:{port}"
            valid = (
                url.scheme == "http"
                and host in ("127.0.0.1", "::1")
                and port is not None
                and 1 <= port <= 65535
                and url.netloc == expected
                and url.path in ("", "/")
                and not url.query
                and not url.fragment
                and not any(c.isspace() or ord(c) < 32 for c in base_url)
            )
        except (TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise ValueError("Expected a numeric loopback HTTP origin with explicit port")
        if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None:
            raise ValueError("Expected a session token")
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise ValueError("Expected socket timeout in (0, 30]")
        self._host, self._port, self._token = host, port, token
        self._instance_id, self._timeout, self._verified = _uuid(instance_id), timeout, False

    def _request(self, method: str, path: str, payload: bytes | None, expected: int) -> dict:
        """{
        責務: [proxy・redirect・再送なしでCoreへ1回だけHTTP要求する]
        処理: [1: 認証付き送信, 2: statusと1MiB以内のJSONを検証, 3: 接続を閉じる]
        引数: [method: メソッド, path: 内部生成パス, payload: JSON本文, expected: HTTP状態]
        戻り値: [JSON object。失敗はCoreJobErrorで本文・tokenを返さない]
        }"""
        connection = HTTPConnection(self._host, self._port, timeout=self._timeout)
        try:
            connection.request(
                method,
                path,
                body=payload,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "X-Open-Karaoke-Api-Version": "1",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            if response.status != expected:
                raise CoreJobError(f"Core Job HTTP status {response.status}")
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise CoreJobError("Core Job response exceeds 1 MiB")
            value = json.loads(raw, object_pairs_hook=_object)
            if not isinstance(value, dict):
                raise CoreJobError("Invalid Core Job response")
            return value
        except (OSError, HTTPException, ValueError, RecursionError):
            raise CoreJobError("Core Job transport or JSON failure") from None
        finally:
            connection.close()

    def _session(self) -> None:
        """{
        責務: [最初のJob操作前に所有sessionを照合する]
        処理: [1: 未照合ならsessionを取得, 2: API版とinstanceを照合して保持]
        引数: []
        戻り値: [不一致はCoreJobError。別sessionへのfallbackなし]
        }"""
        if not self._verified:
            session = self._request("GET", "/session", None, 200)
            if (
                type(session.get("api_version")) is not int
                or session["api_version"] != 1
                or session.get("instance_id") != self._instance_id
            ):
                raise CoreJobError("Core session identity or API version mismatch")
            self._verified = True

    def _snapshot(
        self, value: dict, *, identifier: str | None = None, kind: str | None = None
    ) -> JobSnapshot:
        """{
        責務: [Job応答から識別可能な最小状態を検証して取り出す]
        処理: [1: ID・種別・状態・進捗を検証, 2: 期待するJobとの一致を照合]
        引数: [value: 応答, identifier: 取得対象ID, kind: 作成要求種別]
        戻り値: [JobSnapshot。不正な応答はCoreJobError]
        }"""
        try:
            job_id, job_kind, status, progress = (
                value["id"],
                value["kind"],
                value["status"],
                value["progress"],
            )
            _uuid(job_id)
            if (
                not isinstance(job_kind, str)
                or not 1 <= len(job_kind) <= 128
                or status not in ("queued", "running", "completed", "failed", "cancelled")
            ):
                raise ValueError()
            if (
                type(progress) not in (int, float)
                or not math.isfinite(progress)
                or not 0 <= progress <= 1
            ):
                raise ValueError()
            if (identifier is not None and identifier != job_id) or (
                kind is not None and kind != job_kind
            ):
                raise ValueError()
        except (KeyError, TypeError, ValueError, OverflowError):
            raise CoreJobError("Invalid or mismatched Core Job snapshot") from None
        return JobSnapshot(job_id, job_kind, status, float(progress))

    def create_job(self, request: dict) -> JobSnapshot:
        """{
        責務: [CoreへJobを一度だけ作成要求する]
        処理: [1: JSONと64KiB上限を検証, 2: session照合, 3: POSTして状態を検証]
        引数: [request: Core公開Job要求。詳細なkind/options検証はCoreが所有する]
        戻り値: [JobSnapshot。送信後失敗でも自動再送しない]
        }"""
        if (
            not isinstance(request, dict)
            or not isinstance(request.get("kind"), str)
            or not 1 <= len(request["kind"]) <= 128
        ):
            raise ValueError("Expected a Core Job request with kind")
        try:
            payload = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError, UnicodeError):
            raise ValueError("Invalid Core Job request JSON") from None
        if len(payload) > 65_536:
            raise ValueError("Core Job request exceeds 64 KiB")
        self._session()
        return self._snapshot(self._request("POST", "/jobs", payload, 202), kind=request["kind"])

    def get_job(self, identifier: str) -> JobSnapshot:
        """{
        責務: [所有session内のJob状態を一度取得する]
        処理: [1: ID検証, 2: session照合, 3: GETして対象Jobとの一致を確認]
        引数: [identifier: 正規表記UUID]
        戻り値: [JobSnapshot。HTTP失敗や不正応答はCoreJobError]
        }"""
        _uuid(identifier)
        self._session()
        return self._snapshot(
            self._request("GET", f"/jobs/{identifier}", None, 200), identifier=identifier
        )
