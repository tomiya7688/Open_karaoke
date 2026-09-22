"""CER/WER evaluator. Japanese CER is primary; WER uses whitespace, not morphology."""

import argparse
import json
import unicodedata
from pathlib import Path


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(c for c in text if not unicodedata.category(c).startswith("P"))


def edit_distance(reference, hypothesis) -> int:
    if len(reference) * len(hypothesis) > 4_000_000:
        raise ValueError("Split the evaluation into bounded utterances (<=4M edit cells)")
    previous = list(range(len(hypothesis) + 1))
    for row, expected in enumerate(reference, 1):
        current = [row]
        for column, actual in enumerate(hypothesis, 1):
            current.append(
                min(
                    previous[column] + 1,
                    current[-1] + 1,
                    previous[column - 1] + (expected != actual),
                )
            )
        previous = current
    return previous[-1]


def _rate(errors: int, count: int) -> float | None:
    return errors / count if count else (0.0 if errors == 0 else None)


def evaluate(pairs: list[dict], *, normalized: bool = True, max_cer: float | None = None) -> dict:
    if not pairs or len(pairs) > 1000:
        raise ValueError("Expected 1..1000 evaluation pairs")
    if max_cer is not None and not 0 <= max_cer <= 10:
        raise ValueError("max_cer must be finite and in 0..10")
    results = []
    ids = set()
    totals = {"char_errors": 0, "reference_chars": 0, "word_errors": 0, "reference_words": 0}
    empty_reference_insertions = 0
    for index, pair in enumerate(pairs):
        identifier = pair.get("id", str(index))
        if not isinstance(identifier, str) or identifier in ids:
            raise ValueError("Evaluation IDs must be unique strings")
        ids.add(identifier)
        reference, hypothesis = pair.get("reference"), pair.get("hypothesis")
        if not isinstance(reference, str) or not isinstance(hypothesis, str):
            raise ValueError("Each pair needs reference and hypothesis strings")
        if max(len(reference), len(hypothesis)) > 20_000:
            raise ValueError("Evaluation utterance exceeds 20k characters")
        if normalized:
            reference, hypothesis = normalize(reference), normalize(hypothesis)
        ref_chars = "".join(reference.split())
        hyp_chars = "".join(hypothesis.split())
        counts = {
            "char_errors": edit_distance(ref_chars, hyp_chars),
            "reference_chars": len(ref_chars),
            "word_errors": edit_distance(reference.split(), hypothesis.split()),
            "reference_words": len(reference.split()),
        }
        for key, value in counts.items():
            totals[key] += value
        if not ref_chars:
            empty_reference_insertions += len(hyp_chars)
        results.append(
            {
                "id": identifier,
                **counts,
                "cer": _rate(counts["char_errors"], counts["reference_chars"]),
                "wer": _rate(counts["word_errors"], counts["reference_words"]),
            }
        )
    cer = _rate(totals["char_errors"], totals["reference_chars"])
    return {
        "evaluator": "lyric-transcription",
        "version": 1,
        "normalization": "NFKC+casefold+remove-punctuation" if normalized else "none",
        "wer_tokenizer": "whitespace",
        "cer_whitespace": "removed",
        "totals": totals,
        "cer": cer,
        "wer": _rate(totals["word_errors"], totals["reference_words"]),
        "empty_reference_insertions": empty_reference_insertions,
        "utterances": results,
        "quality_gate_applied": max_cer is not None,
        "max_cer": max_cer,
        "passed": None
        if max_cer is None
        else (cer is not None and cer <= max_cer and empty_reference_insertions == 0),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pairs", type=Path, help="JSON array of id/reference/hypothesis records")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exact", action="store_true", help="Do not normalize Unicode/punctuation")
    parser.add_argument("--max-cer", type=float)
    args = parser.parse_args()
    try:
        with args.pairs.open("rb") as source:
            data = source.read(4_194_305)
        if len(data) > 4_194_304:
            raise ValueError("Evaluation file exceeds 4 MiB")
        pairs = json.loads(data)
        if not isinstance(pairs, list) or any(not isinstance(p, dict) for p in pairs):
            raise ValueError("Expected a JSON array of evaluation pairs")
        report = evaluate(pairs, normalized=not args.exact, max_cer=args.max_cer)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    except (OSError, UnicodeError, ValueError) as error:
        parser.error(str(error))
    if report["passed"] is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
