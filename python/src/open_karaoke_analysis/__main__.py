"""Managed entry point. Bind loopback before publishing the readiness handshake."""

import argparse
import asyncio
import os
import socket
from pathlib import Path

import uvicorn

from .adapters import atomic_json
from .contracts import PROTOCOL_VERSION
from .service import create_app


async def serve(root: Path, ready_file: Path, token: str, port: int = 0) -> None:
    app = create_app(root, token)
    config = uvicorn.Config(app, log_level="warning", access_log=False, timeout_graceful_shutdown=5)
    server = uvicorn.Server(config)
    ready_file.parent.mkdir(parents=True, exist_ok=True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", port))
        listener.listen(128)
        listener.setblocking(False)
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Analysis server exited before becoming ready")
                await asyncio.sleep(0.01)
            atomic_json(
                ready_file,
                {
                    "port": listener.getsockname()[1],
                    "pid": os.getpid(),
                    "instance_id": str(app.state.instance_id),
                    "protocol_version": PROTOCOL_VERSION,
                },
            )
            while not task.done():
                if app.state.stop_requested.is_set():
                    server.should_exit = True
                    break
                await asyncio.sleep(0.05)
            await task
        finally:
            server.should_exit = True
            if not task.done():
                await task
            ready_file.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Open Karaoke local analysis service")
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    token = os.environ.get("OPEN_KARAOKE_ANALYSIS_TOKEN", "")
    if len(token) < 32:
        parser.error("OPEN_KARAOKE_ANALYSIS_TOKEN must contain at least 32 characters")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    asyncio.run(serve(args.artifact_root, args.ready_file, token, args.port))


if __name__ == "__main__":
    main()
