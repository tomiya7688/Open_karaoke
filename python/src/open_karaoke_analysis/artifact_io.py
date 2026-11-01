"""Shared artifact hashing and JSON reading. Adapters pass in their own policy."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from .adapters import resolve_artifact
from .contracts import ServiceError

CHUNK_BYTES = 1024 * 1024


def _reject_constant(value: str):
    raise ValueError(f"Non-finite JSON constant: {value}")


def hash_file(
    path: Path,
    *,
    checkpoint: Callable[[], None],
    max_bytes: int | None = None,
    oversize_error: Callable[[], Exception] | None = None,
    final_checkpoint: bool = False,
) -> tuple[str, int]:
    """Return (sha256 hex, size). Calls checkpoint per chunk so cancellation is honoured.

    If max_bytes is set, hashing stops with oversize_error() as soon as it is exceeded.
    I/O errors propagate unchanged; the caller owns error-code policy.
    """
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while block := stream.read(CHUNK_BYTES):
            checkpoint()
            size += len(block)
            if max_bytes is not None and size > max_bytes:
                if oversize_error is None:
                    raise ServiceError("size_limit", "File exceeds its size limit")
                raise oversize_error()
            digest.update(block)
    if final_checkpoint:
        checkpoint()
    return digest.hexdigest(), size


def read_json_object(
    root: Path,
    relative: str,
    *,
    max_bytes: int,
    error_code: str,
    size_message: str = "Artifact exceeds size limit",
    invalid_message: str = "Artifact is not valid JSON",
    root_message: str = "Artifact root must be an object",
) -> dict:
    """Read a JSON object from a confined artifact path.

    Rejects traversal/symlinks (resolve_artifact), oversize files, non-UTF-8 text,
    NaN/Infinity (including overflowing literals) and non-object roots.
    """
    path = resolve_artifact(root, relative, must_exist=True)
    try:
        if path.stat().st_size > max_bytes:
            raise ServiceError(error_code, size_message)
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ServiceError(error_code, size_message)
        value = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
        json.dumps(value, allow_nan=False)  # overflowing literals such as 1e999
    except ServiceError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        raise ServiceError(error_code, invalid_message) from error
    if not isinstance(value, dict):
        raise ServiceError(error_code, root_message)
    return value
