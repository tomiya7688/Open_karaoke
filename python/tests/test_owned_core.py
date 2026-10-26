import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from open_karaoke_analysis import owned_core as api

INSTANCE = "11111111-1111-4111-8111-111111111111"
READY = {"event": "core_ready", "api_version": 1, "instance_id": INSTANCE, "port": 12345}
WAIT_TIMEOUT = 8 if os.name == "nt" else 5


def _child(monkeypatch, raw=None):
    """{
    責務: [起動契約を記録する有限の子プロセスを置き換える]
    処理: [1: pipeと待機結果を用意, 2: PopenとHTTP clientを置換]
    引数: [monkeypatch: 依存置換, raw: ready本文]
    戻り値: [process・spawn・client factory・stdinの送信記録]
    }"""
    wire = []
    process = Mock(pid=12345)
    process.stdin = Mock()
    process.stdin.write.side_effect = lambda value: wire.append(value) or len(value)
    process.stdout = io.BytesIO(raw if raw is not None else json.dumps(READY).encode() + b"\n")
    process.stdout.readline = Mock(wraps=process.stdout.readline)
    process.stderr = io.BytesIO(b"private child diagnostics")
    process.poll.return_value = None
    spawn = Mock(return_value=process)
    factory = Mock()
    monkeypatch.setattr(api.subprocess, "Popen", spawn)
    monkeypatch.setattr(api, "CoreJobClient", factory)
    monkeypatch.setattr(api, "_exited", lambda child: child.poll() is not None)
    if os.name != "nt":
        monkeypatch.setattr(api, "_wait_posix_exit", Mock())
        monkeypatch.setattr(api.os, "killpg", Mock())
    return process, spawn, factory, wire


def test_owned_bootstrap_context_and_cleanup(monkeypatch, tmp_path):
    """{
    責務: [tokenは新規生成しstdin一回だけで渡し、正常・例外終了ともEOFで閉じる]
    処理: [1: 2回起動, 2: argv/env/stdioとsession照合を確認, 3: 終了後アクセスを拒否]
    引数: [monkeypatch: 依存置換, tmp_path: 専用データ場所]
    戻り値: []
    }"""
    tokens = []
    for fail in (False, True):
        process, spawn, factory, wire = _child(monkeypatch)
        owner = api.OwnedCore.start(sys.executable, tmp_path, sys.executable)
        token = wire[0].decode().rstrip("\n")
        tokens.append(token)
        assert len(wire) == 1 and len(token) == 64 and wire[0].endswith(b"\n")
        assert all(c in "0123456789abcdef" for c in token)
        args, kwargs = spawn.call_args
        assert args == ([sys.executable],) and kwargs["shell"] is False
        assert token not in repr(kwargs) and token not in repr(owner)
        assert kwargs["cwd"] == Path(sys.executable).parent
        assert kwargs["env"]["OPEN_KARAOKE_BIND"] == "127.0.0.1:0"
        assert kwargs["env"]["OPEN_KARAOKE_DATA_DIR"] == str(tmp_path)
        assert kwargs["env"]["OPEN_KARAOKE_PYTHON"] == sys.executable
        assert all(kwargs[name] == subprocess.PIPE for name in ("stdin", "stdout", "stderr"))
        assert kwargs["start_new_session"] == (os.name != "nt")
        factory.assert_called_once_with("http://127.0.0.1:12345", token, INSTANCE)
        factory.return_value.verify_session.assert_called_once_with()
        if fail:
            with pytest.raises(LookupError):
                with owner:
                    raise LookupError("caller failure")
        else:
            with owner as connected:
                assert connected.client is factory.return_value
        owner.close()
        process.stdin.close.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=WAIT_TIMEOUT)
        process.stdout.readline.assert_called_once_with(8193)
        assert process.stdout.closed and process.stderr.closed
        with pytest.raises(api.CoreLaunchError):
            _ = owner.client
    assert tokens[0] != tokens[1]


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{}\n",
        b"[]\n",
        b"not JSON\n",
        b" " * 8193,
        b" " * 8193 + b"\n",
        json.dumps(READY).encode(),
        (json.dumps({**READY, "api_version": True}) + "\n").encode(),
        (json.dumps({**READY, "port": True}) + "\n").encode(),
        (json.dumps({**READY, "port": 0}) + "\n").encode(),
        (json.dumps({**READY, "instance_id": "bad"}) + "\n").encode(),
        (json.dumps(READY)[:-1] + ', "port": 1}\n').encode(),
    ],
    ids=[
        "eof",
        "empty",
        "array",
        "json",
        "oversize",
        "oversize-lf",
        "missing-lf",
        "version",
        "bool-port",
        "zero-port",
        "uuid",
        "duplicate",
    ],
)
def test_bad_ready_never_connects_and_always_closes(monkeypatch, tmp_path, raw):
    """{
    責務: [不正readyを認証先として使わず所有Coreを終了する]
    処理: [1: 不正な起動出力を返す, 2: 接続なしとEOF終了を確認]
    引数: [monkeypatch: 依存置換, tmp_path: データ場所, raw: 不正ready]
    戻り値: []
    }"""
    process, _, factory, wire = _child(monkeypatch, raw)
    with pytest.raises(api.CoreLaunchError) as error:
        api.OwnedCore.start(sys.executable, tmp_path, sys.executable)
    assert wire[0].decode().strip() not in str(error.value)
    factory.assert_not_called()
    process.stdin.close.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=WAIT_TIMEOUT)


