import json
from dataclasses import replace

import pytest

from open_karaoke_analysis.job_runner import JobRunResult
from open_karaoke_analysis.job_summary import JobBenchmarkSummary


def _mixed():
    """{
    責務: [全outcomeを含む既知の匿名結果を用意する]
    処理: [1: 件数・秒・poll数が既知の6件を返す]
    引数: []
    戻り値: [JobRunResultのlist]
    }"""
    return [
        JobRunResult("completed", 2, 1),
        JobRunResult("completed", 4, 3),
        JobRunResult("failed", 1, 2),
        JobRunResult("cancelled", 3, 0),
        JobRunResult("timed_out", 5, None),
        JobRunResult("transport_error", 7, 4),
    ]


def test_known_mixed_outcomes_are_separate_and_roundtrip():
    """{
    責務: [5状態・分母・時間集合・未知poll件数を固定契約として検証する]
    処理: [1: 既知の6観測を集計, 2: 各集計値を照合, 3: JSON往復を確認]
    引数: []
    戻り値: []
    }"""
    summary = JobBenchmarkSummary(_mixed())
    value = summary.to_dict()
    assert set(value) == {
        "format_version",
        "evaluator",
        "trials",
        "outcomes",
        "completion_rate",
        "elapsed_seconds",
        "poll_attempts",
    }
    assert value["format_version"] == 1 and value["evaluator"] == "core-job-benchmark"
    assert value["trials"] == 6 and value["completion_rate"] == 2 / 6
    assert value["outcomes"] == {
        "completed": 2,
        "failed": 1,
        "cancelled": 1,
        "timed_out": 1,
        "transport_error": 1,
    }
    assert value["elapsed_seconds"] == {
        "all_trials": {"count": 6, "total": 22, "mean": 22 / 6, "min": 1, "max": 7},
        "completed_trials": {"count": 2, "total": 6, "mean": 3, "min": 2, "max": 4},
    }
    assert value["poll_attempts"] == {"known_trials": 5, "unknown_trials": 1, "total_known": 10}
    assert JobBenchmarkSummary.from_json(summary.to_json()).to_json() == summary.to_json()


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "timed_out", "transport_error"])
def test_no_completion_is_not_zero_latency_or_unknown_poll_zero(outcome):
    """{
    責務: [成功なしの平均はnull、timeoutのpollは未知のままとする]
    処理: [1: 完了以外の1件を集計, 2: 空集合と既知/未知件数を照合]
    引数: [outcome: 完了以外の結果]
    戻り値: []
    }"""
    value = JobBenchmarkSummary(
        [JobRunResult(outcome, 2, None if outcome == "timed_out" else 0)]
    ).to_dict()
    assert value["completion_rate"] == 0
    assert value["elapsed_seconds"]["completed_trials"] == {
        "count": 0,
        "total": 0,
        "mean": None,
        "min": None,
        "max": None,
    }
    assert value["poll_attempts"]["unknown_trials"] == (1 if outcome == "timed_out" else 0)
    assert value["poll_attempts"]["known_trials"] == (0 if outcome == "timed_out" else 1)
    assert JobBenchmarkSummary.from_json(json.dumps(value)).to_dict() == value


@pytest.mark.parametrize("duration", [0, 0.1, 1e-200, 1e200, 2**53 + 1])
def test_repeated_duration_rounding_and_finite_extremes(duration):
    """{
    責務: [同じ時間の合計・平均の丸めで有効結果を拒否しない]
    処理: [1: 同一時間を3件集計, 2: 保存形式検証を往復]
    引数: [duration: 有効な有限経過秒]
    戻り値: []
    }"""
    summary = JobBenchmarkSummary([JobRunResult("completed", duration, 0)] * 3)
    assert JobBenchmarkSummary.from_json(summary.to_json()).to_dict() == summary.to_dict()


@pytest.mark.parametrize("results", [[], [JobRunResult("completed", 1, 0)] * 1001, {}, None])
def test_invalid_collection_rejected(results):
    """{
    責務: [空・過大・非配列を拒否する]
    処理: [1: 不正な集合で生成を試みる]
    引数: [results: 不正な集合]
    戻り値: []
    }"""
    with pytest.raises(ValueError):
        JobBenchmarkSummary(results)


def test_maximum_tuple_and_generator_not_consumed():
    """{
    責務: [1000件境界を受理し、長さ不明の入力を消費しない]
    処理: [1: 上限tupleを集計, 2: generatorを拒否し先頭が残ることを確認]
    引数: []
    戻り値: []
    }"""
    row = JobRunResult("completed", 1, 0)
    assert JobBenchmarkSummary((row,) * 1000).to_dict()["trials"] == 1000
    generator = iter([row])
    with pytest.raises(ValueError):
        JobBenchmarkSummary(generator)
    assert next(generator) is row


