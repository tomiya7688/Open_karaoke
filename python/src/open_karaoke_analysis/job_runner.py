"""One owned Core, one Job submission, and a bounded observation lifetime."""

import queue
import threading
import time
from dataclasses import dataclass

from .core_job_client import CoreJobClient, CoreJobError
from .owned_core import OwnedCore


class JobRunError(RuntimeError):
    """{
    責務: [予期しない観測失敗やobserverの停止未確認を本文なしで示す]
    フィールド: []
    }"""


@dataclass(frozen=True)
class JobRunResult:
    """{
    責務: [単一Jobの終端判定と観測時間を秘密・入力・Job IDなしで保持する]
    フィールド: [outcome: completed/failed/cancelled/timed_out/transport_error,
      elapsed_seconds: 作成要求から判定までの秒。起動/cleanupは除外,
      poll_attempts: 状態取得の試行数。期限超過時は未確定のためNone]
    }"""

    outcome: str
    elapsed_seconds: float
    poll_attempts: int | None


def _observe(
    client: CoreJobClient,
    request: dict,
    deadline: float,
    interval: float,
    stopped: threading.Event,
    result: queue.Queue,
) -> None:
    """{
    責務: [一度だけJobを作成し、同じJobを終端または期限まで観測する]
    処理: [1: 作成, 2: 終端と期限を確認, 3: 有限間隔で状態取得, 4: 一度だけ結果通知]
    引数: [client: 認証済み接続, request: Core作成要求, deadline: 単調時刻の期限,
      interval: polling秒, stopped: 所有者の停止通知, result: 容量1の結果queue]
    戻り値: [None。例外本文やrequestをqueueへ返さない]
    }"""
    polls = 0
    try:
        if stopped.is_set() or time.monotonic() >= deadline:
            return
        job = client.create_job(request)
        identifier, kind = job.id, job.kind
        while not stopped.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result.put(("timed_out", None))
                return
            if job.status in ("completed", "failed", "cancelled"):
                result.put((job.status, polls))
                return
            if job.status not in ("queued", "running"):
                raise CoreJobError("Invalid Job observation state")
            if stopped.wait(min(interval, remaining)):
                return
            if time.monotonic() >= deadline:
                continue
            polls += 1
            job = client.get_job(identifier)
            if job.id != identifier or job.kind != kind:
                raise CoreJobError("Mismatched Job observation")
    except CoreJobError:
        result.put(("transport_error", polls))
    except ValueError:
        result.put(("invalid_request", None))
    except Exception:
        result.put(("execution_error", None))


class JobRunner:
    """{
    責務: [所有済みCoreを一回のJob実行に消費し、期限と終了処理を管理する]
    フィールド: [_timeout: Job全体秒, _poll_interval: 状態取得間隔秒]
    }"""

    def __init__(self, *, timeout: float = 30, poll_interval: float = 0.2):
        """{
        責務: [実行方針だけを検証し保持する。ここではCoreに接続しない]
        処理: [1: 有限の予算と間隔を検証, 2: 保持]
        引数: [timeout: 0超3600秒以下, poll_interval: 0.01以上10秒以下]
        戻り値: [不正設定はValueError]
        }"""
        if type(timeout) not in (int, float) or not 0 < timeout <= 3600:
            raise ValueError("Expected Job timeout in (0, 3600]")
        if type(poll_interval) not in (int, float) or not 0.01 <= poll_interval <= 10:
            raise ValueError("Expected polling interval in [0.01, 10]")
        self._timeout, self._poll_interval = timeout, poll_interval

    def run(self, owner: OwnedCore, request: dict) -> JobRunResult:
        """{
        責務: [作成・polling・通信待機を含む期限で一回実行し、常に所有Coreを閉じる]
        処理: [1: 接続を受け取る, 2: observer起動, 3: 期限内の結果を判定,
          4: Coreを閉じobserverの停止を確認する]
        引数: [owner: この実行で消費する所有済みCore, request: Core公開要求]
        戻り値: [JobRunResult。不正要求はValueError、予期しない失敗はJobRunError。
          owner終了失敗はCoreLaunchErrorを優先して伝える]
        補足: [自動再送/再起動なし。起動とcleanupには別予算。requestは実行中変更しない]
        }"""
        stopped, result = threading.Event(), queue.Queue(maxsize=1)
        thread = None
        try:
            client = owner.client
            started = time.monotonic()
            deadline = started + self._timeout
            thread = threading.Thread(
                target=_observe,
                args=(client, request, deadline, self._poll_interval, stopped, result),
                daemon=True,
            )
            try:
                thread.start()
            except RuntimeError:
                raise JobRunError("Could not start Job observer") from None
            try:
                outcome, polls = result.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                outcome, polls = "timed_out", None
            elapsed = time.monotonic() - started
            if outcome == "invalid_request":
                raise ValueError("Invalid Core Job request") from None
            if outcome == "execution_error":
                raise JobRunError("Unexpected Core Job observation failure") from None
            if elapsed >= self._timeout:
                outcome, polls = "timed_out", None
            return JobRunResult(outcome, elapsed, polls)
        finally:
            stopped.set()
            close_failed = True
            try:
                owner.close()
                close_failed = False
            finally:
                if thread is not None and thread.ident is not None:
                    thread.join(timeout=1)
                    if thread.is_alive() and not close_failed:
                        raise JobRunError("Job observer did not stop after Core shutdown")
