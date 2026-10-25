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
        ("2026.10.19.1", (2026, 10, 19, 1, 1)),
        (" 0.13.4 ", (0, 13, 4, 0, 1)),
        ("26.2.2.post1", (26, 2, 2, 0, 1)),
        ("26.2.2+build1", (26, 2, 2, 0, 1)),
        ("26.2.2+ubuntu1", (26, 2, 2, 0, 1)),
        ("26.2.2.post1.dev1", (26, 2, 2, 0, 1)),
        ("26.2.2-post2", (26, 2, 2, 0, 1)),
    ],
)
def test_release_versions_are_final(text, expected):
    assert trust.parse_version(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "26.2.2a1",
        "26.2.2b1",
        "26.2.2c1",
        "26.2.2rc1",
        "26.2.2-rc1",
        "26.2.2.dev0",
        "26.2.2alpha",
        "26.2.2beta2",
        "26.2.2pre",
        "26.2.2preview1",
        "26.2.2RC1",
        "26.2.2rc1.post2",
        "26.2.2rc1+local",
    ],
)
def test_pre_and_dev_releases_are_below_the_release(text):
    assert trust.parse_version(text) == (26, 2, 2, 0, 0)
    assert trust.parse_version(text) < trust.parse_version("26.2.2")


def test_pre_release_marker_in_local_label_does_not_downgrade_a_release():
    assert trust.parse_version("26.2.2+rc1") == trust.parse_version("26.2.2")
    assert trust.parse_version("26.2.2+dev") == trust.parse_version("26.2.2")


def test_parse_version_compares_numerically_not_lexically():
    assert trust.parse_version("26.10.0") > trust.parse_version("26.2.2")
    assert trust.parse_version("26.2.3rc1") > trust.parse_version("26.2.2")
    assert trust.parse_version("2026.10.19.1") > trust.parse_version("2026.10.19")


def test_equivalent_versions_compare_equal():
    assert trust.parse_version("26.2") == trust.parse_version("26.2.0")


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "abc",
        "v26.2.2",
        "1.2.3.4.5",
        ".5",
        "26.2.2final",
        "26.2.2abc",
        "26.2.2-1",
        "26.2.2rc1b2",
    ],
)
def test_parse_version_rejects_unrecognized_text(text):
    with pytest.raises(ValueError):
        trust.parse_version(text)


def test_minimum_and_newer_versions_pass():
    assert trust.find_problems(GOOD) == []
    newer = {**GOOD, "pip": "26.3.0", "certifi": "2026.11.1", "uv": "0.13.4", "sigstore": "4.5.1"}
    assert trust.find_problems(newer) == []
    assert trust.find_problems({**GOOD, "pip": "26.2.2+local"}) == []


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
    (problem,) = trust.find_problems({**GOOD, package: version})
    assert problem.startswith(f"{package} {version} is older than ")


def test_required_packages_must_be_installed_but_optional_ones_need_not():
    problems = trust.find_problems({**GOOD, "pip": None, "certifi": None})
    assert sorted(problem.split()[0] for problem in problems) == ["certifi", "pip"]
    assert all("is not installed" in problem for problem in problems)
    assert trust.find_problems({**GOOD, "uv": None, "sigstore": None}) == []


def test_unparseable_versions_are_problems_not_exceptions():
    (problem,) = trust.find_problems({**GOOD, "pip": "1.2.3.4.5"})
    assert problem == "pip version '1.2.3.4.5' cannot be compared"


def test_unknown_suffixes_fail_closed_instead_of_passing_as_releases():
    (problem,) = trust.find_problems({**GOOD, "certifi": "2026.10.19final"})
    assert problem == "certifi version '2026.10.19final' cannot be compared"


def test_bundle_must_be_a_nonempty_file(tmp_path):
    good = tmp_path / "cacert.pem"
    good.write_text("pem", encoding="utf-8")
    empty = tmp_path / "empty.pem"
    empty.write_text("", encoding="utf-8")
    assert trust.find_bundle_problems(good) == []
    assert "does not exist" in trust.find_bundle_problems(tmp_path / "missing.pem")[0]
    assert "does not exist" in trust.find_bundle_problems(tmp_path)[0]
    assert "is empty" in trust.find_bundle_problems(empty)[0]


def test_unreadable_bundle_is_a_problem_not_a_crash(monkeypatch, tmp_path):
    bundle = tmp_path / "cacert.pem"
    bundle.write_text("pem", encoding="utf-8")

    def deny(self):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "stat", deny)
    (problem,) = trust.find_bundle_problems(bundle)
    assert problem.startswith(f"certifi CA bundle cannot be accessed: {bundle}")
    assert "denied" in problem


def run_main(monkeypatch, capsys, versions, tmp_path, env=None, bundle_text="test"):
    bundle = tmp_path / "cacert.pem"
    if bundle_text is not None:
        bundle.write_text(bundle_text, encoding="utf-8")
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
    assert f"certifi CA bundle: {bundle} (exists=True)" in output
    assert "pip: 26.2.2" in output and "uv: not installed" in output
    assert "ssl default cafile" in output
    assert "ERROR" not in output and "WARNING" not in output


def test_main_fails_for_an_outdated_environment(monkeypatch, capsys, tmp_path):
    code, output, _ = run_main(monkeypatch, capsys, {**GOOD, "pip": "26.1.2"}, tmp_path)
    assert code == 1
    assert "ERROR: pip 26.1.2 is older than 26.2.2" in output


