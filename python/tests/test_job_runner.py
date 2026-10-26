import os
import queue
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from open_karaoke_analysis import job_runner as api
from open_karaoke_analysis.core_job_client import CoreJobError, JobSnapshot
from open_karaoke_analysis.owned_core import CoreLaunchError, OwnedCore

JOB = "22222222-2222-4222-8222-222222222222"
SECRET = "private fixture data"


def _snapshot(status):
    """{
    責務: [テスト用の検証済みJob状態を用意する]
    処理: [1: 固定IDとkindに状態を付ける]
    引数: [status: Coreの状態]
    戻り値: [JobSnapshot]
    }"""
    return JobSnapshot(JOB, "analysis.mock", status, 1 if status == "completed" else 0)


def _owner(status="queued"):
    """{
    責務: [外部プロセスに触れないownerとclientを用意する]
    処理: [1: 作成と取得の応答を設定]
    引数: [status: 作成応答の状態]
    戻り値: [owner mock]
    }"""
    owner = Mock()
    owner.client.create_job.return_value = _snapshot(status)
    owner.client.get_job.return_value = _snapshot("completed")
    return owner


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_polling_terminal_result_and_single_submission(status):
    """{
    責務: [一度だけ作成し同じJobの終端を区別してownerを消費する]
    処理: [1: queued→running→終端, 2: message・回数・計測・最小結果を確認]
    引数: [status: 終端状態]
    戻り値: []
    }"""
    owner = _owner()
    owner.client.get_job.side_effect = [_snapshot("running"), _snapshot(status)]
    request = {"kind": "analysis.mock", "analysis": {"input": SECRET}}
    result = api.JobRunner(timeout=2, poll_interval=0.01).run(owner, request)
    assert result.outcome == status and result.poll_attempts == 2
    assert 0.01 <= result.elapsed_seconds < 2
    assert set(vars(result)) == {"outcome", "elapsed_seconds", "poll_attempts"}
    assert SECRET not in repr(result) and JOB not in repr(result)
    owner.client.create_job.assert_called_once_with(request)
    assert owner.client.get_job.call_count == 2
    owner.client.get_job.assert_called_with(JOB)
    owner.close.assert_called_once_with()


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_immediate_terminal_never_polls(status):
    """{
    責務: [作成応答が終端なら追加取得しない]
    処理: [1: 終端を返す, 2: pollingなしと結果を確認]
    引数: [status: 終端状態]
    戻り値: []
    }"""
    owner = _owner(status)
    result = api.JobRunner().run(owner, {"kind": "analysis.mock"})
    assert result.outcome == status and result.poll_attempts == 0
    owner.client.get_job.assert_not_called()
    owner.close.assert_called_once()


@pytest.mark.parametrize("phase", ["create_job", "get_job"])
def test_transport_failure_is_not_retried_or_exposed(phase):
    """{
    責務: [不確定な通信失敗をJob失敗と区別し再送しない]
    処理: [1: 指定messageを失敗させる, 2: 匿名の失敗結果と一度の送信を確認]
    引数: [phase: 失敗するmessage]
    戻り値: []
    }"""
    owner = _owner()
    getattr(owner.client, phase).side_effect = CoreJobError(SECRET)
    result = api.JobRunner(poll_interval=0.01).run(owner, {"kind": "analysis.mock"})
    assert result.outcome == "transport_error"
    assert result.poll_attempts == (0 if phase == "create_job" else 1)
    assert SECRET not in repr(result)
    owner.client.create_job.assert_called_once()
    owner.close.assert_called_once()


@pytest.mark.parametrize("phase", ["create_job", "get_job"])
def test_deadline_includes_blocked_io_and_closes_owner(phase):
    """{
    責務: [socket待機中にも全体期限で打ち切り、遅い応答を成功にしない]
    処理: [1: messageをブロック, 2: 期限でownerを閉じ解放, 3: 再送なしを確認]
    引数: [phase: ブロックするmessage]
    戻り値: []
    }"""
    owner = _owner()
    release = threading.Event()
    getattr(owner.client, phase).side_effect = lambda *args: (
        release.wait(2) and _snapshot("completed")
    )
    owner.close.side_effect = release.set
    before = time.monotonic()
    result = api.JobRunner(timeout=0.1, poll_interval=0.01).run(owner, {"kind": "analysis.mock"})
    assert result.outcome == "timed_out" and result.poll_attempts is None
    assert result.elapsed_seconds >= 0.1 and time.monotonic() - before < 1
    owner.client.create_job.assert_called_once()
    assert owner.client.get_job.call_count == (0 if phase == "create_job" else 1)
    owner.close.assert_called_once()


