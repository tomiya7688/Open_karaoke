import hashlib
import json
import os
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
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
        timeout=30,
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
    summary_text = output.read_text(encoding="utf-8")
    for private_value in ("private-a", "private-b", "秘密の空", "private reference"):
        escaped_value = json.dumps(private_value, ensure_ascii=True)[1:-1]
        # 生の本文・ASCIIエスケープ後の本文・標準出力/標準エラーのどこにも参照を出さない。
        assert private_value not in summary_text
        assert escaped_value not in summary_text
        assert private_value not in result.stdout + result.stderr
        assert escaped_value not in result.stdout + result.stderr


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


@pytest.mark.parametrize("explicit_id", [False, True])
def test_reference_digest_has_a_stable_serialization(tmp_path, explicit_id):
    """{
    責務: [参照集合hashの永続的な表現を固定する]
    処理: [1: 既知の参照を評価する, 2: 固定hashとID省略時の対応を確認する]
    引数: [tmp_path: 作業先, explicit_id: 位置と同じIDを明示するか]
    戻り値: []
    }"""
    pair = {"reference": "青い空", "hypothesis": "空"}
    if explicit_id:
        pair["id"] = "0"
    result, _, _, output = _run_summary(tmp_path, [pair])
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["dataset"]["reference_sha256"] == (
        "ee551690f14599aff15247e686df90e0d50652d3e384714621def0d0470ff469"
    )


def test_reference_digest_fixes_multi_item_order_and_id_pairing(tmp_path):
    """{
    責務: [複数参照のhashがid昇順・ASCII固定の直列化であることを固定する]
    処理: [
      1: idの昇順と逆順に並べた2件を評価する,
      2: 仕様の式で手計算したhashと固定値に一致することを確認する,
      3: idと参照の組み換えでhashが変わることを確認する
    ]
    引数: [tmp_path: 作業先]
    戻り値: []
    }"""
    pairs = [
        {"id": "b", "reference": "海", "hypothesis": "海"},
        {"id": "a", "reference": "青い空", "hypothesis": "空"},
    ]
    result, _, _, output = _run_summary(tmp_path, pairs)
    assert result.returncode == 0, result.stderr
    digest = json.loads(output.read_text(encoding="utf-8"))["dataset"]["reference_sha256"]
    body = json.dumps([("a", "青い空"), ("b", "海")], ensure_ascii=True, separators=(",", ":"))
    assert (
        digest
        == hashlib.sha256(b"open-karaoke-lyric-reference-v1\0" + body.encode("ascii")).hexdigest()
    )
    assert digest == "e73f74f6da382aa6c9e44414931aac0472b6054de1c5590519382f2e5a0c9933"
    result, _, _, output = _run_summary(tmp_path, list(reversed(pairs)))
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["dataset"]["reference_sha256"] == digest
    swapped = [{**pairs[0], "id": "a"}, {**pairs[1], "id": "b"}]
    result, _, _, output = _run_summary(tmp_path, swapped)
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["dataset"]["reference_sha256"] != digest


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