def test_main_fails_when_a_required_package_is_missing(monkeypatch, capsys, tmp_path):
    code, output, _ = run_main(monkeypatch, capsys, {**GOOD, "pip": None}, tmp_path)
    assert code == 1
    assert "ERROR: pip is not installed (need >= 26.2.2)" in output


def test_main_fails_when_the_ca_bundle_is_missing_or_empty(monkeypatch, capsys, tmp_path):
    code, output, bundle = run_main(monkeypatch, capsys, GOOD, tmp_path, bundle_text=None)
    assert code == 1
    assert f"ERROR: certifi CA bundle does not exist: {bundle}" in output
    code, output, bundle = run_main(monkeypatch, capsys, GOOD, tmp_path, bundle_text="")
    assert code == 1
    assert f"ERROR: certifi CA bundle is empty: {bundle}" in output


def test_main_reports_a_broken_certifi_install_as_an_error(monkeypatch, capsys, tmp_path):
    def broken():
        raise OSError("cacert.pem unreadable")

    monkeypatch.setitem(sys.modules, "certifi", SimpleNamespace(where=broken))
    monkeypatch.setattr(trust, "_installed_version", lambda name: GOOD.get(name))
    assert trust.main() == 1
    assert "ERROR: certifi cannot be imported: OSError('cacert.pem unreadable')" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize("variable", trust.TRUST_OVERRIDE_VARIABLES)
def test_main_warns_when_the_trust_store_is_overridden(monkeypatch, capsys, tmp_path, variable):
    code, output, _ = run_main(
        monkeypatch, capsys, GOOD, tmp_path, env={variable: "/custom/ca.pem"}
    )
    assert code == 0
    assert f"WARNING: {variable} overrides the trust store: /custom/ca.pem" in output
    assert output.count("WARNING") == 1


def step_blocks(source):
    """Return, for each `steps:` list in a workflow, the text of every `- ` step (any indent)."""
    lists = []
    lines = source.splitlines()
    index = 0
    while index < len(lines):
        header = re.match(r"^(\s*)steps:\s*$", lines[index])
        index += 1
        if not header:
            continue
        base = len(header.group(1))
        steps, item_indent = [], None
        while index < len(lines):
            line = lines[index]
            if line.lstrip().startswith("#"):  # コメント行(例: "# - foo")は step として数えない
                index += 1
                continue
            indent = len(line) - len(line.lstrip())
            if line.strip() and indent <= base:
                break
            if line.lstrip().startswith("- ") and item_indent in (None, indent):
                item_indent = indent
                steps.append(line)
            elif steps:
                steps[-1] += "\n" + line
            index += 1
        lists.append(steps)
    return lists


def test_step_blocks_ignores_comments_and_keeps_multiline_steps():
    source = (
        "jobs:\n  a:\n    steps:\n      # - fake\n"
        "      - uses: actions/setup-python@v5\n        with:\n          x: 1\n"
        "      - uses: ./.github/actions/python-trust\n"
    )
    (steps,) = step_blocks(source)
    assert len(steps) == 2
    assert "with:" in steps[0] and "python-trust" in steps[1]


def test_the_trust_action_upgrades_before_verifying_from_the_workspace():
    action = REPO / ".github" / "actions" / "python-trust" / "action.yml"
    text = action.read_text(encoding="utf-8")
    assert "pip>=26.2.2" in text and "certifi>=2026.10.19" in text
    upgrade = text.index("pip install --upgrade")
    verify = text.index("check_python_trust.py")
    assert upgrade < verify
    assert "$GITHUB_WORKSPACE/scripts/check_python_trust.py" in text


def test_every_python_setup_is_immediately_followed_by_the_trust_action():
    checked = 0
    workflows = sorted((REPO / ".github" / "workflows").glob("*.y*ml"))
    for workflow in workflows:
        source = workflow.read_text(encoding="utf-8")
        parsed = 0
        for position, steps in enumerate(step_blocks(source)):
            setups = [i for i, step in enumerate(steps) if "actions/setup-python@" in step]
            for index in setups:
                following = steps[index + 1] if index + 1 < len(steps) else ""
                assert "uses: ./.github/actions/python-trust" in following, (
                    f"{workflow.name}: steps #{position}: setup-python is not followed by trust"
                )
                parsed += 1
            trusts = [i for i, step in enumerate(steps) if "python-trust" in step]
            assert trusts == [i + 1 for i in setups], f"{workflow.name}: steps #{position}"
        # 解析で取りこぼした箇所を黙って通さない: 文字列の総数と一致させる。
        assert parsed == len(re.findall(r"uses:\s*actions/setup-python@", source)), workflow.name
        checked += parsed
    assert checked == 14


def test_local_scripts_upgrade_and_verify_before_every_package_install():
    for name in ("check.ps1", "build.ps1"):
        lines = (REPO / "scripts" / name).read_text(encoding="utf-8").splitlines()
        upgrade = next(i for i, line in enumerate(lines) if "pip>=26.2.2" in line)
        verify = next(i for i, line in enumerate(lines) if "check_python_trust.py" in line)
        installs = [i for i, line in enumerate(lines) if "pip install" in line and i != upgrade]
        assert installs, name
        assert upgrade < verify < min(installs), name
