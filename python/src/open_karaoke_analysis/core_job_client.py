"""Authenticated Core Job messages for headless evaluators; no worker API access."""

import json
import math
import re
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
from urllib.parse import urlsplit
from uuid import UUID

ERROR_CODES = frozenset(
    {
        "transport",
        "invalid_response",
        "http_status",
        "unauthorized",
        "api_version",
        "session_mismatch",
        "job_not_found",
    }
)
REBOOTSTRAP_CODES = frozenset({"unauthorized", "session_mismatch"})
_STATUS_CODES = {401: "unauthorized", 404: "job_not_found", 426: "api_version"}


class CoreJobError(RuntimeError):
    """{
    責務: [Core Job通信・応答契約の失敗を秘密や本文なしで、区別可能な分類付きで示す]
    フィールド: [code: ERROR_CODESの一つ。unauthorized・session_mismatchは
      所有sessionが失われたことを示し、利用側はCoreを再bootstrapする]
    }"""

    def __init__(self, message: str, code: str = "invalid_response"):
        """{
        責務: [固定の分類と秘密を含まない説明を保持する]
        処理: [1: 分類を検証, 2: 例外を初期化]
        引数: [message: 秘密・応答本文を含まない説明, code: ERROR_CODESの分類]
        戻り値: []
        }"""
        if code not in ERROR_CODES:
            raise ValueError("Unknown Core Job error code")
        super().__init__(message)
        self.code = code

    @property
    def requires_rebootstrap(self) -> bool:
        """{
        責務: [この接続を捨てて新しいCore sessionを起動すべきか示す]
        処理: [1: 分類がsession喪失か判定]
        引数: []
        戻り値: [再bootstrapが必要ならTrue。旧Jobの再送はしない]
        }"""
        return self.code in REBOOTSTRAP_CODES


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
      _instance_id: 起動元の識別子, _timeout: socket I/O秒, _verified: session照合済み,
      _lost: session喪失を観測した分類。以後は通信せず同じ分類で失敗する]
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
        self._lost: str | None = None

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
                status = response.status
                code = _STATUS_CODES.get(status, "http_status")
                if code == "unauthorized":
                    self._lose(code)
                raise CoreJobError(f"Core Job HTTP status {status}", code)
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise CoreJobError("Core Job response exceeds 1 MiB")
            value = json.loads(raw, object_pairs_hook=_object)
            if not isinstance(value, dict):
                raise CoreJobError("Invalid Core Job response")
            return value
        except (OSError, HTTPException):
            raise CoreJobError("Core Job transport failure", "transport") from None
        except (ValueError, RecursionError):
            raise CoreJobError("Core Job JSON failure") from None
        finally:
            connection.close()

    def _lose(self, code: str) -> None:
        """{
        責務: [所有sessionの喪失を記録し、別sessionへのfallbackを禁止する]
        処理: [1: 最初の喪失分類を保持, 2: 照合済み状態を解除]
        引数: [code: unauthorizedまたはsession_mismatch]
        戻り値: []
        }"""
        self._lost = self._lost or code
        self._verified = False

    def _confirm_session(self) -> None:
        """{
        責務: [キャッシュを使わずsessionを一度照合する]
        処理: [1: 喪失済みなら通信前に失敗, 2: sessionを取得, 3: API版とinstanceを照合]
        引数: []
        戻り値: [不一致はsession_mismatchとして記録しCoreJobError]
        }"""
        if self._lost is not None:
            raise CoreJobError("Core session was lost; start a new Core session", self._lost)
        session = self._request("GET", "/session", None, 200)
        if (
            type(session.get("api_version")) is not int
            or session["api_version"] != 1
            or session.get("instance_id") != self._instance_id
        ):
            self._lose("session_mismatch")
            raise CoreJobError("Core session identity or API version mismatch", "session_mismatch")
        self._verified = True

    def verify_session(self) -> None:
        """{
        責務: [最初のJob操作前に所有sessionを照合する]
        処理: [1: 喪失済みなら失敗, 2: 未照合ならsessionを取得しAPI版とinstanceを照合して保持]
        引数: []
        戻り値: [不一致・喪失済みはCoreJobError。別sessionへのfallbackなし]
        }"""
        if self._lost is not None or not self._verified:
            self._confirm_session()

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
        処理: [1: JSONと64KiB上限を検証, 2: キャッシュせずsession再照合, 3: POSTして状態を検証]
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
        # Job creation has side effects: re-confirm identity every time so a restarted
        # Core that accepts a reused token never receives the Job.
        self._confirm_session()
        return self._snapshot(self._request("POST", "/jobs", payload, 202), kind=request["kind"])

    def get_job(self, identifier: str) -> JobSnapshot:
        """{
        責務: [所有session内のJob状態を一度取得する]
        処理: [1: ID検証, 2: session照合, 3: GETして対象Jobとの一致を確認]
        引数: [identifier: 正規表記UUID]
        戻り値: [JobSnapshot。HTTP失敗や不正応答はCoreJobError。404はsessionを再照合し、
          同じinstanceならjob_not_found、再起動後の別instanceならsession_mismatch]
        }"""
        _uuid(identifier)
        self.verify_session()
        try:
            value = self._request("GET", f"/jobs/{identifier}", None, 200)
        except CoreJobError as error:
            if error.code == "job_not_found":
                # Job history is per Core instance: tell an unknown ID from a restart.
                self._confirm_session()
            raise
        return self._snapshot(value, identifier=identifier)