@pytest.mark.parametrize(
    "changes",
    [
        {"outcome": "queued"},
        {"outcome": []},
        {"outcome": "private token"},
        {"elapsed_seconds": True},
        {"elapsed_seconds": -1},
        {"elapsed_seconds": None},
        {"elapsed_seconds": float("nan")},
        {"elapsed_seconds": float("inf")},
        {"elapsed_seconds": 10**1000},
        {"poll_attempts": None},
        {"poll_attempts": True},
        {"poll_attempts": -1},
        {"poll_attempts": 1.5},
        {"poll_attempts": 2**53},
        {"outcome": "timed_out", "poll_attempts": 0},
    ],
)
def test_invalid_observation_does_not_enter_summary(changes):
    """{
    責務: [runner結果の未検証fieldを集計境界で拒否する]
    処理: [1: 有効結果のfieldを不正値に置換, 2: 本文なしの拒否を確認]
    引数: [changes: 置換値]
    戻り値: []
    }"""
    with pytest.raises(ValueError) as failure:
        JobBenchmarkSummary([replace(JobRunResult("completed", 1, 0), **changes)])
    assert "private token" not in str(failure.value)


@pytest.mark.parametrize("row", [{"outcome": "completed"}, object(), None])
def test_non_result_rejected(row):
    """{
    責務: [構造の似た外部objectをrunner結果として扱わない]
    処理: [1: 型が異なる入力を拒否]
    引数: [row: JobRunResult以外]
    戻り値: []
    }"""
    with pytest.raises(ValueError):
        JobBenchmarkSummary([row])


def test_aggregate_overflow_rejected():
    """{
    責務: [有限の個別値でも集計overflowを保存しない]
    処理: [1: 秒合計とpoll合計の上限超過をそれぞれ生成]
    引数: []
    戻り値: []
    }"""
    for duration, polls in [(1e308, 0), (1, 2**53 - 1)]:
        with pytest.raises(ValueError):
            JobBenchmarkSummary([JobRunResult("completed", duration, polls)] * 2)


def test_poll_total_exact_limit_and_overflow():
    """{
    責務: [poll合計の正確な整数上限と超過時のエラーを検証する]
    処理: [1: 2件の合計上限を受理, 2: 一つ超過した合計を拒否]
    引数: []
    戻り値: []
    }"""
    rows = [JobRunResult("completed", 1, 2**53 - 2), JobRunResult("failed", 1, 1)]
    assert JobBenchmarkSummary(rows).to_dict()["poll_attempts"]["total_known"] == 2**53 - 1
    rows.append(JobRunResult("failed", 1, 1))
    with pytest.raises(ValueError, match="^Benchmark poll total overflow$"):
        JobBenchmarkSummary(rows)


@pytest.mark.parametrize("value", [True, 10**400])
def test_json_duration_bool_and_oversized_integer_rejected(value):
    """{
    責務: [外部JSONのbool・巨大整数が正規化前に拒否されることを確認する]
    処理: [1: 有効な集計の合計秒を置換, 2: 固定のValueErrorを確認]
    引数: [value: 不正な数値]
    戻り値: []
    }"""
    report = JobBenchmarkSummary(_mixed()).to_dict()
    report["elapsed_seconds"]["all_trials"]["total"] = value
    with pytest.raises(ValueError, match="^Invalid Job benchmark JSON$"):
        JobBenchmarkSummary.from_json(json.dumps(report))


def test_summary_owns_no_source_data_or_mutable_exports():
    """{
    責務: [元結果・入力配列・公開dictと所有集計を切り離す]
    処理: [1: 元結果に未知fieldを追加, 2: 集計後に配列と公開値を変更, 3: 匿名性を確認]
    引数: []
    戻り値: []
    }"""
    rows = _mixed()
    rows[0].__dict__["private_input"] = "secret-token-and-path"
    summary = JobBenchmarkSummary(rows)
    canonical = summary.to_json()
    rows.clear()
    exported = summary.to_dict()
    exported["outcomes"]["completed"] = 100
    exported["elapsed_seconds"]["all_trials"]["total"] = 0
    assert summary.to_json() == canonical and "secret-token-and-path" not in canonical
    assert summary.to_dict()["outcomes"]["completed"] == 2
    assert summary.to_dict()["elapsed_seconds"]["all_trials"]["total"] == 22
    assert set(vars(summary)) == {"_json"}


