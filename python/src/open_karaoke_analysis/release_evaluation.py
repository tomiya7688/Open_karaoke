"""Aggregate release-evaluation results into a verdict; unverified is never a pass."""

import argparse
import json
import math
import re
from pathlib import Path

_STATUSES = ("pass", "fail", "unverified")
_KEY = re.compile(r"[a-z_]+/[a-z0-9_]+")
_RESULT_KEYS = {"area", "metric", "value", "threshold", "direction", "status", "evidence_ref"}
_REPORT_KEYS = {
    "format_version",
    "app_sha",
    "dataset_manifest_hash",
    "model_snapshot",
    "results",
    "blocking",
}


def _number(value: object) -> bool:
    """
    {
      責務: [_number: 有限の数値(boolを除く)か判定する]
      処理: [1: bool除外, 2: int/floatで有限か確認する]
      引数: [value: 判定対象]
      戻り値: [有限の数値ならTrue]
    }
    """
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _within(value: float, threshold: float, direction: str) -> bool:
    """
    {
      責務: [_within: 値が閾値に対して合格側か判定する]
      処理: [1: maxなら value<=threshold, 2: minなら value>=threshold]
      引数: [value: 計測値, threshold: 閾値, direction: max または min]
      戻り値: [合格側ならTrue]
    }
    """
    return value <= threshold if direction == "max" else value >= threshold


def validate_report(report: dict) -> None:
    """
    {
      責務: [validate_report: report.jsonが契約を満たすか検証する(未知キー・未較正pass・矛盾を拒否)]
      処理: [
        1: 最上位キーとメタデータ(app_sha・dataset hash・model_snapshot)を検証する,
        2: 各結果の型・status・方向を検証する,
        3: passには値とevidence_refを要求し、閾値があれば方向に沿うか確認する,
        4: blocking項目のpassは閾値が非nullであることを要求する,
        5: blockingが空でなく、形式が正しく、重複しないことを確認する
      ]
      引数: [report: report.jsonの内容]
      戻り値: []
      エラー: [ValueError: 契約違反]
    }
    """
    if not isinstance(report, dict) or set(report) != _REPORT_KEYS:
        raise ValueError("Report must have exactly the contract keys")
    if type(report["format_version"]) is not int or report["format_version"] != 1:
        raise ValueError("Unsupported report format_version")
    sha, manifest = report["app_sha"], report["dataset_manifest_hash"]
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError("app_sha must be a lowercase 40-hex commit")
    if not isinstance(manifest, str) or re.fullmatch(r"[0-9a-f]{64}", manifest) is None:
        raise ValueError("dataset_manifest_hash must be a lowercase SHA-256 digest")
    snapshot = report["model_snapshot"]
    if not isinstance(snapshot, str) or not snapshot.strip() or len(snapshot) > 256:
        raise ValueError("model_snapshot must be a nonempty string")
    blocking, results = report["blocking"], report["results"]
    if not isinstance(blocking, list) or not blocking:
        raise ValueError("blocking must be a nonempty list")
    if any(not isinstance(k, str) or _KEY.fullmatch(k) is None for k in blocking):
        raise ValueError("blocking entries must be 'area/metric'")
    if len(set(blocking)) != len(blocking):
        raise ValueError("blocking entries must be unique")
    if not isinstance(results, list):
        raise ValueError("results must be a list")
    seen: set[str] = set()
    for item in results:
        if not isinstance(item, dict) or set(item) != _RESULT_KEYS:
            raise ValueError("Result must have exactly the contract keys")
        key = f"{item['area']}/{item['metric']}"
        if _KEY.fullmatch(key) is None or key in seen:
            raise ValueError(f"Invalid or duplicate result key: {key}")
        seen.add(key)
        value, threshold = item["value"], item["threshold"]
        for label, number in (("value", value), ("threshold", threshold)):
            if number is not None and not _number(number):
                raise ValueError(f"{key}: {label} must be a finite number or null")
        if item["direction"] not in ("max", "min"):
            raise ValueError(f"{key}: direction must be max or min")
        if item["status"] not in _STATUSES:
            raise ValueError(f"{key}: invalid status")
        evidence = item["evidence_ref"]
        if evidence is not None and (not isinstance(evidence, str) or len(evidence) > 512):
            raise ValueError(f"{key}: evidence_ref must be a string or null")
        if item["status"] != "pass" and value is not None and threshold is not None:
            # 数値が揃っているなら、statusは測定値と矛盾させない(失敗を未検証に見せない)。
            met = _within(value, threshold, item["direction"])
            if (item["status"] == "fail") == met:
                raise ValueError(f"{key}: status contradicts value and threshold")
        if item["status"] == "pass":
            if value is None or not evidence or not evidence.strip():
                raise ValueError(f"{key}: pass requires a value and evidence_ref")
            if threshold is not None and not _within(value, threshold, item["direction"]):
                raise ValueError(f"{key}: pass contradicts the threshold")
            if key in blocking and threshold is None:
                raise ValueError(f"{key}: a blocking pass requires a calibrated threshold")


