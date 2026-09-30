#!/usr/bin/env python3
"""Build narrow, evidence-backed focus classifications without an AI model.

The classifier reads the local SQLite sections created by ``legal_pipeline.py``.
It evaluates short windows around legally meaningful anchors, not whole pages,
and publishes only accepted classifications with page-level evidence.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from legal_pipeline import DEFAULT_DB, REPOSITORY, ROLE_LABELS, clean, normalize


SITE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES = SITE_ROOT / "config/focus_rules.json"
DEFAULT_OUTPUT = REPOSITORY / "focus-public.json"
DEFAULT_REVIEW = REPOSITORY / "focus-review.csv"
DECISIVE_ROLES = {"analysis", "holding", "operative"}


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def compile_rules(path: Path) -> tuple[dict, dict]:
    source = json.loads(path.read_text(encoding="utf-8"))
    compiled = {}
    for key, rule in source["topics"].items():
        compiled[key] = {
            **rule,
            "anchors": [{**item, "regex": re.compile(item["pattern"])} for item in rule["anchors"]],
            "support_regex": [re.compile(pattern) for pattern in rule.get("support", [])],
            "required_anchor_regex": re.compile(rule["required_anchor"]) if rule.get("required_anchor") else None,
            "required_support_regex": [re.compile(pattern) for pattern in rule.get("required_support", [])],
            "negative_regex": [re.compile(pattern) for pattern in rule.get("negative", [])],
        }
    return source, compiled


def short_window(text: str, start: int, end: int, radius: int) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    value = clean(text[left:right])
    return f"{'…' if left else ''}{value}{'…' if right < len(text) else ''}"


def classify_document(sections: list[sqlite3.Row], rules_source: dict, rules: dict) -> list[dict]:
    role_weights = rules_source["role_weights"]
    radius = int(rules_source.get("window_chars", 240))
    max_evidence = int(rules_source.get("max_evidence", 4))
    output = []
    for key, rule in rules.items():
        hits = []
        for section in sections:
            text = section["normalized_text"]
            role = section["role"]
            for anchor in rule["anchors"]:
                for match in anchor["regex"].finditer(text):
                    start = max(0, match.start() - radius)
                    end = min(len(text), match.end() + radius)
                    window = text[start:end]
                    if any(pattern.search(window) for pattern in rule["negative_regex"]):
                        continue
                    support_labels = [pattern.pattern for pattern in rule["support_regex"] if pattern.search(window)]
                    required_support = [pattern.pattern for pattern in rule["required_support_regex"] if pattern.search(window)]
                    role_weight = float(role_weights.get(role, 0.8))
                    score = float(anchor["weight"]) * role_weight + min(2.4, len(support_labels) * 0.8)
                    hits.append({
                        "page": int(section["page_number"]), "role": role, "anchor": anchor["label"],
                        "score": round(score, 2), "support": len(support_labels),
                        "requiredSupport": len(required_support),
                        "requiredAnchor": bool(rule["required_anchor_regex"] and rule["required_anchor_regex"].fullmatch(match.group(0))),
                        "snippet": short_window(text, match.start(), match.end(), radius),
                    })
        if not hits:
            continue
        unique = {}
        for hit in hits:
            identity = (hit["page"], hit["anchor"], hit["snippet"])
            if identity not in unique or hit["score"] > unique[identity]["score"]:
                unique[identity] = hit
        hits = sorted(unique.values(), key=lambda item: (-item["score"], item["page"]))
        evidence_hits = sorted(
            hits,
            key=lambda item: (0 if item["requiredAnchor"] else 1, -item["score"], item["page"]),
        ) if rule["required_anchor_regex"] else hits
        anchor_hits = len(hits)
        support_hits = sum(hit["support"] > 0 for hit in hits)
        required_support_hits = sum(hit["requiredSupport"] > 0 for hit in hits)
        required_anchor_hits = sum(hit["requiredAnchor"] for hit in hits)
        decisive_hits = sum(hit["role"] in DECISIVE_ROLES for hit in hits)
        pages = sorted({hit["page"] for hit in hits})
        score = round(sum(hit["score"] for hit in hits[:8]), 2)
        enough_anchors = anchor_hits >= int(rule["minimum_anchor_hits"])
        decisive_exception = bool(rule.get("single_decisive_allowed") and decisive_hits and anchor_hits >= 1)
        enough_pages = len(pages) >= int(rule.get("minimum_pages", 1))
        decisive_or_dominant = (
            decisive_hits >= int(rule.get("minimum_decisive_hits", 0))
            or score >= float(rule.get("non_decisive_score", 0))
        )
        eligible = (
            score >= float(rule["minimum_score"])
            and support_hits >= int(rule["minimum_support_hits"])
            and required_support_hits >= int(rule.get("minimum_required_support_hits", 0))
            and (not rule["required_anchor_regex"] or required_anchor_hits > 0)
            and (enough_anchors or decisive_exception)
            and enough_pages
            and decisive_or_dominant
        )
        if not eligible:
            continue
        confidence = "גבוהה" if score >= float(rule["high_score"]) and (decisive_hits or len(pages) >= 2) else "ממוקדת"
        best = evidence_hits[0]
        enough_core_hits = decisive_hits >= 2 or bool(rule.get("single_core_allowed") and decisive_hits >= 1)
        tier = "core" if (
            confidence == "גבוהה" and enough_core_hits and best["role"] in {"holding", "operative"}
        ) else "related"
        reasons = [f"{anchor_hits} מופעי עוגן", f"{len(pages)} עמודים", f"{support_hits} התאמות הקשר"]
        if decisive_hits:
            reasons.append(f"{decisive_hits} מופעים בדיון או בהכרעה")
        output.append({
            "key": key, "label": rule["label"], "confidence": confidence, "tier": tier, "score": score,
            "sectionRole": ROLE_LABELS.get(best["role"], best["role"]), "pages": pages[:12],
            "reason": " · ".join(reasons),
            "anchors": [{"label": label, "count": count} for label, count in sorted(Counter(hit["anchor"] for hit in hits).items())],
            "metrics": {"anchorHits": anchor_hits, "pageCount": len(pages), "supportHits": support_hits,
                        "requiredSupportHits": required_support_hits, "decisiveHits": decisive_hits},
            "evidence": evidence_hits[:max_evidence],
        })
    return sorted(output, key=lambda item: (-item["score"], item["label"]))


def run(db_path: Path, rules_path: Path, output_path: Path, review_path: Path) -> dict:
    rules_source, rules = compile_rules(rules_path)
    db = connect(db_path)
    records = {}
    counts = Counter()
    review_rows = []
    documents = db.execute("SELECT id,case_number,decision_date,office FROM documents WHERE text_path<>'' ORDER BY id").fetchall()
    for document in documents:
        sections = db.execute(
            "SELECT page_number,role,normalized_text FROM sections WHERE document_id=? ORDER BY page_number,id",
            (document["id"],),
        ).fetchall()
        classifications = classify_document(sections, rules_source, rules)
        if classifications:
            records[document["id"]] = classifications
        for item in classifications:
            counts[item["key"]] += 1
            for evidence in item["evidence"]:
                review_rows.append({
                    "document_id": document["id"], "case_number": document["case_number"],
                    "decision_date": document["decision_date"], "office": document["office"],
                    "topic": item["label"], "confidence": item["confidence"], "score": item["score"],
                    "page": evidence["page"], "section_role": ROLE_LABELS.get(evidence["role"], evidence["role"]),
                    "anchor": evidence["anchor"], "snippet": evidence["snippet"],
                })
    payload = {
        "version": rules_source["version"], "generatedAt": datetime.now().astimezone().isoformat(),
        "documents": len(documents), "counts": dict(sorted(counts.items())), "records": records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    review_path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["document_id", "case_number", "decision_date", "office", "topic", "confidence", "score", "page", "section_role", "anchor", "snippet"]
    with review_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(review_rows)
    return {
        "version": rules_source["version"], "documents": len(documents), "classifiedDocuments": len(records),
        "topicCounts": dict(sorted(counts.items())), "evidenceRows": len(review_rows),
        "publicOutput": str(output_path), "reviewCsv": str(review_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Create strict local focus classifications from short evidence windows")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
    args = parser.parse_args()
    print(json.dumps(run(args.db, args.rules, args.output, args.review), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
