from threading import Event

import pytest

from open_karaoke_analysis.adapters import AnalysisContext
from open_karaoke_analysis.alignment_text import map_reference, read_reference, reference_timeline
from open_karaoke_analysis.contracts import ServiceError


def setup(tmp_path, text):
    (tmp_path / "reference.txt").write_bytes(text.encode("utf-8"))
    context = AnalysisContext(tmp_path, "jobs/test/ref", None, Event(), lambda *_: None)
    return context, read_reference(context, "reference.txt")


@pytest.mark.parametrize("asr,reference,tag", [
    ("青い空", "青い海", "replace"), ("青い空", "青い空へ", "insert"),
    ("青い空へ", "青い空", "delete"), ("Ａ Ｂ", "ab", "equal"),
    ("", "新しい歌", "insert"), ("歌", "", "delete"),
])
def test_missing_extra_alternate_versions(tmp_path, asr, reference, tag):
    context, imported = setup(tmp_path, reference)
    mapping = map_reference([{"id": "asr-1", "text": asr}], imported, context.checkpoint)
    assert tag in [op["operation"] for op in mapping["operations"]]
    assert imported["text"] == reference
    assert mapping["reference_is_truth"] is False
    indices = [item["asr_index"] for item in mapping["matches"]]
    assert indices == sorted(set(indices))


def test_repeated_chorus_not_collapsed(tmp_path):
    context, imported = setup(tmp_path, "あい\nあい\n")
    segments = [{"id": "asr-1", "text": "あい"}, {"id": "asr-2", "text": "あい"}]
    mapping = map_reference(segments, imported, context.checkpoint)
    assert len(mapping["matches"]) == 4
    result = reference_timeline(imported, mapping, [
        {"id": "asr-1", "characters": []}, {"id": "asr-2", "characters": []}])
    assert len(result) == 2
    assert all("repeated_reference_review" in line["flags"] for line in result)
    assert all(line["start_sample"] is None for line in result)


def test_mapping_budget_not_unbounded(tmp_path):
    context, imported = setup(tmp_path, "あ" * 5000)
    with pytest.raises(ServiceError, match="budget"):
        map_reference([{"id": "asr", "text": "あ" * 5000}], imported, context.checkpoint)


@pytest.mark.parametrize("content", [b"\xff\xfe", b"text\x00", b"a" * 65537])
def test_invalid_import(tmp_path, content):
    (tmp_path / "reference.txt").write_bytes(content)
    context = AnalysisContext(tmp_path, "jobs/test/ref", None, Event(), lambda *_: None)
    with pytest.raises(ServiceError):
        read_reference(context, "reference.txt")


def test_bom_crlf_blank_lines_and_path_rejection(tmp_path):
    context, imported = setup(tmp_path, "\ufeffあ\r\n\r\nい\r\n")
    assert [line["line_number"] for line in imported["lines"]] == [1, 3]
    with pytest.raises(ServiceError):
        read_reference(context, "../secret.txt")
