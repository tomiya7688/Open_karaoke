import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx


def real_interpreter_launch(python, env):
    """
    目的: Windows venvのpython.exe中継を避け、Popen PIDを実プロセスと一致させる
    引数: [python: 起動したいpython実行ファイル, env: 子プロセス環境]
    戻り値: (実行ファイル, 環境) のtuple。venvでなければ入力をそのまま返す
    処理: [1: pyvenv.cfgのhomeを読む, 2: home直下のpython.exeを使い__PYVENV_LAUNCHER__を設定]
    """
    if os.name != "nt":
        return python, env
    config = Path(python).parent.parent / "pyvenv.cfg"
    if not config.is_file():
        return python, env
    for line in config.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip().lower() == "home":
            base = Path(value.strip()) / "python.exe"
            if base.is_file():
                return str(base), {**env, "__PYVENV_LAUNCHER__": str(python)}
    return python, env


def test_real_process_startup_and_shutdown(tmp_path):
    ready = tmp_path / "ready.json"
    env = {**os.environ, "OPEN_KARAOKE_ANALYSIS_TOKEN": "t" * 48}
    executable, env = real_interpreter_launch(sys.executable, env)
    command = [
        executable,
        "-m",
        "open_karaoke_analysis",
        "--artifact-root",
        str(tmp_path / "artifacts"),
        "--ready-file",
        str(ready),
    ]
    process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if process.poll() is not None:
                raise AssertionError(process.stderr.read().decode())
            assert time.monotonic() < deadline, "Readiness deadline exceeded"
            time.sleep(0.01)
        handshake = json.loads(ready.read_text())
        assert handshake["pid"] == process.pid
        with httpx.Client(
            base_url=f"http://127.0.0.1:{handshake['port']}",
            trust_env=False,
            headers={"Authorization": "Bearer " + "t" * 48},
        ) as client:
            assert client.get("/health").json()["instance_id"] == handshake["instance_id"]
            assert client.get("/models").status_code == 200
            assert client.post("/shutdown").status_code == 200
        assert process.wait(timeout=10) == 0
        assert not ready.exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        process.stderr.close()


def test_no_token_fails_closed(tmp_path):
    env = dict(os.environ)
    env.pop("OPEN_KARAOKE_ANALYSIS_TOKEN", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "open_karaoke_analysis",
            "--artifact-root",
            str(tmp_path),
            "--ready-file",
            str(tmp_path / "ready.json"),
        ],
        env=env,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert b"OPEN_KARAOKE_ANALYSIS_TOKEN" in result.stderr
    assert not (tmp_path / "ready.json").exists()


def test_real_interpreter_launch_follows_venv_home(tmp_path, monkeypatch):
    base = tmp_path / "base"
    base.mkdir()
    (base / "python.exe").write_text("")
    venv = tmp_path / "venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(f"home = {base}\nversion = 3\n", encoding="utf-8")
    launcher = venv / "Scripts" / "python.exe"
    monkeypatch.setattr(os, "name", "nt")
    executable, env = real_interpreter_launch(str(launcher), {"A": "1"})
    assert executable == str(base / "python.exe")
    assert env == {"A": "1", "__PYVENV_LAUNCHER__": str(launcher)}


def test_real_interpreter_launch_unchanged_without_venv(tmp_path, monkeypatch):
    (tmp_path / "Scripts").mkdir()
    python = tmp_path / "Scripts" / "python.exe"
    monkeypatch.setattr(os, "name", "nt")
    assert real_interpreter_launch(str(python), {"A": "1"}) == (str(python), {"A": "1"})
    monkeypatch.setattr(os, "name", "posix")
    assert real_interpreter_launch("/usr/bin/python", {}) == ("/usr/bin/python", {})
