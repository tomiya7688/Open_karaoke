import json
import os
import sys
from unittest.mock import Mock

import pytest

from open_karaoke_analysis import job_benchmark as api
from open_karaoke_analysis.job_runner import JobRunError, JobRunResult
from open_karaoke_analysis.owned_core import CoreLaunchError


def _runner(*items):
    """{
    責務: [順番に結果または例外を返す偽runnerを作る]
    引数: [items: JobRunResultまたは例外]
    戻り値: [Mock]
    }"""
    runner = Mock()
    runner.run.side_effect = list(items)
    return runner


def test_aggregates_outcomes_and_completed_statistics():
    runner = _runner(
        JobRunResult("completed", 1.0, 2),
        JobRunResult("completed", 3.0, 4),
        JobRunResult("failed", 9.0, 1),
        JobRunResult("timed_out", 5.0, None),
        JobRunResult("transport_error", 0.5, 0),
        JobRunResult("cancelled", 0.7, 1),
    )
    report = api.run_benchmark(lambda i: object(), runner, {"kind": "x"}, 6)
    assert report["trials"] == 6
    assert report["outcomes"]["completed"] == 2
    assert report["outcomes"]["failed"] == 1
    assert report["outcomes"]["timed_out"] == 1
    assert report["outcomes"]["transport_error"] == 1
    assert report["outcomes"]["cancelled"] == 1
    assert report["all_completed"] is False
    assert report["elapsed_seconds"] == {"min": 1.0, "median": 2.0, "mean": 2.0, "max": 3.0}
    assert report["poll_attempts"]["max"] == 4


def test_launch_cleanup_and_runner_failures_are_counted_not_raised():
    def factory(index):
        if index == 0:
            raise CoreLaunchError("start")
        return object()

    runner = _runner(CoreLaunchError("close"), JobRunError("x"))
    report = api.run_benchmark(factory, runner, {}, 3)
    assert report["outcomes"]["launch_error"] == 1
    assert report["outcomes"]["cleanup_error"] == 1
    assert report["outcomes"]["runner_error"] == 1
    assert report["elapsed_seconds"] is None and report["poll_attempts"] is None
    assert runner.run.call_count == 2


def test_report_is_anonymous_and_json_safe():
    runner = _runner(JobRunResult("completed", 0.25, 1))
    report = api.run_benchmark(lambda i: object(), runner, {"secret": "s3cret"}, 1)
    text = json.dumps(report, allow_nan=False)
    assert "s3cret" not in text and report["all_completed"] is True
    assert set(report) == {
        "format_version",
        "kind",
        "trials",
        "outcomes",
        "all_completed",
        "elapsed_seconds",
        "poll_attempts",
    }


@pytest.mark.parametrize("trials", [0, -1, api.MAX_TRIALS + 1, True, 1.5, "2"])
def test_invalid_trial_count_is_rejected_before_starting_any_core(trials):
    factory = Mock()
    with pytest.raises(ValueError):
        api.run_benchmark(factory, _runner(), {}, trials)
    factory.assert_not_called()


def test_invalid_request_error_propagates_after_runner_consumed_owner():
    runner = _runner(ValueError("bad"))
    with pytest.raises(ValueError):
        api.run_benchmark(lambda i: object(), runner, {}, 1)


@pytest.mark.parametrize(
    "extra",
    [["--trials", "0"], ["--timeout", "0"], ["--poll-interval", "99"]],
)
def test_cli_rejects_bad_options(tmp_path, extra):
    base = ["--core", sys.executable, "--python", sys.executable, "--data-root", str(tmp_path)]
    with pytest.raises(SystemExit) as raised:
        api.main(base + extra)
    assert raised.value.code == 2


def test_cli_writes_output_and_maps_exit_code(tmp_path, monkeypatch):
    def fake(factory, runner, request, trials):
        assert request["analysis"]["model_id"] == "mock-v1"
        return {"all_completed": ok, "trials": trials}

    out = tmp_path / "report.json"
    argv = ["--core", sys.executable, "--python", sys.executable, "--data-root", str(tmp_path)]
    argv += ["--trials", "2", "--output", str(out)]
    monkeypatch.setattr(api, "run_benchmark", fake)
    ok = True
    assert api.main(argv) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["trials"] == 2
    ok = False
    assert api.main(argv) == 1


@pytest.mark.skipif(not os.environ.get("OPEN_KARAOKE_TEST_CORE"), reason="real Core opt-in")
def test_real_core_cli_reports_all_completed(tmp_path):
    out = tmp_path / "report.json"
    code = api.main(
        [
            "--core",
            os.environ["OPEN_KARAOKE_TEST_CORE"],
            "--python",
            os.environ.get("OPEN_KARAOKE_TEST_PYTHON", sys.executable),
            "--data-root",
            str(tmp_path / "data"),
            "--trials",
            "2",
            "--timeout",
            "20",
            "--poll-interval",
            "0.05",
            "--output",
            str(out),
        ]
    )
    report = json.loads(out.read_text(encoding="utf-8"))
    assert code == 0 and report["outcomes"]["completed"] == 2
