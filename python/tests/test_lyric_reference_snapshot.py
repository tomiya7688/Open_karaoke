import hashlib
import json
import subprocess
import sys

import pytest

from open_karaoke_analysis.evaluation_summary import lyric_summary
from open_karaoke_analysis.lyric_evaluator import evaluate


def _run_summary(tmp_path, pairs, *flags):
    """{
    責務: [公開CLIの集計出力を取得する]
    処理: [1: 合成の入力を書き出す, 2: CLIを別プロセスで起動する]
    引数: [tmp_path: 作業先, pairs: 参照と予測, flags: 追加引数]
    戻り値: [CLI結果と入出力パス]
    }"""
    source, detail, summary = (
        tmp_path / name for name in ("pairs.json", "detail.json", "sum.json")
    )
    source.write_text(json.dumps(pairs, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "open_karaoke_analysis.lyric_evaluator",
            str(source),
            "--output",
            str(detail),
            "--summary-output",
            str(summary),
            "--dataset-id",
            "synthetic-set",
            "--dataset-version",
            "v1",
            "--model-name",
            "fixture-model",
            "--model-version",
            "snapshot-1",
            "--evaluation-kind",
            "synthetic",
            *flags,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result, source, detail, summary


@pytest.mark.parametrize("change", ["prediction", "order", "reference", "id"])
def test_reference_identity_tracks_the_reference_set(tmp_path, change):
    """{
    責務: [モデル間比較で同一の参照集合を識別できることを確認する]
    処理: [1: 基準出力を保存する, 2: 一要素を変えて識別情報を比較する]
    引数: [tmp_path: 作業先, change: 変更する要素]
    戻り値: []
    }"""
    pairs = [
        {"id": "private-a", "reference": "秘密の空", "hypothesis": "秘密の空"},
        {"id": "private-b", "reference": "private reference", "hypothesis": "private reference"},
    ]
    result, source, _, output = _run_summary(tmp_path, pairs)
    assert result.returncode == 0, result.stderr
    baseline = json.loads(output.read_text(encoding="utf-8"))
    assert baseline["dataset"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    if change == "prediction":
        pairs[0]["hypothesis"] = "different prediction"
    elif change == "order":
        pairs.reverse()
    else:
        pairs[0][change] = "changed"
    result, _, _, output = _run_summary(tmp_path, pairs)
    assert result.returncode == 0, result.stderr
    current = json.loads(output.read_text(encoding="utf-8"))
    assert current["dataset"]["sha256"] != baseline["dataset"]["sha256"]
    same_reference = change in ("prediction", "order")
    assert (
        current["dataset"]["reference_sha256"] == baseline["dataset"]["reference_sha256"]
    ) is same_reference
    for private_value in ("private-a", "private-b", "秘密の空", "private reference"):
        assert private_value not in output.read_text(encoding="utf-8")


def test_implicit_ids_preserve_positional_meaning(tmp_path):
    """{
    責務: [ID省略時は配列位置が参照との対応を決めることを確認する]
    処理: [1: 異なる参照を評価する, 2: 順序を逆にしてhashの差を確認する]
    引数: [tmp_path: 作業先]
    戻り値: []
    }"""
    pairs = [{"reference": value, "hypothesis": value} for value in ("空", "海")]
    result, _, _, output = _run_summary(tmp_path, pairs)
    assert result.returncode == 0, result.stderr
    before = json.loads(output.read_text(encoding="utf-8"))["dataset"]["reference_sha256"]
    result, _, _, output = _run_summary(tmp_path, list(reversed(pairs)))
    assert result.returncode == 0, result.stderr
    assert before != json.loads(output.read_text(encoding="utf-8"))["dataset"]["reference_sha256"]


@pytest.mark.parametrize(
    "limit,exit_code,passed", [(None, 0, None), ("0", 1, False), ("1", 0, True)]
)
def test_summary_preserves_quality_gate_and_detailed_metrics(tmp_path, limit, exit_code, passed):
    """{
    責務: [閾値の有無と成功・失敗が集計と終了コードに保持されることを確認する]
    処理: [1: 既知の誤りを評価する, 2: 詳細値・集計値・終了コードを照合する]
    引数: [tmp_path: 作業先, limit: CER閾値, exit_code: 期待終了値, passed: 期待判定]
    戻り値: []
    }"""
    flags = [] if limit is None else ["--max-cer", limit]
    result, _, detail_path, summary_path = _run_summary(
        tmp_path, [{"reference": "青い空", "hypothesis": "青い海"}], *flags
    )
    assert result.returncode == exit_code, result.stderr
    detail, summary = (
        json.loads(path.read_text(encoding="utf-8")) for path in (detail_path, summary_path)
    )
    assert summary["metrics"]["cer"] == detail["cer"] == pytest.approx(1 / 3)
    assert summary["gate"]["passed"] is passed
    assert summary["gate"]["applied"] is (limit is not None)
    assert detail["utterances"][0]["id"] == "0"


def test_invalid_metadata_leaves_outputs_unwritten(tmp_path):
    """{
    責務: [モデル版の不正を成功結果として保存しないことを確認する]
    処理: [1: 空のモデル版を指定する, 2: エラー終了と未出力を確認する]
    引数: [tmp_path: 作業先]
    戻り値: []
    }"""
    result, _, detail, summary = _run_summary(
        tmp_path, [{"reference": "空", "hypothesis": "空"}], "--model-version", ""
    )
    assert result.returncode == 2
    assert not detail.exists() and not summary.exists()


@pytest.mark.parametrize("digest", ["A" * 64, "bad", 123, False])
def test_invalid_reference_digest_is_rejected(digest):
    """{
    責務: [比較用hashの保存契約を確認する]
    処理: [1: 不正なhashを渡す, 2: 検証エラーを確認する]
    引数: [digest: 不正なhash]
    戻り値: []
    }"""
    with pytest.raises(ValueError, match="reference_sha256"):
        lyric_summary(
            evaluate([{"reference": "空", "hypothesis": "空"}]),
            dataset_id="fixture",
            dataset_version="v1",
            dataset_sha256="a" * 64,
            model_name="fixture",
            model_version="v1",
            evaluation_kind="synthetic",
            reference_sha256=digest,
        )