def test_early_wakeup_of_clipped_wait_does_not_poll(monkeypatch):
    """{
    責務: [OSの期限前wakeでpolling間隔を満たさずGETする退行を防ぐ]
    処理: [1: 待機が早く戻るEventを用意, 2: 時計を期限まで進める, 3: 取得なしを確認]
    引数: [monkeypatch: observerの時計境界置換]
    戻り値: []
    }"""
    owner = _owner()
    stopped = Mock()
    stopped.is_set.return_value = False
    stopped.wait.return_value = False
    result = queue.Queue(maxsize=1)
    monkeypatch.setattr(
        api, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 0.01, 0.02]))
    )
    api._observe(owner.client, {"kind": "analysis.mock"}, 0.02, 10, stopped, result)
    assert result.get_nowait() == ("timed_out", None)
    assert stopped.wait.call_count == 2
    assert stopped.wait.call_args_list[0].args == (0.02,)
    assert stopped.wait.call_args_list[1].args == (0.01,)
    owner.client.get_job.assert_not_called()


def test_poll_wait_is_clipped_to_deadline():
    """{
    責務: [長いpolling間隔でも期限までの残りだけ待ち、期限後にGETしない]
    処理: [1: intervalを期限より長くする, 2: timeoutと取得なしを確認]
    引数: []
    戻り値: []
    }"""
    owner = _owner()
    result = api.JobRunner(timeout=0.02, poll_interval=10).run(owner, {"kind": "analysis.mock"})
    assert result.outcome == "timed_out" and result.poll_attempts is None
    owner.client.get_job.assert_not_called()
    owner.close.assert_called_once()


@pytest.mark.parametrize(
    "error,expected", [(ValueError(SECRET), ValueError), (LookupError(SECRET), api.JobRunError)]
)
def test_observer_errors_are_sanitized_and_cleanup_runs(error, expected):
    """{
    責務: [不正要求と予期しない実行失敗を明示し本文を漏らさない]
    処理: [1: 作成処理を失敗, 2: 例外種別・本文抑止・owner終了を確認]
    引数: [error: observer内の例外, expected: 呼び出し元の例外型]
    戻り値: []
    }"""
    owner = _owner()
    owner.client.create_job.side_effect = error
    with pytest.raises(expected) as failure:
        api.JobRunner().run(owner, {})
    assert SECRET not in str(failure.value)
    owner.close.assert_called_once()


@pytest.mark.parametrize(
    "outcome,expected",
    [("invalid_request", ValueError), ("execution_error", api.JobRunError)],
)
def test_received_observer_error_survives_foreground_deadline_boundary(
    monkeypatch, outcome, expected
):
    """{
    責務: [受け取った入力拒否や観測例外を判定時刻の期限跨ぎで隠さない]
    処理: [1: observer結果を即通知, 2: foreground時計だけ期限を跨ぐ, 3: 例外と終了を確認]
    引数: [monkeypatch: 時計とobserver境界置換, outcome: 通知種別, expected: 例外型]
    戻り値: []
    }"""
    owner = _owner()

    def notify(*args):
        """{
        責務: [時計に依存せずobserver例外を通知する]
        処理: [1: 結果queueへ通知]
        引数: [args: 最後の引数が結果queue]
        戻り値: []
        }"""
        args[-1].put((outcome, None))

    monkeypatch.setattr(api, "_observe", notify)
    monkeypatch.setattr(api, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 1])))
    with pytest.raises(expected):
        api.JobRunner(timeout=0.1).run(owner, {})
    owner.close.assert_called_once()


def test_cleanup_failure_overrides_a_successful_job():
    """{
    責務: [所有Core終了を確認できないのに正常完了としない]
    処理: [1: Job完了後のcloseを失敗, 2: cleanupエラーを確認]
    引数: []
    戻り値: []
    }"""
    owner = _owner("completed")
    owner.close.side_effect = CoreLaunchError("cleanup unconfirmed")
    with pytest.raises(CoreLaunchError, match="cleanup"):
        api.JobRunner().run(owner, {"kind": "analysis.mock"})
    owner.close.assert_called_once()


def test_observer_that_ignores_core_shutdown_is_not_reported_as_clean():
    """{
    責務: [Core終了後にもobserverが停止しない場合を明示失敗にする]
    処理: [1: 応答をブロック, 2: 期限とjoin期限を超過, 3: テスト側で必ず解放]
    引数: []
    戻り値: []
    }"""
    owner = _owner()
    release = threading.Event()
    finished = threading.Event()

    def blocked(request):
        """{
        責務: [停止しない外部依存を有限のテスト通知で模擬する]
        処理: [1: 解放まで待つ, 2: 終了を通知して応答]
        引数: [request: 未使用の要求]
        戻り値: [JobSnapshot]
        }"""
        release.wait(3)
        finished.set()
        return _snapshot("completed")

    owner.client.create_job.side_effect = blocked
    try:
        with pytest.raises(api.JobRunError, match="did not stop"):
            api.JobRunner(timeout=0.25).run(owner, {"kind": "analysis.mock"})
    finally:
        release.set()
        assert finished.wait(1)
    owner.close.assert_called_once()