def test_session_failure_is_sanitized_and_cleanup_runs(monkeypatch, tmp_path):
    """{
    責務: [認証失敗を子出力や秘密なしで返し別Coreへ再接続しない]
    処理: [1: session照合を失敗させる, 2: 一度の起動と終了を確認]
    引数: [monkeypatch: 依存置換, tmp_path: データ場所]
    戻り値: []
    }"""
    process, spawn, factory, _ = _child(monkeypatch)
    factory.return_value.verify_session.side_effect = RuntimeError("private diagnostics")
    with pytest.raises(api.CoreLaunchError) as error:
        api.OwnedCore.start(sys.executable, tmp_path, sys.executable)
    assert "private" not in str(error.value)
    assert spawn.call_count == factory.call_count == 1
    process.wait.assert_called_once()


def test_startup_deadline_cleans_up_even_during_session_io(monkeypatch, tmp_path):
    """{
    責務: [session I/Oも含む起動期限で待機を打ち切り、遅い結果を採用しない]
    処理: [1: session応答をブロック, 2: 期限超過, 3: EOFで解放され終了する]
    引数: [monkeypatch: 依存置換, tmp_path: データ場所]
    戻り値: []
    }"""
    import threading

    process, _, factory, _ = _child(monkeypatch)
    release = threading.Event()
    factory.return_value.verify_session.side_effect = lambda: release.wait(2)
    process.stdin.close.side_effect = release.set
    started = time.monotonic()
    with pytest.raises(api.CoreLaunchError, match="deadline"):
        api.OwnedCore.start(sys.executable, tmp_path, sys.executable, startup_timeout=0.02)
    assert time.monotonic() - started < 1
    process.wait.assert_called_once()


@pytest.mark.parametrize(
    "field,value",
    [
        ("executable", "relative"),
        ("data_directory", "relative"),
        ("python_executable", "relative"),
        ("executable", None),
        ("startup_timeout", True),
        ("startup_timeout", float("nan")),
        ("startup_timeout", float("inf")),
        ("startup_timeout", 0),
        ("shutdown_timeout", 31),
    ],
)
def test_invalid_configuration_has_no_spawn(monkeypatch, tmp_path, field, value):
    """{
    責務: [パス・待機予算の不正を子の生成前に拒否する]
    処理: [1: 一項目を不正にする, 2: ValueErrorと起動なしを確認]
    引数: [monkeypatch: 依存置換, tmp_path: データ場所, field・value: 不正設定]
    戻り値: []
    }"""
    _, spawn, _, _ = _child(monkeypatch)
    options = dict(
        executable=sys.executable, data_directory=tmp_path, python_executable=sys.executable
    )
    options[field] = value
    with pytest.raises(ValueError):
        api.OwnedCore.start(**options)
    spawn.assert_not_called()


