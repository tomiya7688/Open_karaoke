"""Cross-adapter contract tests for shared artifact hashing and JSON reading."""

import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from open_karaoke_analysis import alignment_backend, song, stem_backend, whisper_backend
from open_karaoke_analysis.artifact_io import hash_file, read_json_object
from open_karaoke_analysis.contracts import ServiceError
from open_karaoke_analysis.notes import _read_json as notes_read_json
from open_karaoke_analysis.vocal_event_features import read_json_artifact, sha256_file


class Cancelled(Exception):
    pass


def make_context(root, cancel_after=None):
    calls = {"n": 0}

    def checkpoint():
        calls["n"] += 1
        if cancel_after is not None and calls["n"] > cancel_after:
            raise Cancelled

    return SimpleNamespace(root=root, checkpoint=checkpoint, calls=calls)


def json_readers(root):
    ctx = make_context(root)
    return {
        "notes": lambda rel: notes_read_json(ctx, rel, max_bytes=1024, error_code="notes_bad"),
        "vocal": lambda rel: read_json_artifact(ctx, rel, max_bytes=1024, error_code="vocal_bad"),
        "song": lambda rel: song._json(ctx, rel, 1024),
        "shared": lambda rel: read_json_object(root, rel, max_bytes=1024, error_code="x"),
    }


CODES = {
    "notes": "notes_bad",
    "vocal": "vocal_bad",
    "song": "invalid_song_artifact",
    "shared": "x",
}


@pytest.mark.parametrize("name", list(CODES))
def test_json_valid_object_is_read(tmp_path, name):
    (tmp_path / "a.json").write_text('{"v": 1}', encoding="utf-8")
    assert json_readers(tmp_path)[name]("a.json") == {"v": 1}


@pytest.mark.parametrize("name", list(CODES))
@pytest.mark.parametrize(
    "payload",
    [
        b'{"v": NaN}',
        b'{"v": Infinity}',
        b'{"v": -Infinity}',
        b'{"v": 1e999}',
        b"[1]",
        b"not json",
        b'{"v": "\xff"}',
        b"{" + b'"a":1,' * 400 + b'"b":2}',
    ],
)
def test_json_invalid_or_oversize_is_rejected_with_role_code(tmp_path, name, payload):
    (tmp_path / "a.json").write_bytes(payload)
    with pytest.raises(ServiceError) as caught:
        json_readers(tmp_path)[name]("a.json")
    assert caught.value.detail.code == CODES[name]


@pytest.mark.parametrize("name", list(CODES))
def test_json_path_escape_and_symlink_are_rejected(tmp_path, name):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.json").write_text("{}", encoding="utf-8")
    reader = json_readers(root)[name]
    for bad in ("../outside.json", "/etc/passwd", "C:/x.json", "a\\b.json", ""):
        with pytest.raises(ServiceError) as caught:
            reader(bad)
        assert caught.value.detail.code == "invalid_path"
    link = root / "link.json"
    try:
        os.symlink(tmp_path / "outside.json", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(ServiceError) as caught:
        reader("link.json")
    assert caught.value.detail.code == "invalid_path"


@pytest.mark.parametrize("name", list(CODES))
def test_json_missing_file_is_input_not_found(tmp_path, name):
    with pytest.raises(ServiceError) as caught:
        json_readers(tmp_path)[name]("missing.json")
    assert caught.value.detail.code == "input_not_found"


def test_role_specific_messages_are_preserved(tmp_path):
    (tmp_path / "a.json").write_text("[]", encoding="utf-8")
    ctx = make_context(tmp_path)
    with pytest.raises(ServiceError, match="Evidence artifact root"):
        read_json_artifact(ctx, "a.json", max_bytes=10, error_code="c")
    with pytest.raises(ServiceError, match="Artifact root must be an object"):
        notes_read_json(ctx, "a.json", max_bytes=10, error_code="c")
    with pytest.raises(ServiceError, match="Invalid or oversized stage JSON"):
        song._json(ctx, "a.json", 10)


HASHERS = {
    "stem": lambda ctx, p: stem_backend.file_sha256(p, ctx),
    "whisper": lambda ctx, p: whisper_backend.sha256_file(p, ctx),
    "alignment": lambda ctx, p: alignment_backend.digest_file(p, ctx, 10**9),
    "vocal": lambda ctx, p: sha256_file(ctx, p),
    "song": lambda ctx, p: song._digest(p, ctx)[0],
}


@pytest.mark.parametrize("name", list(HASHERS))
def test_every_adapter_hash_matches_hashlib(tmp_path, name):
    data = os.urandom(2 * 1024 * 1024 + 17)
    path = tmp_path / "f.bin"
    path.write_bytes(data)
    assert HASHERS[name](make_context(tmp_path), path) == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("name", list(HASHERS))
def test_every_adapter_hash_honours_cancellation(tmp_path, name):
    path = tmp_path / "f.bin"
    path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(Cancelled):
        HASHERS[name](make_context(tmp_path, cancel_after=1), path)


def test_final_checkpoint_only_where_requested(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"abc")
    for name in ("stem", "whisper"):
        ctx = make_context(tmp_path)
        HASHERS[name](ctx, path)
        assert ctx.calls["n"] == 2  # one chunk plus the final checkpoint
    ctx = make_context(tmp_path)
    HASHERS["vocal"](ctx, path)
    assert ctx.calls["n"] == 1


def test_song_digest_enforces_size_limit_with_role_code(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"x" * 100)
    with pytest.raises(ServiceError) as caught:
        song._digest(path, make_context(tmp_path), limit=99)
    assert caught.value.detail.code == "song_size_limit"
    assert song._digest(path, make_context(tmp_path), limit=100)[1] == 100


def test_alignment_digest_keeps_integrity_policy(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"x" * 10)
    ctx = make_context(tmp_path)
    for bad in (tmp_path / "missing.bin", tmp_path):
        with pytest.raises(ServiceError) as caught:
            alignment_backend.digest_file(bad, ctx, 100)
        assert caught.value.detail.code == "model_integrity"
    with pytest.raises(ServiceError) as caught:
        alignment_backend.digest_file(path, ctx, 9)
    assert caught.value.detail.code == "model_integrity"


def test_vocal_sha256_maps_io_errors_to_artifact_io(tmp_path):
    with pytest.raises(ServiceError) as caught:
        sha256_file(make_context(tmp_path), tmp_path / "missing.bin")
    assert caught.value.detail.code == "artifact_io"


def test_hash_file_default_oversize_error_and_json_dump_roundtrip(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"x" * 5)
    with pytest.raises(ServiceError) as caught:
        hash_file(path, checkpoint=lambda: None, max_bytes=4)
    assert caught.value.detail.code == "size_limit"
    assert hash_file(path, checkpoint=lambda: None) == (hashlib.sha256(b"x" * 5).hexdigest(), 5)
    (tmp_path / "j.json").write_text(json.dumps({"a": [1, 2]}), encoding="utf-8")
    assert read_json_object(tmp_path, "j.json", max_bytes=100, error_code="e") == {"a": [1, 2]}


def test_alignment_digest_enforces_limit_while_hashing(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"x" * 10)
    # Size grows after the pre-check: the pre-check is made to see size 0.
    ctx = make_context(tmp_path)
    fake = SimpleNamespace(
        is_file=lambda: True,
        stat=lambda: SimpleNamespace(st_size=0),
        open=path.open,
    )
    with pytest.raises(ServiceError) as caught:
        alignment_backend.digest_file(fake, ctx, 9)
    assert caught.value.detail.code == "model_integrity"
