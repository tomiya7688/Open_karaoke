"""Bounded, aggregate-only Job observations; no process or filesystem operations."""

import json
import math

from .job_runner import JobRunResult

OUTCOMES = ("completed", "failed", "cancelled", "timed_out", "transport_error")
MAX_TRIALS = 1000
MAX_JSON_BYTES = 8192
MAX_POLLS = 2**53 - 1


def _number(value):
    """{
    責務: [有限で非負の経過秒を検証する]
    処理: [1: boolや非数値を拒否, 2: 有限範囲を検査]
    引数: [value: 非信頼の数値]
    戻り値: [秒として正規化したfloat。不正なら本文なしのValueError]
    }"""
    try:
        if type(value) not in (int, float) or value < 0 or not math.isfinite(value):
            raise ValueError()
    except (ValueError, OverflowError):
        raise ValueError("Invalid benchmark duration") from None
    return float(value)


def _count(value, maximum):
    """{
    責務: [JSONで厳密に表現できる非負整数を検証する]
    処理: [1: 整数型と上下限を照合]
    引数: [value: 件数, maximum: 上限]
    戻り値: [検証済み件数。不正ならValueError]
    }"""
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("Invalid benchmark count")
    return value


def _keys(value, expected):
    """{
    責務: [固定JSON契約の欠落・追加フィールドを拒否する]
    処理: [1: object型とキー集合を照合]
    引数: [value: JSON object, expected: 必須キー集合]
    戻り値: [None。不正ならValueError]
    }"""
    if type(value) is not dict or set(value) != set(expected):
        raise ValueError("Invalid benchmark fields")


