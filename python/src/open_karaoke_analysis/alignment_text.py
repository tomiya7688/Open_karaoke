"""Bounded reference import and monotonic text mapping. Text is never silently replaced."""

import hashlib
import json
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .adapters import AnalysisContext, resolve_artifact
from .contracts import ServiceError

RATE = 48_000
MAX_DURATION = 3600 * RATE
MAX_TEXT_CELLS = 16_000_000


class InputSegment(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    id: str = Field(min_length=1, max_length=128)
    text: str = Field(max_length=4096)
    start_sample: int = Field(ge=0, le=MAX_DURATION)
    end_sample: int = Field(gt=0, le=MAX_DURATION)
    text_confidence: float | None = Field(default=None, ge=0, le=1)
    timing_confidence: float | None = Field(default=None, ge=0, le=1)
    source: str | None = None


class InputLyrics(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    format_version: Literal[1]
    analysis_version: int = Field(ge=1, le=4_294_967_295)
    segments: list[InputSegment] = Field(max_length=2048)


def read_artifact(context: AnalysisContext, relative: str, limit: int) -> bytes:
    context.checkpoint()
    path = resolve_artifact(context.root, relative, must_exist=True)
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ServiceError("input_too_large", "Alignment input exceeds its size limit")
    context.checkpoint()
    return data


def reject_constant(value: str):
    raise ValueError(f"Non-finite JSON constant: {value}")


def read_lyrics(context: AnalysisContext, relative: str) -> tuple[dict, str]:
    data = read_artifact(context, relative, 4_194_304)
    try:
        raw = json.loads(data, parse_constant=reject_constant)
        if not isinstance(raw, dict) or type(raw.get("format_version")) is not int:
            raise ValueError("Invalid version")
        parsed = InputLyrics.model_validate(raw)
    except (ValueError, ValidationError, UnicodeError, RecursionError) as error:
        raise ServiceError("invalid_lyrics", "Expected a version-1 LyricsDocument") from error
    ids, previous, total = set(), -1, 0
    for segment in parsed.segments:
        total += len(segment.text)
        if (
            not segment.id.strip()
            or segment.id in ids
            or segment.start_sample >= segment.end_sample
            or segment.start_sample < previous
            or total > 16_000
        ):
            raise ServiceError("invalid_lyrics", "Invalid, duplicate or unordered lyric segments")
        previous = segment.start_sample
        ids.add(segment.id)
    # Keep original fields (including future evidence extensions) in the audit snapshot.
    return raw, hashlib.sha256(data).hexdigest()


def read_reference(context: AnalysisContext, relative: str | None) -> dict | None:
    if relative is None:
        return None
    data = read_artifact(context, relative, 65_536)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as error:
        raise ServiceError("invalid_reference", "Imported lyrics must be UTF-8 plain text") from error
    if any(ord(char) < 32 and char not in "\r\n\t" for char in text):
        raise ServiceError("invalid_reference", "Imported lyrics contain control characters")
    lines = [
        {"id": f"reference-{index + 1:06d}", "text": line, "line_number": index + 1}
        for index, line in enumerate(text.splitlines())
        if line.strip()
    ]
    if len(lines) > 2048:
        raise ServiceError("invalid_reference", "Too many imported lyric lines")
    return {"artifact": relative, "sha256": hashlib.sha256(data).hexdigest(),
            "text": text, "lines": lines, "authoritative": False}


def normalized_chars(text: str) -> list[dict]:
    """NFKC/casefold with original codepoint offsets, including combining/halfwidth kana."""
    clusters: list[tuple[int, int]] = []
    for index, char in enumerate(text):
        if clusters and (unicodedata.combining(char) or char in "\uff9e\uff9f"):
            clusters[-1] = (clusters[-1][0], index + 1)
        else:
            clusters.append((index, index + 1))
    chars = []
    for start, end in clusters:
        for char in unicodedata.normalize("NFKC", text[start:end]).casefold():
            if char.isspace() or unicodedata.category(char).startswith("P"):
                continue
            chars.append({"char": char, "char_start": start, "char_end": end,
                          "normalized_index": len(chars)})
    return chars


def map_reference(segments: list[dict], reference: dict | None, checkpoint) -> dict | None:
    """Map equal normalized characters only; unequal spans keep both source texts."""
    if reference is None:
        return None
    asr = [{**char, "segment_id": segment["id"]}
           for segment in segments for char in normalized_chars(segment["text"])]
    ref = [{**char, "line_id": line["id"]}
           for line in reference["lines"] for char in normalized_chars(line["text"])]
    if len(ref) > 16_000 or len(asr) * len(ref) > MAX_TEXT_CELLS:
        raise ServiceError("mapping_too_large", "Reference mapping exceeds the bounded text budget")
    checkpoint()
    a = "".join(char["char"] for char in asr)
    b = "".join(char["char"] for char in ref)
    operations, matches = [], []
    for tag, a0, a1, b0, b1 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        checkpoint()
        operations.append({"operation": tag, "asr_range": [a0, a1],
                           "reference_range": [b0, b1], "asr_text": a[a0:a1],
                           "reference_text": b[b0:b1]})
        if tag == "equal":
            matches.extend({"asr_index": i, "reference_index": j}
                           for i, j in zip(range(a0, a1), range(b0, b1), strict=True))
    return {"normalization": "NFKC-casefold-ignore-punctuation-and-whitespace-v1",
            "asr_characters": asr, "reference_characters": ref,
            "operations": operations, "matches": matches,
            "reference_is_truth": False, "matching_is_correctness_probability": False}


def reference_timeline(reference: dict, mapping: dict, lines: list[dict]) -> list[dict]:
    """Project observed character times. Never interpolate missing/alternate-version lyrics."""
    aligned = {(line["id"], c["normalized_index"]): c
               for line in lines for c in line["characters"]}
    match = {item["reference_index"]: item["asr_index"] for item in mapping["matches"]}
    results = []
    texts = ["".join(c["char"] for c in normalized_chars(line["text"]))
             for line in reference["lines"]]
    counts = Counter(texts)
    grouped: dict[str, list[tuple[int, dict]]] = {}
    for index, char in enumerate(mapping["reference_characters"]):
        grouped.setdefault(char["line_id"], []).append((index, char))
    for line, normalized in zip(reference["lines"], texts, strict=True):
        units, indices = [], []
        for index, char in grouped.get(line["id"], []):
            source_index = match.get(index)
            source = mapping["asr_characters"][source_index] if source_index is not None else None
            observed = (aligned.get((source["segment_id"], source["normalized_index"])) if source else None)
            timed = observed is not None and observed["status"] == "aligned"
            units.append({**char, "source": source, "status": "matched" if timed else "unaligned",
                          "start_sample": observed["start_sample"] if timed else None,
                          "end_sample": observed["end_sample"] if timed else None})
            if source_index is not None:
                indices.append(source_index)
        complete = bool(units) and all(unit["status"] == "matched" for unit in units)
        contiguous = bool(indices) and indices == list(range(indices[0], indices[-1] + 1))
        flags = []
        if counts[normalized] > 1:
            flags.append("repeated_reference_review")
        if not complete or not contiguous:
            flags.append("reference_mismatch_or_unaligned")
        results.append({**line, "status": "matched" if complete and contiguous else "unaligned",
                        "start_sample": units[0]["start_sample"] if complete and contiguous else None,
                        "end_sample": units[-1]["end_sample"] if complete and contiguous else None,
                        "characters": units, "flags": flags, "timing_confidence": None})
    return results