def test_forced_cleanup_targets_only_owned_tree(monkeypatch, tmp_path):
    """{
    責務: [EOF後に停止しない所有子だけをOS別のtree操作で終了する]
    処理: [1: 正常待機の期限を超過させる, 2: 所有PIDと有限の再待機を確認]
    引数: [monkeypatch: 依存置換, tmp_path: データ場所]
    戻り値: []
    }"""
    process, _, _, _ = _child(monkeypatch)
    if os.name == "nt":
        process.wait.side_effect = [subprocess.TimeoutExpired("Core", 8), 0]
    terminate = Mock()
    if os.name == "nt":
        monkeypatch.setattr(api.subprocess, "run", terminate)
    else:
        monkeypatch.setattr(api.os, "killpg", terminate)
    owner = api.OwnedCore.start(sys.executable, tmp_path, sys.executable)
    owner.close()
    if os.name == "nt":
        assert terminate.call_args.args[0][0] == str(
            Path(os.environ["SystemRoot"]) / "System32/taskkill.exe"
        )
        assert terminate.call_args.args[0][1:] == ["/PID", "12345", "/T", "/F"]
        assert terminate.call_args.kwargs["timeout"] == 5
    else:
        terminate.assert_called_once_with(12345, api.signal.SIGKILL)
    assert process.wait.call_args_list[-1].kwargs == {"timeout": 5}


def test_spawn_error_does_not_disclose_child_diagnostics(monkeypatch, tmp_path):
    """{
    責務: [OS起動エラーを秘密やパスの詳細なしで伝える]
    処理: [1: Popenを失敗させる, 2: sanitizedエラーと一度の起動を確認]
    引数: [monkeypatch: 依存置換, tmp_path: データ場所]
    戻り値: []
    }"""
    _, spawn, _, _ = _child(monkeypatch)
    spawn.side_effect = OSError("private diagnostics")
    with pytest.raises(api.CoreLaunchError, match="Could not launch Core"):
        api.OwnedCore.start(sys.executable, tmp_path, sys.executable)
    assert spawn.call_count == 1


def test_partial_bootstrap_write_never_connects(monkeypatch, tmp_path):
    """{
    責務: [bootstrapの部分書込みを成功にせず、壊れたstdinでも終了処理する]
    処理: [1: writeを短く返す, 2: closeを失敗させる, 3: HTTPなしとwaitを確認]
    引数: [monkeypatch: 依存置換, tmp_path: データ場所]
    戻り値: []
    }"""
    process, _, factory, _ = _child(monkeypatch)
    process.stdin.write.side_effect = None
    process.stdin.write.return_value = 1
    process.stdin.close.side_effect = BrokenPipeError("private")
    with pytest.raises(api.CoreLaunchError):
        api.OwnedCore.start(sys.executable, tmp_path, sys.executable)
    process.wait.assert_called_once()
    factory.assert_not_called()


def test_shutdown_failure_revokes_access_and_allows_cleanup_again(monkeypatch, tmp_path):
    """{
    責務: [停止確認失敗を成功にせず、接続失効後にもcleanupを再実行可能にする]
    処理: [1: wait失敗, 2: client拒否, 3: 再closeで停止を確認]
    引数: [monkeypatch: 依存置換, tmp_path: データ場所]
    戻り値: []
    }"""
    process, _, _, _ = _child(monkeypatch)
    owner = api.OwnedCore.start(sys.executable, tmp_path, sys.executable)
    process.wait.side_effect = [OSError("private"), 0]
    with pytest.raises(api.CoreLaunchError, match="shutdown"):
        owner.close()
    with pytest.raises(api.CoreLaunchError):
        _ = owner.client
    owner.close()
    assert process.wait.call_count == 2


def test_missing_windows_system_root_reports_shutdown_failure(monkeypatch, tmp_path):
    """{
    責務: [Windows終了に必要な設定欠落をKeyErrorとして漏らさない]
    処理: [1: 起動後のOS境界を置換, 2: SystemRootなしで終了, 3: 明示失敗を確認]
    引数: [monkeypatch: OS境界置換, tmp_path: データ場所]
    戻り値: []
    }"""
    from types import SimpleNamespace

    process, _, _, _ = _child(monkeypatch)
    owner = api.OwnedCore.start(sys.executable, tmp_path, sys.executable)
    process.wait.side_effect = subprocess.TimeoutExpired("Core", 8)
    monkeypatch.setattr(api, "os", SimpleNamespace(name="nt", environ={}))
    with pytest.raises(api.CoreLaunchError, match="shutdown"):
        owner.close()