def verdict(report: dict) -> dict:
    """
    {
      責務: [verdict: blocking項目の状態からリリース可否を判定する]
      処理: [
        1: 契約を検証する,
        2: blocking各項目の状態を引く(結果が無ければunverified),
        3: 全てpassのときだけreleasableをTrueにする
      ]
      引数: [report: report.jsonの内容]
      戻り値: [{releasable, blocking_status{key: status}, failing[], unverified[]}]
      エラー: [ValueError: 契約違反]
    }
    """
    validate_report(report)
    by_key = {f"{r['area']}/{r['metric']}": r["status"] for r in report["results"]}
    states = {key: by_key.get(key, "unverified") for key in report["blocking"]}
    failing = sorted(k for k, s in states.items() if s == "fail")
    unverified = sorted(k for k, s in states.items() if s == "unverified")
    return {
        "releasable": not failing and not unverified,
        "blocking_status": states,
        "failing": failing,
        "unverified": unverified,
    }


def lyric_result(summary: dict, *, evidence_ref: str, threshold: float | None) -> dict:
    """
    {
      責務: [lyric_result: evaluation_summary.lyric_summaryの要約を、lyrics/cerの結果行へ変換する]
      処理: [
        1: 要約が実歌唱(real_singing)か確認する(合成は店頭品質の証拠にしない),
        2: CERがnullまたは閾値未設定ならunverifiedにする,
        3: それ以外は閾値と比較してpass/failにする
      ]
      引数: [summary: 要約, evidence_ref: 証拠参照, threshold: 校正済みCER閾値かNone]
      戻り値: [results[]の1要素]
      エラー: [ValueError: 実歌唱でない、または要約でない]
    }
    """
    if not isinstance(summary, dict) or type(summary.get("format_version")) is not int:
        raise ValueError("Expected a lyric evaluation summary")
    if summary["format_version"] != 1 or not isinstance(summary.get("metrics"), dict):
        raise ValueError("Expected a lyric evaluation summary")
    if summary.get("evaluation_kind") != "real_singing":
        raise ValueError("Only real_singing summaries count as release evidence")
    cer = summary["metrics"].get("cer")
    if cer is not None and not _number(cer):
        raise ValueError("cer must be a finite number or null")
    if threshold is not None and not _number(threshold):
        raise ValueError("threshold must be a finite number or null")
    if cer is None or threshold is None:
        status = "unverified"
    else:
        status = "pass" if _within(cer, threshold, "max") else "fail"
    return {
        "area": "lyrics",
        "metric": "cer",
        "value": cer,
        "threshold": threshold,
        "direction": "max",
        "status": status,
        "evidence_ref": evidence_ref,
    }


def main() -> int:
    """
    {
      責務: [main: report.jsonを読んでリリース可否を表示し終了コードで返す]
      処理: [1: JSONを読む, 2: verdictを計算する, 3: 結果をJSONで出力する]
      引数: []
      戻り値: [0: リリース可, 1: 不可(failまたはunverified)。入力不正は2で終了]
    }
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        result = verdict(json.loads(args.report.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["releasable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