def _statistics(values):
    """{
    責務: [観測秒の全体またはcompleted集合を集計する]
    処理: [1: 有限の合計を算出, 2: 平均と範囲を設定]
    引数: [values: 検証済み経過秒の有限list]
    戻り値: [件数・合計・平均・最小・最大。空集合の平均と範囲はNone]
    }"""
    try:
        total = math.fsum(values)
    except OverflowError:
        raise ValueError("Benchmark duration total overflow") from None
    return {
        "count": len(values),
        "total": total,
        "mean": total / len(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def _validate_statistics(value, count):
    """{
    責務: [保存済み時間集計の件数・平均・範囲の整合性を検査する]
    処理: [1: 固定キーと件数, 2: 空集合, 3: 有限性と平均・範囲を照合]
    引数: [value: 時間集計object, count: 期待する件数]
    戻り値: [None。不整合ならValueError]
    }"""
    _keys(value, ("count", "total", "mean", "min", "max"))
    if _count(value["count"], MAX_TRIALS) != count:
        raise ValueError("Inconsistent benchmark duration count")
    total = _number(value["total"])
    if not count:
        if total != 0 or any(value[key] is not None for key in ("mean", "min", "max")):
            raise ValueError("Invalid empty benchmark statistics")
        return
    mean, low, high = (_number(value[key]) for key in ("mean", "min", "max"))
    if (
        not (_leq(low, mean) and _leq(mean, high) and _leq(high, total))
        or not math.isclose(mean, total / count, rel_tol=1e-12, abs_tol=0)
        or (count == 1 and not low == mean == high == total)
        or not _leq(low + (high - low) / count, mean)
        or not _leq(mean, high - (high - low) / count)
    ):
        raise ValueError("Inconsistent benchmark duration statistics")


def _leq(left, right):
    """{
    責務: [合計と平均の浮動小数点丸めを許容して上下関係を照合する]
    処理: [1: 順序または相対誤差1e-12以内の一致を確認]
    引数: [left: 左辺, right: 右辺。検証済みの有限数値]
    戻り値: [bool。0との比較は絶対誤差を加えない]
    }"""
    return left <= right or math.isclose(left, right, rel_tol=1e-12, abs_tol=0)


def _validate(value):
    """{
    責務: [版付きJob集計の固定schemaと相互整合性を検証する]
    処理: [1: 版と識別子, 2: 件数とcompletion率, 3: 時間集合とpoll件数を照合]
    引数: [value: 非信頼のJSON object]
    戻り値: [None。未知フィールドや不整合はValueError]
    }"""
    _keys(
        value,
        (
            "format_version",
            "evaluator",
            "trials",
            "outcomes",
            "completion_rate",
            "elapsed_seconds",
            "poll_attempts",
        ),
    )
    if type(value["format_version"]) is not int or value["format_version"] != 1:
        raise ValueError("Unsupported benchmark format version")
    if value["evaluator"] != "core-job-benchmark":
        raise ValueError("Invalid benchmark evaluator")
    trials = _count(value["trials"], MAX_TRIALS)
    counts = value["outcomes"]
    _keys(counts, OUTCOMES)
    if not trials or sum(_count(counts[key], MAX_TRIALS) for key in OUTCOMES) != trials:
        raise ValueError("Inconsistent benchmark trial counts")
    if _number(value["completion_rate"]) != counts["completed"] / trials:
        raise ValueError("Inconsistent benchmark completion rate")
    elapsed = value["elapsed_seconds"]
    _keys(elapsed, ("all_trials", "completed_trials"))
    all_stats, completed = elapsed["all_trials"], elapsed["completed_trials"]
    _validate_statistics(all_stats, trials)
    _validate_statistics(completed, counts["completed"])
    if (
        not _leq(completed["total"], all_stats["total"])
        or (
            counts["completed"]
            and not (
                _leq(all_stats["min"], completed["min"])
                and _leq(completed["max"], all_stats["max"])
            )
        )
        or (counts["completed"] == trials and completed != all_stats)
    ):
        raise ValueError("Inconsistent completed duration subset")
    polls = value["poll_attempts"]
    _keys(polls, ("known_trials", "unknown_trials", "total_known"))
    if (
        _count(polls["unknown_trials"], MAX_TRIALS) != counts["timed_out"]
        or _count(polls["known_trials"], MAX_TRIALS) != trials - counts["timed_out"]
        or (_count(polls["total_known"], MAX_POLLS) and not polls["known_trials"])
    ):
        raise ValueError("Inconsistent benchmark polling counts")


def _object(pairs):
    """{
    責務: [JSONの重複キーを上書きせず拒否する]
    処理: [1: 一意なキーだけobjectへ登録]
    引数: [pairs: JSON decoderのキーと値]
    戻り値: [object。重複はValueError]
    }"""
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate benchmark field")
        value[key] = item
    return value


class JobBenchmarkSummary:
    """{
    責務: [匿名のJob観測だけから有界な集計を所有し、固定JSONを公開する]
    フィールド: [_json: 検証済みの版付きJSON。個別trialや元の入力は保持しない]
    }"""

    def __init__(self, results: list[JobRunResult] | tuple[JobRunResult, ...]):
        """{
        責務: [1以上1000以下の観測結果を副作用なしで集計する]
        処理: [1: 有限のlist/tupleを検証, 2: 各結果を検証, 3: 匿名集計を固定]
        引数: [results: runnerが返した結果。実行中に変更しない]
        戻り値: [集計object。不正値や合計overflowはValueError]
        }"""
        if type(results) not in (list, tuple) or not 1 <= len(results) <= MAX_TRIALS:
            raise ValueError("Expected 1 to 1000 Job observations")
        counts = dict.fromkeys(OUTCOMES, 0)
        elapsed, completed = [], []
        total_polls = 0
        for result in results:
            if (
                type(result) is not JobRunResult
                or type(result.outcome) is not str
                or result.outcome not in OUTCOMES
            ):
                raise ValueError("Invalid Job observation")
            duration = _number(result.elapsed_seconds)
            if result.outcome == "timed_out":
                if result.poll_attempts is not None:
                    raise ValueError("Timed out polling count must be unknown")
            else:
                total_polls += _count(result.poll_attempts, MAX_POLLS)
                if total_polls > MAX_POLLS:
                    raise ValueError("Benchmark poll total overflow")
            counts[result.outcome] += 1
            elapsed.append(duration)
            if result.outcome == "completed":
                completed.append(duration)
        value = {
            "format_version": 1,
            "evaluator": "core-job-benchmark",
            "trials": len(results),
            "outcomes": counts,
            "completion_rate": counts["completed"] / len(results),
            "elapsed_seconds": {
                "all_trials": _statistics(elapsed),
                "completed_trials": _statistics(completed),
            },
            "poll_attempts": {
                "known_trials": len(results) - counts["timed_out"],
                "unknown_trials": counts["timed_out"],
                "total_known": total_polls,
            },
        }
        _validate(value)
        self._json = json.dumps(value, allow_nan=False, sort_keys=True) + "\n"

    def to_dict(self) -> dict:
        """{
        責務: [集計を呼び出し側が変更可能な独立objectとして公開する]
        処理: [1: 固定JSONから新しいobjectを作る]
        引数: []
        戻り値: [固定schemaのdict。変更しても所有値に影響しない]
        }"""
        return json.loads(self._json)

    def to_json(self) -> str:
        """{
        責務: [秘密や個別trialを含まない版付きJSONを返す]
        処理: [1: 固定JSON文字列を返す]
        引数: []
        戻り値: [UTF-8で保存できるJSON。末尾LF付き。ファイルI/Oはしない]
        }"""
        return self._json

    @classmethod
    def from_json(cls, text: str):
        """{
        責務: [保存済み集計を8KiB以内の固定契約として読み込む]
        処理: [1: UTF-8サイズ検査, 2: 重複なしJSONをdecode, 3: schemaを検証して固定]
        引数: [text: 外部JSON文字列]
        戻り値: [検証済み集計。不正な本文を含めずValueError]
        }"""
        try:
            if (
                type(text) is not str
                or len(text) > MAX_JSON_BYTES
                or len(text.encode("utf-8")) > MAX_JSON_BYTES
            ):
                raise ValueError()
            value = json.loads(text, object_pairs_hook=_object)
            _validate(value)
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise ValueError("Invalid Job benchmark JSON") from None
        value["completion_rate"] = float(value["completion_rate"])
        for stats in value["elapsed_seconds"].values():
            for field in ("total", "mean", "min", "max"):
                if stats[field] is not None:
                    stats[field] = float(stats[field])
        summary = cls.__new__(cls)
        summary._json = json.dumps(value, allow_nan=False, sort_keys=True) + "\n"
        return summary