@pytest.mark.parametrize("shutdown_fails", [False, True])
def test_pipe_close_failure_attempts_both_streams(monkeypatch, tmp_path, shutdown_fails):
    """{
    責務: [stdoutのclose失敗でもstderrを閉じ、失敗を明示する]
    処理: [1: 起動, 2: reader終了後にcloseを失敗させる, 3: 両pipeの処理を確認]
    引数: [monkeypatch: 依存置換, tmp_path: データ場所, shutdown_fails: waitも失敗するか]
    戻り値: []
    }"""
    process, _, _, _ = _child(monkeypatch)
    owner = api.OwnedCore.start(sys.executable, tmp_path, sys.executable)
    for thread in owner._threads:
        thread.join(timeout=1)
        assert not thread.is_alive()
    if shutdown_fails:
        process.wait.side_effect = [OSError("private"), 0]
    process.stdout.close = Mock(side_effect=OSError("private"))
    expected = "shutdown; pipe cleanup also failed" if shutdown_fails else "pipes"
    with pytest.raises(api.CoreLaunchError, match=expected):
        owner.close()
    assert process.stderr.closed
    process.stdout.close.side_effect = None
    owner.close()
    assert process.stdout.close.call_count == 2
    assert process.wait.call_count == (2 if shutdown_fails else 1)
    owner.close()
    assert process.wait.call_count == (2 if shutdown_fails else 1)


def test_ready_at_exact_byte_limit_is_accepted(monkeypatch, tmp_path):
    """{
    責務: [LF込み8KiBちょうどのreadyを上限付きで読んで受理する]
    処理: [1: 空白とJSONで境界長を生成, 2: 起動, 3: readline上限を照合]
    引数: [monkeypatch: 依存置換, tmp_path: データ場所]
    戻り値: []
    }"""
    raw = json.dumps(READY).encode()
    process, _, _, _ = _child(monkeypatch, b" " * (8191 - len(raw)) + raw + b"\n")
    with api.OwnedCore.start(sys.executable, tmp_path, sys.executable):
        process.stdout.readline.assert_called_once_with(8193)


def test_core_exit_before_context_entry_still_closes(monkeypatch, tmp_path):
    """{
    責務: [起動直後に子が終了してもwith入口から資源を片付ける]
    処理: [1: 起動, 2: 子の終了を通知, 3: enter失敗とcleanupを確認]
    引数: [monkeypatch: 依存置換, tmp_path: データ場所]
    戻り値: []
    }"""
    process, _, _, _ = _child(monkeypatch)
    owner = api.OwnedCore.start(sys.executable, tmp_path, sys.executable)
    process.poll.return_value = 0
    with pytest.raises(api.CoreLaunchError):
        with owner:
            pytest.fail("Exited Core must not enter context")
    process.wait.assert_called_once()