def test_balanced_count_tampering_is_rejected():
    """{
    責務: [総件数を維持した不整合も個別の契約で拒否する]
    処理: [1: poll既知/未知を入れ替え, 2: 完了/失敗件数を入れ替えて検査]
    引数: []
    戻り値: []
    }"""
    report = JobBenchmarkSummary(_mixed()).to_dict()
    report["poll_attempts"].update(known_trials=6, unknown_trials=0)
    with pytest.raises(ValueError):
        JobBenchmarkSummary.from_json(json.dumps(report))
    report = JobBenchmarkSummary(_mixed()).to_dict()
    report["outcomes"].update(completed=1, failed=2)
    report["completion_rate"] = 1 / 6
    with pytest.raises(ValueError):
        JobBenchmarkSummary.from_json(json.dumps(report))


def test_each_schema_key_is_required():
    """{
    責務: [固定schemaの各必須keyが省略できないことを確認する]
    処理: [1: 各objectのkeyを列挙, 2: 一つずつ削除して読込を検査]
    引数: []
    戻り値: []
    }"""
    paths = [
        (),
        ("outcomes",),
        ("poll_attempts",),
        ("elapsed_seconds",),
        ("elapsed_seconds", "all_trials"),
        ("elapsed_seconds", "completed_trials"),
    ]
    for path in paths:
        original = JobBenchmarkSummary(_mixed()).to_dict()
        target = original
        for part in path:
            target = target[part]
        for field in tuple(target):
            report = JobBenchmarkSummary(_mixed()).to_dict()
            target = report
            for part in path:
                target = target[part]
            del target[field]
            with pytest.raises(ValueError):
                JobBenchmarkSummary.from_json(json.dumps(report))


def test_integer_json_seconds_are_normalized_on_load():
    """{
    責務: [同じ集計の整数表記とfloat表記を同じJSONへ正規化する]
    処理: [1: 完了1件のJSON秒を整数表記にする, 2: 読込後の出力を照合]
    引数: []
    戻り値: []
    }"""
    summary = JobBenchmarkSummary([JobRunResult("completed", 2, 0)])
    value = summary.to_dict()
    for stats in value["elapsed_seconds"].values():
        for key in ("total", "mean", "min", "max"):
            stats[key] = int(stats[key])
    value["completion_rate"] = 1
    assert JobBenchmarkSummary.from_json(json.dumps(value)).to_json() == summary.to_json()


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "transport_error"])
def test_non_timeout_unknown_poll_count_is_rejected(outcome):
    """{
    責務: [timeout以外のpoll未知値を集計時に拒否する]
    処理: [1: 未知pollを持つ結果を生成, 2: 集計境界で拒否]
    引数: [outcome: timeout以外の失敗状態]
    戻り値: []
    }"""
    with pytest.raises(ValueError):
        JobBenchmarkSummary([JobRunResult(outcome, 1, None)])


@pytest.mark.parametrize("field,value", [("total", 1), ("mean", 0), ("min", 0), ("max", 0)])
def test_empty_completion_statistics_cannot_be_fabricated(field, value):
    """{
    責務: [完了0件に観測していない時間集計を付けない]
    処理: [1: timeoutだけの集計を改変, 2: 読込で拒否]
    引数: [field: 破壊する項目, value: 非nullまたは非0の値]
    戻り値: []
    }"""
    report = JobBenchmarkSummary([JobRunResult("timed_out", 1, None)]).to_dict()
    report["elapsed_seconds"]["completed_trials"][field] = value
    with pytest.raises(ValueError):
        JobBenchmarkSummary.from_json(json.dumps(report))


def test_all_unknown_polls_cannot_claim_a_known_total():
    """{
    責務: [timeoutしかない場合のpoll数を捏造しない]
    処理: [1: 既知合計を追加, 2: 拒否を確認]
    引数: []
    戻り値: []
    }"""
    report = JobBenchmarkSummary([JobRunResult("timed_out", 1, None)]).to_dict()
    report["poll_attempts"]["total_known"] = 1
    with pytest.raises(ValueError):
        JobBenchmarkSummary.from_json(json.dumps(report))


