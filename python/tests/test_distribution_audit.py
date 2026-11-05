import json

import pytest

from open_karaoke_analysis.distribution_audit import (
    audit_problems,
    distribution_result,
    main,
    sha256_file,
)
from open_karaoke_analysis.release_evaluation import validate_report


def make_bundle(tmp_path, conf="--disable-gpl --enable-shared"):
    bundle = tmp_path / "ffmpeg"
    bundle.mkdir()
    (bundle / "ffmpeg.exe").write_bytes(b"fake-ffmpeg-binary")
    (bundle / "buildconf.txt").write_text(conf, encoding="utf-8")
    sha = sha256_file(bundle / "ffmpeg.exe")
    (bundle / "NOTICE.txt").write_text(f"FFmpeg 7.1 {sha} LGPL-2.1-or-later", encoding="utf-8")
    manifest = {
        "components": [
            {
                "name": "ffmpeg",
                "version": "7.1",
                "sha256": sha,
                "license": "LGPL-2.1-or-later",
                "source_url": "https://example.invalid/ffmpeg",
            }
        ]
    }
    inventory = {
        "name": "ffmpeg",
        "sha256": sha,
        "buildconf_sha256": sha256_file(bundle / "buildconf.txt"),
    }
    return bundle, manifest, inventory


def test_matching_bundle_passes_and_is_a_valid_report_row(tmp_path):
    bundle, manifest, inventory = make_bundle(tmp_path)
    assert audit_problems(bundle, manifest, inventory) == []
    row = distribution_result(bundle, manifest, inventory, evidence_ref="inv.json")
    assert (row["status"], row["value"]) == ("pass", 1.0)
    report = {
        "format_version": 1,
        "app_sha": "b" * 40,
        "dataset_manifest_hash": "c" * 64,
        "model_snapshot": "n/a",
        "results": [row],
        "blocking": ["distribution/license_inventory_match"],
    }
    validate_report(report)


def test_hash_mismatch_fails(tmp_path):
    bundle, manifest, inventory = make_bundle(tmp_path)
    (bundle / "ffmpeg.exe").write_bytes(b"tampered")
    problems = audit_problems(bundle, manifest, inventory)
    assert "ffmpeg sha256 differs from manifest" in problems
    assert "ffmpeg sha256 differs from inventory" in problems
    assert distribution_result(bundle, manifest, inventory, evidence_ref="i")["status"] == "fail"


def test_inventory_mismatch_alone_fails(tmp_path):
    bundle, manifest, inventory = make_bundle(tmp_path)
    inventory["buildconf_sha256"] = "0" * 64
    assert audit_problems(bundle, manifest, inventory) == [
        "buildconf sha256 differs from inventory"
    ]


@pytest.mark.parametrize("flag", ["--enable-gpl", "--ENABLE-NONFREE"])
def test_gpl_or_nonfree_buildconf_fails(tmp_path, flag):
    bundle, manifest, inventory = make_bundle(tmp_path, conf=f"--x {flag}")
    inventory["buildconf_sha256"] = sha256_file(bundle / "buildconf.txt")
    assert any("buildconf contains" in p for p in audit_problems(bundle, manifest, inventory))


def test_non_lgpl_or_notice_gap_fails(tmp_path):
    bundle, manifest, inventory = make_bundle(tmp_path)
    manifest["components"][0]["license"] = "GPL-3.0"
    problems = audit_problems(bundle, manifest, inventory)
    assert "manifest license is not LGPL" in problems
    assert "NOTICE does not mention manifest license" in problems


def test_missing_files_or_inventory_is_unverified_not_pass(tmp_path):
    bundle, manifest, inventory = make_bundle(tmp_path)
    assert distribution_result(bundle, manifest, None, evidence_ref="i")["status"] == "unverified"
    (bundle / "NOTICE.txt").unlink()
    row = distribution_result(bundle, manifest, inventory, evidence_ref="i")
    assert (row["status"], row["value"]) == ("unverified", None)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert (
        distribution_result(empty, manifest, inventory, evidence_ref="i")["status"] == "unverified"
    )


def test_manifest_without_ffmpeg_fails(tmp_path):
    bundle, _, inventory = make_bundle(tmp_path)
    assert audit_problems(bundle, {"components": []}, inventory) == [
        "runtime manifest must have exactly one ffmpeg component"
    ]


def test_cli_exit_codes(tmp_path, monkeypatch, capsys):
    bundle, manifest, inventory = make_bundle(tmp_path)
    m, i = tmp_path / "m.json", tmp_path / "i.json"
    m.write_text(json.dumps(manifest), encoding="utf-8")
    i.write_text(json.dumps(inventory), encoding="utf-8")
    argv = ["x", str(bundle), str(m), "--inventory", str(i), "--evidence-ref", "e"]
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["problems"] == []
    (bundle / "ffmpeg.exe").write_bytes(b"tampered")
    assert main() == 1
    monkeypatch.setattr(
        "sys.argv", ["x", str(bundle), str(tmp_path / "none.json"), "--evidence-ref", "e"]
    )
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2
