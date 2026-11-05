"""Repeat the single Job runner and emit an anonymous aggregate JSON report."""

import argparse
import json
import statistics
import sys
from collections.abc import Callable
from pathlib import Path

from .job_runner import JobRunError, JobRunner
from .owned_core import CoreLaunchError, OwnedCore

MAX_TRIALS = 1000
OUTCOMES = (
    "completed",
    "failed",
    "cancelled",
    "timed_out",
    "transport_error",
    "launch_error",
    "cleanup_error",
    "runner_error",
)


def _stats(values: list[float]) -> dict | None:
    """{
    責務: [数値列のmin/median/mean/maxを丸めて返す]
    処理: [1: 空ならNone, 2: 6桁に丸めた4指標を返す]
    引数: [values: 有限の秒または回数]
    戻り値: [指標のdict、空ならNone]
    }"""
    if not values:
        return None
    return {
        "min": round(min(values), 6),
        "median": round(statistics.median(values), 6),
        "mean": round(statistics.fmean(values), 6),
        "max": round(max(values), 6),
    }


def run_benchmark(
    owner_factory: Callable[[int], OwnedCore],
    runner: JobRunner,
    request: dict,
    trials: int,
) -> dict:
    """{
    責務: [trialごとに新しい所有Coreで単一Jobを実行し、匿名の集計を作る]
    処理: [1: trial数を検証, 2: 各trialでCore起動とrunner実行, 3: 起動・終了・runner失敗を
      outcomeとして数える, 4: 完了trialの時間とpolling数を集計]
    引数: [owner_factory: trial番号から未使用のOwnedCoreを返す, runner: 実行方針,
      request: Core公開要求, trials: 1以上MAX_TRIALS以下]
    戻り値: [format_version/trials/outcomes/all_completed/elapsed_seconds/poll_attempts。
      入力・パス・token・Job IDは含めない。不正要求はValueError]
    }"""
    if type(trials) is not int or not 1 <= trials <= MAX_TRIALS:
        raise ValueError(f"Expected trials in [1, {MAX_TRIALS}]")
    counts = dict.fromkeys(OUTCOMES, 0)
    elapsed: list[float] = []
    polls: list[float] = []
    for index in range(trials):
        try:
            owner = owner_factory(index)
        except CoreLaunchError:
            counts["launch_error"] += 1
            continue
        try:
            result = runner.run(owner, request)
        except CoreLaunchError:
            counts["cleanup_error"] += 1
            continue
        except JobRunError:
            counts["runner_error"] += 1
            continue
        counts[result.outcome] += 1
        if result.outcome == "completed":
            elapsed.append(result.elapsed_seconds)
            if result.poll_attempts is not None:
                polls.append(result.poll_attempts)
    return {
        "format_version": 1,
        "kind": "job_api_benchmark",
        "trials": trials,
        "outcomes": counts,
        "all_completed": counts["completed"] == trials,
        "elapsed_seconds": _stats(elapsed),
        "poll_attempts": _stats(polls),
    }


def main(argv: list[str] | None = None) -> int:
    """{
    責務: [Job API benchmarkのCLI入口]
    処理: [1: 引数検証, 2: trialごとに専用データ場所でCore起動, 3: JSONを標準出力と任意fileへ,
      4: 全trial完了なら0、未完了なら1、引数エラーは2]
    引数: [argv: テスト用の引数列。省略時はsys.argv]
    戻り値: [終了コード]
    補足: [tokenは扱わない。mock-v1の接続計測であり実モデル精度ではない]
    }"""
    parser = argparse.ArgumentParser(description="Aggregate repeated Core Job API runs")
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--poll-interval", type=float, default=0.2)
    parser.add_argument("--steps", type=int, default=2)
    parser.add_argument("--delay-ms", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        runner = JobRunner(timeout=args.timeout, poll_interval=args.poll_interval)
        core, interpreter, root = (p.resolve() for p in (args.core, args.python, args.data_root))
        request = {
            "kind": "analysis.mock",
            "analysis": {
                "model_id": "mock-v1",
                "options": {"steps": args.steps, "delay_ms": args.delay_ms, "fail": False},
            },
        }

        def factory(index: int) -> OwnedCore:
            """{
            責務: [trial専用のデータ場所でCoreを起動する]
            処理: [1: data-root配下にtrial directory作成, 2: OwnedCore起動]
            引数: [index: trial番号]
            戻り値: [未使用のOwnedCore]
            }"""
            directory = root / f"trial-{index}"
            directory.mkdir(parents=True, exist_ok=True)
            return OwnedCore.start(core, directory, interpreter)

        report = run_benchmark(factory, runner, request, args.trials)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    text = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    if args.output is not None:
        try:
            args.output.write_text(text, encoding="utf-8", newline="\n")
        except OSError as error:
            parser.error(str(error))
    sys.stdout.write(text)
    return 0 if report["all_completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
