"""Evaluator-owned Core lifetime; credentials travel only through the bootstrap pipe."""

import json
import os
import queue
import secrets
import signal
import subprocess
import threading
from pathlib import Path
from uuid import UUID

from .core_job_client import CoreJobClient


class CoreLaunchError(RuntimeError):
    """{
    責務: [起動・終了の失敗を子プロセスの出力や秘密なしで伝える]
    フィールド: []
    }"""


def _pairs(pairs):
    """{
    責務: [ready JSONの重複キーを拒否する]
    処理: [1: キーの重複を検査, 2: objectを返す]
    引数: [pairs: decoderのキーと値]
    戻り値: [重複のないdict。不正はValueError]
    }"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _drain(stream):
    """{
    責務: [子の出力を保存・表示せず固定メモリで排出する]
    処理: [1: EOFまで小さいblockで読む]
    引数: [stream: 所有するstdoutまたはstderr]
    戻り値: []
    }"""
    try:
        while stream.read(4096):
            pass
    except (OSError, ValueError):
        pass


class OwnedCore:
    """{
    責務: [Evaluatorが起動したCore一つの認証接続と生存期間を所有する]
    フィールド: [_process: 起動した子, _threads: 出力の排出, _client: 照合済み接続,
      _closed: 接続失効, _stopped: 停止確認済み, _shutdown_timeout: 正常終了待ち秒]
    }"""

    def __init__(self, process, shutdown_timeout):
        """{
        責務: [startが生成した所有資源を初期化する]
        処理: [1: 子と終了予算を保持, 2: 未接続状態にする]
        引数: [process: 所有するPopen, shutdown_timeout: EOF後の待機秒]
        戻り値: []
        補足: [利用側はstartを使う。任意PIDへattachするAPIではない]
        }"""
        self._process, self._shutdown_timeout = process, shutdown_timeout
        self._threads, self._client, self._closed = [], None, False
        self._stopped = False

    @classmethod
    def start(
        cls,
        executable,
        data_directory,
        python_executable,
        *,
        startup_timeout=45,
        shutdown_timeout=8,
    ):
        """{
        責務: [新しい能力でCoreを起動しreadyとsessionを期限内に照合する]
        処理: [1: パスと予算を検証, 2: shellなしで起動, 3: tokenをstdinへ一度送る,
          4: readyとsession照合を待つ。失敗時は所有子を終了する]
        引数: [executable: Core絶対パス, data_directory: 専用データの絶対パス,
          python_executable: worker Python絶対パス, startup_timeout: 起動待ち0超120秒以下,
          shutdown_timeout: EOF後の待機0超30秒以下]
        戻り値: [接続済みOwnedCore。不正入力はValueError、起動失敗はCoreLaunchError]
        }"""
        try:
            paths = [Path(value) for value in (executable, data_directory, python_executable)]
        except TypeError:
            raise ValueError("Expected absolute Core, data directory and Python paths") from None
        if not all(p.is_absolute() for p in paths) or not all(paths[i].is_file() for i in (0, 2)):
            raise ValueError("Expected absolute Core, data directory and Python paths")
        for value, maximum in ((startup_timeout, 120), (shutdown_timeout, 30)):
            if type(value) not in (int, float) or not 0 < value <= maximum:
                raise ValueError("Invalid Core lifetime timeout")
        core, data, python = paths
        token = secrets.token_hex(32)
        env = os.environ.copy()
        env.update(
            OPEN_KARAOKE_BIND="127.0.0.1:0",
            OPEN_KARAOKE_DATA_DIR=str(data),
            OPEN_KARAOKE_PYTHON=str(python),
        )
        try:
            data.mkdir(parents=True, exist_ok=True)
            process = subprocess.Popen(
                [str(core)],
                cwd=core.parent,
                env=env,
                shell=False,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=os.name != "nt",
            )
        except OSError:
            raise CoreLaunchError("Could not launch Core") from None
        owner = cls(process, shutdown_timeout)
        result = queue.Queue(maxsize=1)
        try:
            for target, args in ((_drain, (process.stderr,)), (owner._connect, (token, result))):
                thread = threading.Thread(target=target, args=args, daemon=True)
                thread.start()
                owner._threads.append(thread)
            client = result.get(timeout=startup_timeout)
            if client is None:
                raise CoreLaunchError("Core bootstrap or session verification failed")
            owner._client = client
            return owner
        except queue.Empty:
            raise CoreLaunchError("Core startup deadline exceeded") from None
        except (OSError, RuntimeError):
            raise CoreLaunchError("Core bootstrap or session verification failed") from None
        finally:
            if owner._client is None:
                owner.close()

    def _connect(self, token, result):
        """{
        責務: [起動通信を行い認証済み接続だけを待機者へ返す]
        処理: [1: bootstrap送信, 2: 8KiB以内のreadyを検証, 3: session照合,
          4: 結果を一度通知してstdoutを排出]
        引数: [token: メモリ内の新規能力, result: 起動結果の有限queue]
        戻り値: []
        }"""
        client = None
        try:
            payload = (token + "\n").encode("ascii")
            if self._process.stdin.write(payload) != len(payload):
                raise ValueError()
            self._process.stdin.flush()
            raw = self._process.stdout.readline(8193)
            if len(raw) > 8192 or not raw.endswith(b"\n"):
                raise ValueError()
            ready = json.loads(raw, object_pairs_hook=_pairs)
            if (
                not isinstance(ready, dict)
                or ready.get("event") != "core_ready"
                or type(ready.get("api_version")) is not int
                or ready["api_version"] != 1
                or type(ready.get("port")) is not int
                or not 1 <= ready["port"] <= 65535
                or not isinstance(ready.get("instance_id"), str)
                or str(UUID(ready["instance_id"])) != ready["instance_id"]
            ):
                raise ValueError()
            if self._closed:
                raise ValueError()
            client = CoreJobClient(f"http://127.0.0.1:{ready['port']}", token, ready["instance_id"])
            client.verify_session()
        except (OSError, ValueError, RuntimeError, RecursionError):
            client = None
        finally:
            result.put(client)
            _drain(self._process.stdout)

    @property
    def client(self):
        """{
        責務: [生存する所有Coreの接続だけを公開する]
        処理: [1: lifetimeと接続を検査, 2: clientを返す]
        引数: []
        戻り値: [CoreJobClient。終了済みならCoreLaunchError]
        }"""
        if self._closed or self._client is None or self._process.poll() is not None:
            raise CoreLaunchError("Owned Core is not running")
        return self._client

    def close(self):
        """{
        責務: [EOFで正常終了を依頼し期限超過時は所有プロセスツリーだけを終了する]
        処理: [1: 一度だけstdinを閉じる, 2: 有限時間待つ, 3: 超過時に所有treeを停止,
          4: readerとpipeを片付ける]
        引数: []
        戻り値: [停止を確認できなければCoreLaunchError。startupとは別の終了予算を使う]
        }"""
        if self._stopped:
            return
        self._closed, self._client = True, None
        process = self._process
        shutdown_failure = False
        try:
            try:
                process.stdin.close()
            except OSError:
                pass  # A broken bootstrap pipe must still run wait/tree cleanup.
            try:
                process.wait(timeout=self._shutdown_timeout)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    taskkill = Path(os.environ["SystemRoot"]) / "System32/taskkill.exe"
                    subprocess.run(
                        [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        check=False,
                    )
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        if process.poll() is None:
                            raise
                process.wait(timeout=5)
            self._stopped = True
        except (OSError, KeyError, subprocess.SubprocessError):
            shutdown_failure = True
        finally:
            for thread in self._threads:
                thread.join(timeout=1)
            pipe_failure = False
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    pipe_failure = True
            if pipe_failure:
                self._stopped = False
        if shutdown_failure:
            suffix = "; pipe cleanup also failed" if pipe_failure else ""
            raise CoreLaunchError("Could not confirm owned Core shutdown" + suffix) from None
        if pipe_failure:
            raise CoreLaunchError("Could not close owned Core pipes") from None

    def __enter__(self):
        """{
        責務: [接続済み所有者をwithブロックへ渡す]
        処理: [1: 生存を確認, 2: 自身を返す]
        引数: []
        戻り値: [OwnedCore]
        }"""
        try:
            _ = self.client
        except CoreLaunchError:
            self.close()
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """{
        責務: [利用側の例外時も所有Coreを終了する]
        処理: [1: closeを呼ぶ]
        引数: [exc_type・exc_value・traceback: withの例外情報]
        戻り値: [None。利用側の例外を抑止しない]
        }"""
        self.close()
