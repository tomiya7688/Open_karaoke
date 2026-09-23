import json
import os
import subprocess
import sys
import time

import httpx


def test_real_process_startup_and_shutdown(tmp_path):
    ready = tmp_path / "ready.json"
    env = {**os.environ, "OPEN_KARAOKE_ANALYSIS_TOKEN": "t" * 48}
    command = [
        sys.executable,
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
