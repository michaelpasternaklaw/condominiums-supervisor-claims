#!/usr/bin/env python3
"""Consistency checks for the locally generated precedent index."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def check(output: Path) -> dict:
    index = csv_rows(output / "precedents_index.csv")
    mentions = csv_rows(output / "precedent_citations.csv")
    payload = json.loads((output / "precedents_index.json").read_text(encoding="utf-8"))
    focus = json.loads((output / "precedents_focus.json").read_text(encoding="utf-8"))
    keys = {row["מפתח אסמכתה"] for row in index}
    assert len(keys) == len(index), "duplicate precedent keys"
    assert all(row["citation_key"] in keys for row in mentions), "orphan citation mention"
    assert all(row["proceeding_type"] != "מספר ללא סוג הליך" for row in mentions), "low-confidence bare number leaked into default index"
    assert payload["counts"]["precedents"] == len(index)
    assert payload["counts"]["mentions"] == len(mentions)
    focus_keys = [row["key"] for row in focus["precedents"]]
    assert len(focus_keys) == len(set(focus_keys)), "duplicate focused precedent keys"
    assert all(row["importance"] in {"מרכזית", "חשובה"} for row in focus["precedents"])
    assert focus["counts"]["precedents"] == len(focus_keys)
    for row in focus["precedents"]:
        source_ids = [item["sourceId"] for item in row["citingDecisionEvidence"]]
        assert len(source_ids) == len(set(source_ids)), f"duplicate citing decision: {row['key']}"
        assert len(source_ids) == row["citingDecisions"], f"missing citing decision evidence: {row['key']}"

    by_key: dict[str, set[str]] = {}
    for row in mentions:
        by_key.setdefault(row["citation_key"], set()).add(row["source_document_id"])
    for row in index:
        assert int(row["מספר החלטות מפקח מצטטות"]) == len(by_key[row["מפתח אסמכתה"]])
        assert row["קטע לדוגמה"], f"missing evidence snippet: {row['מפתח אסמכתה']}"
        assert int(row["עמוד לדוגמה"]) >= 1

    kershin = next((row for row in index if row["סוג הליך"] == 'רע"א' and row["מספר הליך"] == "7828/06"), None)
    assert kershin, "רע\"א 7828/06 was not extracted"
    assert int(kershin["מספר החלטות מפקח מצטטות"]) == 2
    assert "קרשין" in kershin["שם הליך שחולץ"]
    assert kershin["מפתח אסמכתה"] in focus_keys, "קרשין missing from focused public index"

    db = sqlite3.connect(output / "precedents.sqlite")
    db_precedents = db.execute("SELECT count(*) FROM precedents").fetchone()[0]
    db_mentions = db.execute("SELECT count(*) FROM citation_mentions").fetchone()[0]
    fts_hits = db.execute("SELECT count(*) FROM precedent_fts WHERE precedent_fts MATCH 'קרשין'").fetchone()[0]
    db.close()
    assert db_precedents == len(index)
    assert db_mentions == len(mentions)
    assert fts_hits >= 1
    return {
        "status": "passed",
        "precedents": len(index),
        "mentions": len(mentions),
        "kershinCitingDecisions": int(kershin["מספר החלטות מפקח מצטטות"]),
        "sqliteFtsKershinHits": fts_hits,
        "focusedImportantPrecedents": len(focus_keys),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