def test_thread_start_failure_still_closes_owner(monkeypatch):
    """{
    責務: [observerを開始できなくてもownerを閉じ、秘密なしで失敗を返す]
    処理: [1: startを失敗, 2: JobRunErrorと作成なしを確認]
    引数: [monkeypatch: thread境界置換]
    戻り値: []
    }"""
    owner = _owner()
    monkeypatch.setattr(api.threading.Thread, "start", Mock(side_effect=RuntimeError(SECRET)))
    with pytest.raises(api.JobRunError, match="start Job observer") as failure:
        api.JobRunner().run(owner, {})
    assert SECRET not in str(failure.value)
    owner.client.create_job.assert_not_called()
    owner.close.assert_called_once()


@pytest.mark.parametrize(
    "changed",
    [
        JobSnapshot("33333333-3333-4333-8333-333333333333", "analysis.mock", "completed", 1),
        JobSnapshot(JOB, "different", "completed", 1),
    ],
)
def test_changed_job_identity_is_a_transport_contract_failure(changed):
    """{
    責務: [観測中にIDやkindが変わった応答を同じJobの完了にしない]
    処理: [1: 別Job応答を返す, 2: transport_errorを確認]
    引数: [changed: IDまたはkindの異なる状態]
    戻り値: []
    }"""
    owner = _owner()
    owner.client.get_job.return_value = changed
    result = api.JobRunner(poll_interval=0.01).run(owner, {"kind": "analysis.mock"})
    assert result.outcome == "transport_error" and result.poll_attempts == 1
    owner.client.create_job.assert_called_once()
    owner.close.assert_called_once()


@pytest.mark.parametrize(
    "options",
    [
        {"timeout": True},
        {"timeout": 0},
        {"timeout": float("nan")},
        {"timeout": float("inf")},
        {"timeout": 10**1000},
        {"poll_interval": True},
        {"poll_interval": 0},
        {"poll_interval": 11},
        {"poll_interval": float("nan")},
    ],
)
def test_invalid_policy_is_rejected_without_io(options):
    """{
    責務: [有限でない実行方針やbusy loopを起動前に拒否する]
    処理: [1: 不正設定でrunnerを作る, 2: ValueErrorを確認]
    引数: [options: 不正な予算または間隔]
    戻り値: []
    }"""
    with pytest.raises(ValueError):
        api.JobRunner(**options)


@pytest.mark.parametrize("status", ["completed", "failed", "timed_out"])
@pytest.mark.skipif(not os.environ.get("OPEN_KARAOKE_TEST_CORE"), reason="real Core opt-in")
def test_real_core_runner_closes_worker_runtime(tmp_path, status, monkeypatch):
    """{
    責務: [実Core/workerで一回のJobを観測し、終了後のruntime cleanupを確認する]
    処理: [1: 専用Coreを起動, 2: mock解析を実行, 3: 匿名結果とowner失効を確認]
    引数: [tmp_path: 専用データ場所, status: 完了・失敗・期限超過,
      monkeypatch: 実clientの観測結果を記録する]
    戻り値: []
    補足: [mock-v1の接続検証であり、実モデル精度やbenchmark集計の証拠ではない]
    }"""
    runner = api.JobRunner(timeout=5 if status == "timed_out" else 20, poll_interval=0.01)
    owner = OwnedCore.start(
        os.environ["OPEN_KARAOKE_TEST_CORE"],
        tmp_path,
        os.environ.get("OPEN_KARAOKE_TEST_PYTHON", sys.executable),
    )
    request = {
        "kind": "analysis.mock",
        "analysis": {
            "model_id": "mock-v1",
            "options": {
                "steps": 100 if status == "timed_out" else 2,
                "delay_ms": 100 if status == "timed_out" else 10,
                "fail": status == "failed",
            },
        },
    }
    runtime = tmp_path / "artifacts/.runtime"
    running_runtime = []
    client = owner.client
    get_job = client.get_job

    def observe(identifier):
        """{
        責務: [実HTTP取得を保持し、稼働中workerのruntimeを観測する]
        処理: [1: 実clientに取得依頼, 2: running時のruntime存在を記録]
        引数: [identifier: 作成済みJob ID]
        戻り値: [元のJobSnapshot]
        }"""
        snapshot = get_job(identifier)
        if status == "timed_out" and snapshot.status == "running":
            try:
                running_runtime.append(any(runtime.iterdir()))
            except FileNotFoundError:
                running_runtime.append(False)
        return snapshot

    monkeypatch.setattr(client, "get_job", observe)
    result = runner.run(owner, request)
    assert result.outcome == status
    if status == "timed_out":
        assert result.poll_attempts is None
        assert any(running_runtime)
        assert 5 <= result.elapsed_seconds < 10
    else:
        assert result.poll_attempts >= 0
    with pytest.raises(CoreLaunchError):
        _ = owner.client
    assert not runtime.exists() or not any(runtime.iterdir())