def test_json_exact_byte_limit_and_nested_duplicate():
    """{
    責務: [8KiB境界を受理し、既知object内の重複も拒否する]
    処理: [1: 有効JSONに空白を追加, 2: byte境界を検証, 3: nested重複を拒否]
    引数: []
    戻り値: []
    }"""
    canonical = JobBenchmarkSummary(_mixed()).to_json()
    text = canonical + " " * (8192 - len(canonical.encode("utf-8")))
    assert JobBenchmarkSummary.from_json(text).to_json() == canonical
    with pytest.raises(ValueError):
        JobBenchmarkSummary.from_json(text + " ")
    with pytest.raises(ValueError):
        JobBenchmarkSummary.from_json(
            canonical.replace('"known_trials": 5', '"known_trials": 5, "known_trials": 5')
        )


def test_arithmetically_impossible_extrema_are_rejected():
    """{
    責務: [平均が範囲内でも実現できない2件の最小最大を拒否する]
    処理: [1: 2件でmean2・min1・max2と改変, 2: 合計4との不整合を拒否]
    引数: []
    戻り値: []
    }"""
    report = JobBenchmarkSummary([JobRunResult("failed", 2, 0)] * 2).to_dict()
    report["elapsed_seconds"]["all_trials"]["min"] = 1
    with pytest.raises(ValueError):
        JobBenchmarkSummary.from_json(json.dumps(report))


@pytest.mark.parametrize(
    "path,value",
    [
        (("format_version",), True),
        (("format_version",), 2),
        (("evaluator",), "private token"),
        (("trials",), 0),
        (("trials",), 1001),
        (("trials",), 6.0),
        (("outcomes", "completed"), 3),
        (("outcomes", "failed"), True),
        (("outcomes", "failed"), -1),
        (("completion_rate",), 0),
        (("completion_rate",), float("nan")),
        (("completion_rate",), True),
        (("poll_attempts", "known_trials"), 6),
        (("poll_attempts", "unknown_trials"), 0),
        (("poll_attempts", "total_known"), -1),
        (("poll_attempts", "total_known"), 2**53),
        (("elapsed_seconds", "all_trials", "count"), 5),
        (("elapsed_seconds", "all_trials", "mean"), 0),
        (("elapsed_seconds", "all_trials", "max"), 30),
        (("elapsed_seconds", "all_trials", "total"), 23),
        (("elapsed_seconds", "completed_trials", "min"), 0),
        (("elapsed_seconds", "completed_trials", "max"), 8),
        (("elapsed_seconds", "completed_trials", "total"), 25),
    ],
)
def test_serialized_schema_rejects_inconsistent_values(path, value):
    """{
    責務: [保存済み集計の型・範囲・相互整合性を検証する]
    処理: [1: 既知集計の1fieldを破壊, 2: JSON境界で拒否を確認]
    引数: [path: field経路, value: 不正な置換値]
    戻り値: []
    }"""
    report = JobBenchmarkSummary(_mixed()).to_dict()
    target = report
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError, match="Invalid Job benchmark JSON"):
        JobBenchmarkSummary.from_json(json.dumps(report))


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("outcomes",),
        ("poll_attempts",),
        ("elapsed_seconds",),
        ("elapsed_seconds", "all_trials"),
        ("elapsed_seconds", "completed_trials"),
    ],
)
@pytest.mark.parametrize("extra", [False, True])
def test_missing_or_extra_fields_at_each_schema_object(path, extra):
    """{
    責務: [未知fieldを匿名結果へ取り込まず、必要fieldの欠落も拒否する]
    処理: [1: 各objectのキーを追加または削除, 2: 拒否を確認]
    引数: [path: object経路, extra: 追加ならTrue]
    戻り値: []
    }"""
    report = JobBenchmarkSummary(_mixed()).to_dict()
    target = report
    for key in path:
        target = target[key]
    if extra:
        target["private_input"] = "secret-token-and-path"
    else:
        del target[next(iter(target))]
    with pytest.raises(ValueError) as failure:
        JobBenchmarkSummary.from_json(json.dumps(report))
    assert "secret-token-and-path" not in str(failure.value)


@pytest.mark.parametrize(
    "text",
    [
        None,
        {},
        "[]",
        "null",
        "{",
        '{"a":1,"a":2}',
        " " * 8193,
        "[" * 2000 + "]" * 2000,
        "\ud800",
        '"' + "あ" * 3000 + '"',
    ],
)
def test_invalid_json_is_bounded_and_sanitized(text):
    """{
    責務: [不正・重複・過大・深いJSONや不正Unicodeを安全に拒否する]
    処理: [1: 非信頼文字列をdecode境界に渡す, 2: 固定の例外を確認]
    引数: [text: 不正JSON]
    戻り値: []
    }"""
    with pytest.raises(ValueError, match="^Invalid Job benchmark JSON$"):
        JobBenchmarkSummary.from_json(text)