@pytest.mark.parametrize("already_exited", [False, True])
def test_posix_observes_without_reaping_then_kills_group(monkeypatch, already_exited):
    """{
    責務: [終了済みleaderも回収前に所有groupを停止し、PID再利用を避ける]
    処理: [1: OS境界を記録, 2: 生存照会, 3: close, 4: 観測→group停止→回収を検証]
    引数: [monkeypatch: OS境界置換, already_exited: Coreが先に終了したか]
    戻り値: []
    }"""
    from types import SimpleNamespace

    events = []
    process = Mock(pid=12345, stdin=Mock(), stdout=io.BytesIO(), stderr=io.BytesIO())
    process.wait.side_effect = lambda **kwargs: events.append("reap")
    observe = Mock(
        side_effect=lambda *args: events.append("observe") or (object() if already_exited else None)
    )
    kill = Mock(side_effect=lambda *args: events.append("group-kill"))
    monkeypatch.setattr(
        api,
        "os",
        SimpleNamespace(
            name="posix", waitid=observe, killpg=kill, P_PID=1, WEXITED=4, WNOHANG=1, WNOWAIT=8
        ),
    )
    monkeypatch.setattr(api, "signal", SimpleNamespace(SIGKILL=9))
    monkeypatch.setattr(
        api, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 1]), sleep=Mock())
    )
    owner = api.OwnedCore(process, 1)
    owner._client = Mock()
    if already_exited:
        with pytest.raises(api.CoreLaunchError):
            _ = owner.client
    else:
        assert owner.client is owner._client
    owner.close()
    owner.close()
    process.poll.assert_not_called()
    observe.assert_called_with(1, 12345, 4 | 1 | 8)
    kill.assert_called_once_with(12345, 9)
    process.wait.assert_called_once_with(timeout=5)
    assert events == ["observe", "observe", "group-kill", "reap"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux real process-group regression")
def test_linux_crashed_leader_does_not_leave_running_descendant():
    """{
    責務: [実Linuxのleader異常終了後にも子孫を停止しPIDを回収する]
    処理: [1: 専用sessionに有限寿命の子孫を起動してleaderを異常終了,
      2: WNOWAITで終了を観測, 3: ownerでgroup停止して子孫の終了を確認]
    引数: []
    戻り値: []
    補足: [stdlibプロセスの回帰テスト。Coreや実モデルの精度評価ではない]
    }"""
    code = (
        "import subprocess,sys,os; p=subprocess.Popen([sys.executable,'-c',"
        "'import time; time.sleep(5)']); print(p.pid,flush=True); os._exit(3)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        bufsize=0,
    )
    owner = api.OwnedCore(process, 0.5)
    try:
        descendant = int(process.stdout.readline(32))
        deadline = time.monotonic() + 2
        while not api._exited(process):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        owner._client = Mock()
        with pytest.raises(api.CoreLaunchError):
            _ = owner.client
        assert process.returncode is None
        owner.close()
        assert process.returncode == 3
        state = Path(f"/proc/{descendant}/stat")
        deadline = time.monotonic() + 2
        while state.exists():
            try:
                status = state.read_text().rpartition(")")[2].split()[0]
            except FileNotFoundError:
                break
            if status == "Z":
                break
            assert time.monotonic() < deadline, "Descendant remained running after group shutdown"
            time.sleep(0.01)
    finally:
        owner.close()


@pytest.mark.skipif(not os.environ.get("OPEN_KARAOKE_TEST_CORE"), reason="real Core opt-in")
def test_real_core_job_and_restart(tmp_path):
    """{
    責務: [実Core・Python workerで認証Job、EOF終了と新規sessionを確認する]
    処理: [1: 起動してmock解析Jobを終端まで観測, 2: EOF終了, 3: 再起動して旧IDを拒否]
    引数: [tmp_path: test専用データ場所]
    戻り値: []
    補足: [解析はmock-v1であり実モデル精度・GUI・benchmarkの検証ではない]
    }"""
    from open_karaoke_analysis.core_job_client import CoreJobError

    core = os.environ["OPEN_KARAOKE_TEST_CORE"]
    python = os.environ.get("OPEN_KARAOKE_TEST_PYTHON", sys.executable)
    clients = []
    old_id = None
    for _ in range(2):
        with api.OwnedCore.start(core, tmp_path, python) as owner:
            clients.append(owner.client)
            if old_id is not None:
                with pytest.raises(CoreJobError, match="404"):
                    owner.client.get_job(old_id)
            job = owner.client.create_job(
                {
                    "kind": "analysis.mock",
                    "analysis": {"model_id": "mock-v1", "options": {"steps": 2, "delay_ms": 10}},
                }
            )
            deadline = time.monotonic() + 20
            while job.status in ("queued", "running"):
                assert time.monotonic() < deadline
                time.sleep(0.05)
                job = owner.client.get_job(job.id)
            assert job.status == "completed"
            old_id = job.id
        with pytest.raises(api.CoreLaunchError):
            _ = owner.client
        runtime = tmp_path / "artifacts/.runtime"
        assert not runtime.exists() or not any(runtime.iterdir())
    assert clients[0] is not clients[1]
