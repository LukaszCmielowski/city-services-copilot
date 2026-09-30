#!/usr/bin/env python3
"""Validate the prepared City Services Copilot inputs before they reach a pipeline."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

RISK_COLUMNS = {"service_type", "neighborhood", "intake_channel", "priority", "opened_at", "sla_days", "sla_miss"}
FORECAST_COLUMNS = {"timestamp", "item_id", "target"}


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def check_csv(path: Path, required: set[str], label: str) -> None:
    if not path.is_file():
        fail(f"{label} file does not exist: {path}")
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                fail(f"{label} needs a UTF-8 CSV header row")
            missing = required - set(reader.fieldnames)
            if missing:
                fail(f"{label} is missing required columns: {', '.join(sorted(missing))}")
            count = sum(1 for _ in reader)
    except UnicodeDecodeError:
        fail(f"{label} is not UTF-8 encoded: {path}")
    if not count:
        fail(f"{label} contains no data rows: {path}")
    print(f"OK: {label} has {count:,} rows and required columns")


def check_evaluation(path: Path, documents: Path, document_prefix: str) -> None:
    if not path.is_file():
        fail(f"AutoRAG evaluation file does not exist: {path}")
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        fail(f"AutoRAG evaluation file is not valid UTF-8 JSON: {error}")
    if not isinstance(entries, list) or not entries:
        fail("AutoRAG evaluation file must be a non-empty JSON array")
    normalized_prefix = document_prefix.strip().strip("/")
    known_documents = (
        {f"{normalized_prefix}/{item.name}" for item in documents.iterdir() if item.is_file()}
        if documents.is_dir()
        else set()
    )
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or not all(entry.get(key) for key in ("question", "correct_answers", "correct_answer_document_keys")):
            fail(f"evaluation entry {index} needs question, correct_answers, and correct_answer_document_keys")
        referenced_keys = set(entry["correct_answer_document_keys"])
        missing = referenced_keys - known_documents
        if missing:
            fail(f"evaluation entry {index} references document key(s) not found under {document_prefix}: {', '.join(sorted(missing))}")
    print(f"OK: AutoRAG evaluation has {len(entries)} questions and valid document keys")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--risk", type=Path, default=Path("data/prepared/resolution-risk.csv"), help="prepared resolution-risk.csv")
    parser.add_argument("--forecast", type=Path, default=Path("data/prepared/service-demand-daily.csv"), help="prepared service-demand-daily.csv")
    parser.add_argument("--evaluation", type=Path, default=Path("data/autorag-evaluation.json"), help="AutoRAG evaluation JSON")
    parser.add_argument("--documents", type=Path, default=Path("data/guidance"), help="folder containing the guidance files")
    parser.add_argument("--document-prefix", default="city-services-copilot/autorag/guidance", help="S3 prefix used when uploading the guidance folder")
    args = parser.parse_args()
    check_csv(args.risk, RISK_COLUMNS, "resolution-risk")
    check_csv(args.forecast, FORECAST_COLUMNS, "service-demand-daily")
    check_evaluation(args.evaluation, args.documents, args.document_prefix)
    print("Ready for S3 upload and pipeline configuration.")


if __name__ == "__main__":
    main()
