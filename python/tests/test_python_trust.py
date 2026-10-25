import importlib.util
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "check_python_trust", REPO / "scripts" / "check_python_trust.py"
)
trust = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trust)

GOOD = {"pip": "26.2.2", "certifi": "2026.10.19", "uv": None, "sigstore": None}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("26.2.2", (26, 2, 2, 0, 1)),
        ("2026.10.19", (2026, 10, 19, 0, 1)),
        (" 0.13.4 ", (0, 13, 4, 0, 1)),
        ("26.2.2rc1", (26, 2, 2, 0, 0)),
        ("26.2.2.dev0", (26, 2, 2, 0, 0)),
        ("26.2.2.post1", (26, 2, 2, 0, 1)),
    ],
)
def test_parse_version(text, expected):
    assert trust.parse_version(text) == expected


def test_parse_version_compares_numerically_not_lexically():
    assert trust.parse_version("26.10.0") > trust.parse_version("26.2.2")
    assert trust.parse_version("26.2.2rc1") < trust.parse_version("26.2.2")


def test_equivalent_versions_compare_equal():
    assert trust.parse_version("26.2") == trust.parse_version("26.2.0")


@pytest.mark.parametrize("text", ["", "abc", "v26.2.2", "1.2.3.4.5"])
def test_parse_version_rejects_unrecognized_text(text):
    with pytest.raises(ValueError):
        trust.parse_version(text)


def test_minimum_and_newer_versions_pass():
    assert trust.find_problems(GOOD) == []
    newer = {**GOOD, "pip": "26.3.0", "certifi": "2026.11.1", "uv": "0.13.4", "sigstore": "4.5.1"}
    assert trust.find_problems(newer) == []


@pytest.mark.parametrize(
    ("package", "version"),
    [
        ("pip", "26.2.1"),
        ("pip", "26.2.2rc1"),
        ("certifi", "2026.10.18"),
        ("uv", "0.13.3"),
        ("sigstore", "4.5.0"),
    ],
)
def test_older_versions_are_reported(package, version):
    problems = trust.find_problems({**GOOD, package: version})
    assert len(problems) == 1 and package in problems[0]


def test_required_packages_must_be_installed_but_optional_ones_need_not():
    problems = trust.find_problems({**GOOD, "pip": None, "certifi": None})
    assert len(problems) == 2
    assert trust.find_problems({**GOOD, "uv": None, "sigstore": None}) == []


def run_main(monkeypatch, capsys, versions, tmp_path, env=None):
    bundle = tmp_path / "cacert.pem"
    bundle.write_text("test", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "certifi", SimpleNamespace(where=lambda: str(bundle)))
    monkeypatch.setattr(trust, "_installed_version", lambda name: versions.get(name))
    for variable in trust.TRUST_OVERRIDE_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    code = trust.main()
    return code, capsys.readouterr().out, bundle


def test_main_reports_the_actual_trust_sources(monkeypatch, capsys, tmp_path):
    code, output, bundle = run_main(monkeypatch, capsys, GOOD, tmp_path)
    assert code == 0
    assert f"certifi CA bundle: {bundle}" in output
    assert "pip: 26.2.2" in output and "uv: not installed" in output
    assert "ssl default cafile" in output
    assert "ERROR" not in output and "WARNING" not in output


def test_main_fails_for_an_outdated_environment(monkeypatch, capsys, tmp_path):
    code, output, _ = run_main(monkeypatch, capsys, {**GOOD, "pip": "26.1.2"}, tmp_path)
    assert code == 1
    assert "ERROR: pip 26.1.2 is older than 26.2.2" in output


def test_main_warns_when_the_trust_store_is_overridden(monkeypatch, capsys, tmp_path):
    code, output, _ = run_main(
        monkeypatch, capsys, GOOD, tmp_path, env={"SSL_CERT_FILE": "/custom/ca.pem"}
    )
    assert code == 0
    assert "WARNING: SSL_CERT_FILE overrides the trust store: /custom/ca.pem" in output


def test_every_python_setup_is_followed_by_the_trust_action():
    action = REPO / ".github" / "actions" / "python-trust" / "action.yml"
    text = action.read_text(encoding="utf-8")
    assert "pip>=26.2.2" in text and "certifi>=2026.10.19" in text
    assert "scripts/check_python_trust.py" in text
    for workflow in sorted((REPO / ".github" / "workflows").glob("*.yml")):
        source = workflow.read_text(encoding="utf-8")
        setups = len(re.findall(r"uses:\s*actions/setup-python@", source))
        trusted = len(re.findall(r"uses:\s*\./\.github/actions/python-trust", source))
        assert setups == trusted, f"{workflow.name}: {setups} setup-python vs {trusted} trust"


def test_local_scripts_upgrade_before_installing_the_package():
    for name in ("check.ps1", "build.ps1"):
        lines = (REPO / "scripts" / name).read_text(encoding="utf-8").splitlines()
        upgrade = next(i for i, line in enumerate(lines) if "pip>=26.2.2" in line)
        verify = next(i for i, line in enumerate(lines) if "check_python_trust.py" in line)
        install = next(i for i, line in enumerate(lines) if "pip install -e" in line)
        assert upgrade < verify < install, name
