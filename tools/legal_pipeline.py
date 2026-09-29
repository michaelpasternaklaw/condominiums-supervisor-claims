#!/usr/bin/env python3
"""Local deterministic research/classification pipeline for the case-law site.

The SQLite database and research working files are local-only.  The only public
contract is the compact JSON written by ``export`` and consumed by build_site.py.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import unicodedata
import urllib.parse
from collections import defaultdict
from datetime import datetime
from pathlib import Path


SITE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SITE_ROOT.parents[2]
DATA_ROOT = PROJECT_ROOT / "outputs/legal_decisions_database"
REPOSITORY = SITE_ROOT.parent / "repository"
DEFAULT_DB = REPOSITORY / "legal_pipeline.sqlite"
DEFAULT_PUBLIC_EXPORT = REPOSITORY / "pipeline-public.json"
RULES_PATH = SITE_ROOT / "config/classification_rules.json"
MASTER = DATA_ROOT / "master_index.csv"
FULL_EXPORT = DATA_ROOT / "full_export/complete_export.csv"
EXTERNAL = DATA_ROOT / "external_missing_decisions.csv"
DUPLICATES = DATA_ROOT / "duplicates.csv"
ASHDOD = DATA_ROOT / "ashdod/ashdod_index.csv"
EVALUATION_DIR = REPOSITORY / "evaluation"

ROLE_LABELS = {
    "metadata": "מטא־דאטה", "background": "רקע", "plaintiff_argument": "טענת תובע",
    "defendant_argument": "טענת נתבע", "evidence": "ראיה", "analysis": "דיון",
    "holding": "קביעה", "operative": "הכרעה אופרטיבית", "costs": "הוצאות",
}
ROLE_PATTERNS = (
    ("operative", re.compile(r"(?:סוף דבר|סיכומו של דבר|אשר על כן|לאור כל האמור|התביעה (?:מתקבלת|נדחית))")),
    ("costs", re.compile(r"(?:הוצאות ההליך|שכר טרחה|הוצאות משפט)")),
    ("holding", re.compile(r"(?:דיון והכרעה|הכרעה|מן הכלל אל הפרט|אני (?:קובע|קובעת|מורה|דוחה|מקבל|מקבלת))")),
    ("plaintiff_argument", re.compile(r"(?:טענות התובע|לטענת התובע|התובעים טוענים)")),
    ("defendant_argument", re.compile(r"(?:טענות הנתבע|לטענת הנתבע|הנתבעים טוענים)")),
    ("evidence", re.compile(r"(?:ראיות|עדות|תצהיר|חוות דעת|פרוטוקול)")),
    ("analysis", re.compile(r"(?:דיון|המסגרת הנורמטיבית|מן הכלל|הלכה)")),
    ("background", re.compile(r"(?:רקע|פתח דבר|עיקרי העובדות|העובדות)")),
)


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def clean(value: object) -> str:
    return " ".join(str(value or "").replace("\u200f", "").replace("\u200e", "").split())


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = re.sub(r"[\u0591-\u05c7\u200e\u200f\u202a-\u202e]", "", value)
    value = value.replace("״", '"').replace("׳", "'").replace("–", "-").replace("—", "-")
    return " ".join(re.sub(r"[^\w\u0590-\u05ff/'\"-]+", " ", value.casefold()).split())


def split_pages(text: str) -> list[tuple[int, str]]:
    matches = list(re.finditer(r"=== PDF PAGE (\d+) ===", text))
    if matches:
        return [(int(match.group(1)), text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)].strip())
                for index, match in enumerate(matches)]
    pages = text.split("\f")
    return [(number, page.strip()) for number, page in enumerate(pages, 1) if page.strip()] or [(1, text)]


def load_rules() -> dict:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    return db


def create_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS runs (
      id INTEGER PRIMARY KEY, command TEXT NOT NULL, started_at TEXT NOT NULL,
      completed_at TEXT, rule_version TEXT, status TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS documents (
      id TEXT PRIMARY KEY, case_number TEXT, decision_date TEXT, year TEXT, office TEXT,
      adjudicator TEXT, municipality TEXT, source_status TEXT, source_name TEXT, source_url TEXT,
      pdf_path TEXT, text_path TEXT, metadata_json TEXT NOT NULL, text_sha256 TEXT,
      document_availability TEXT NOT NULL DEFAULT 'metadata_only', research_run_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS pages (
      document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      page_number INTEGER NOT NULL, raw_text TEXT NOT NULL, normalized_text TEXT NOT NULL,
      PRIMARY KEY(document_id,page_number)
    );
    CREATE TABLE IF NOT EXISTS sections (
      id INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      page_number INTEGER NOT NULL, role TEXT NOT NULL, raw_text TEXT NOT NULL, normalized_text TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS rule_hits (
      id INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      topic TEXT NOT NULL, term TEXT NOT NULL, section_role TEXT NOT NULL, page_number INTEGER NOT NULL,
      score REAL NOT NULL, snippet TEXT NOT NULL, rule_version TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS classifications (
      document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE, topic TEXT NOT NULL,
      confidence TEXT NOT NULL, score REAL NOT NULL, section_role TEXT NOT NULL,
      evidence_pages_json TEXT NOT NULL, evidence_snippets_json TEXT NOT NULL, rule_version TEXT NOT NULL,
      PRIMARY KEY(document_id,topic)
    );
    CREATE TABLE IF NOT EXISTS citations (
      id INTEGER PRIMARY KEY, source_document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      cited_case_number TEXT NOT NULL, page_number INTEGER NOT NULL, snippet TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS research_candidates (
      id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES runs(id), topic TEXT, query_text TEXT,
      case_number TEXT, decision_date TEXT, source_name TEXT, canonical_url TEXT,
      status TEXT NOT NULL, content_sha256 TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
      UNIQUE(canonical_url,case_number,decision_date)
    );
    CREATE TABLE IF NOT EXISTS duplicates (
      group_id TEXT NOT NULL, document_id TEXT NOT NULL, match_level TEXT, note TEXT,
      PRIMARY KEY(group_id,document_id)
    );
    CREATE TABLE IF NOT EXISTS labels (
      document_id TEXT NOT NULL, topic TEXT NOT NULL, expected INTEGER,
      expected_role TEXT, reviewer TEXT, notes TEXT, PRIMARY KEY(document_id,topic)
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
      document_id UNINDEXED, case_number, parties, summary, full_text, tokenize='unicode61'
    );
    CREATE INDEX IF NOT EXISTS pages_document_idx ON pages(document_id,page_number);
    CREATE INDEX IF NOT EXISTS hits_document_idx ON rule_hits(document_id,topic);
    CREATE INDEX IF NOT EXISTS research_status_idx ON research_candidates(status,topic);
    """)
    db.execute("UPDATE runs SET status='interrupted',completed_at=? WHERE status='running'",
               (datetime.now().astimezone().isoformat(),))
    db.commit()


def start_run(db: sqlite3.Connection, command: str, version: str = "") -> int:
    cursor = db.execute("INSERT INTO runs(command,started_at,rule_version,status) VALUES(?,?,?,'running')",
                        (command, datetime.now().astimezone().isoformat(), version))
    db.commit()
    return int(cursor.lastrowid)


def finish_run(db: sqlite3.Connection, run_id: int, details: dict, status: str = "complete") -> None:
    db.execute("UPDATE runs SET completed_at=?,status=?,details_json=? WHERE id=?",
               (datetime.now().astimezone().isoformat(), status, json.dumps(details, ensure_ascii=False), run_id))
    db.commit()


def detect_sections(page_text: str) -> list[tuple[str, str]]:
    blocks = [block.strip() for block in re.split(r"\n\s*\n|(?<=\.)\s+(?=\d{1,3}[.)]\s)", page_text) if block.strip()]
    role = "background"
    sections = []
    for block in blocks or [page_text]:
        probe = normalize(block[:500])
        for candidate, pattern in ROLE_PATTERNS:
            if pattern.search(probe):
                role = candidate
                break
        sections.append((role, block))
    return sections


def ingest(db: sqlite3.Connection) -> dict:
    run_id = start_run(db, "ingest")
    master = read_csv(MASTER)
    exports = {row.get("מזהה רשומה", ""): row for row in read_csv(FULL_EXPORT)}
    with db:
        for table in ("documents_fts", "citations", "rule_hits", "classifications", "sections", "pages", "documents", "duplicates"):
            db.execute(f"DELETE FROM {table}")
        for row in master:
            document_id = row.get("מזהה רשומה", "")
            exported = exports.get(document_id, {})
            text_path = Path(exported.get("טקסט מקומי", "")) if exported.get("טקסט מקומי") else None
            pdf_path = Path(exported.get("PDF מקומי מלא", "")) if exported.get("PDF מקומי מלא") else None
            full_text = text_path.read_text(encoding="utf-8", errors="replace") if text_path and text_path.is_file() else ""
            availability = "local_pdf_and_text" if full_text and pdf_path and pdf_path.is_file() else "public_link"
            metadata = json.dumps(row, ensure_ascii=False)
            db.execute("""INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                document_id, clean(row.get("מספר תיק")), row.get("תאריך", ""), row.get("שנה", ""),
                clean(row.get("לשכה")), clean(row.get("מפקח/ת")), clean(row.get("עיר")), clean(row.get("סטטוס")),
                clean(row.get("מקור")), row.get("קישור מקור") or row.get("קישור PDF", ""),
                str(pdf_path) if pdf_path and pdf_path.is_file() else "", str(text_path) if full_text else "", metadata,
                hashlib.sha256(full_text.encode()).hexdigest() if full_text else "", availability, None,
            ))
            if full_text:
                for page_number, raw_text in split_pages(full_text):
                    normalized = normalize(raw_text)
                    db.execute("INSERT INTO pages VALUES(?,?,?,?)", (document_id, page_number, raw_text, normalized))
                    for role, section_text in detect_sections(raw_text):
                        db.execute("INSERT INTO sections(document_id,page_number,role,raw_text,normalized_text) VALUES(?,?,?,?,?)",
                                   (document_id, page_number, role, section_text, normalize(section_text)))
                parties = " ".join((row.get("תובעים", ""), row.get("נתבעים", ""), row.get("באי כוח", "")))
                db.execute("INSERT INTO documents_fts VALUES(?,?,?,?,?)", (document_id, row.get("מספר תיק", ""), parties, row.get("תקציר", ""), full_text))
        for row in read_csv(DUPLICATES):
            db.execute("INSERT OR REPLACE INTO duplicates VALUES(?,?,?,?)", (row.get("קבוצת כפילות", ""), row.get("מזהה רשומה", ""), row.get("רמת התאמה", ""), row.get("הערה", "")))
    details = {"documents": len(master), "pages": db.execute("SELECT count(*) FROM pages").fetchone()[0]}
    finish_run(db, run_id, details)
    return details


def evidence_snippet(text: str, term: str, limit: int = 300) -> str:
    plain = clean(text)
    position = normalize(plain).find(normalize(term))
    position = max(0, position)
    return ("…" if position > 100 else "") + plain[max(0, position - 100):position + limit] + ("…" if position + limit < len(plain) else "")


def classify(db: sqlite3.Connection) -> dict:
    rules = load_rules()
    version = rules["version"]
    run_id = start_run(db, "classify", version)
    with db:
        db.execute("DELETE FROM rule_hits")
        db.execute("DELETE FROM classifications")
        db.execute("DELETE FROM citations")
        documents = db.execute("SELECT id FROM documents WHERE text_path<>''").fetchall()
        for document in documents:
            document_id = document["id"]
            sections = db.execute("SELECT page_number,role,raw_text,normalized_text FROM sections WHERE document_id=?", (document_id,)).fetchall()
            grouped: dict[str, list[dict]] = defaultdict(list)
            for section in sections:
                for topic, terms in rules["topics"].items():
                    for term in terms:
                        if normalize(term) not in section["normalized_text"]:
                            continue
                        weight = float(rules["role_weights"].get(section["role"], 1))
                        item = {"page": section["page_number"], "role": section["role"], "term": term,
                                "score": weight, "snippet": evidence_snippet(section["raw_text"], term)}
                        grouped[topic].append(item)
                        db.execute("""INSERT INTO rule_hits(document_id,topic,term,section_role,page_number,score,snippet,rule_version)
                                      VALUES(?,?,?,?,?,?,?,?)""", (document_id, topic, term, section["role"], section["page_number"], weight, item["snippet"], version))
            for topic, hits in grouped.items():
                unique_terms = {hit["term"] for hit in hits}
                pages = sorted({hit["page"] for hit in hits})
                decisive = [hit for hit in hits if hit["role"] in {"analysis", "holding", "operative"}]
                score = round(sum(hit["score"] for hit in hits[:20]) + min(3, len(unique_terms)), 2)
                confidence = "גבוהה" if score >= 8 and decisive else "סבירה" if score >= 3 else "לבדיקה"
                best = max(hits, key=lambda hit: hit["score"])
                snippets = []
                for hit in sorted(hits, key=lambda value: (-value["score"], value["page"])):
                    if hit["snippet"] not in snippets:
                        snippets.append(hit["snippet"])
                    if len(snippets) == 3:
                        break
                db.execute("INSERT INTO classifications VALUES(?,?,?,?,?,?,?,?)", (
                    document_id, topic, confidence, score, best["role"], json.dumps(pages[:12]),
                    json.dumps(snippets, ensure_ascii=False), version,
                ))
            for page in db.execute("SELECT page_number,raw_text FROM pages WHERE document_id=?", (document_id,)):
                for match in re.finditer(r"(?:תיק(?:\s+מס['\"׳]?)?|עש[א\"']|רע[א\"']|ע[א\"']|עמש)\s*([0-9]{1,5}(?:[-/][0-9]{1,5}){1,2})", page["raw_text"], re.I):
                    db.execute("INSERT INTO citations(source_document_id,cited_case_number,page_number,snippet) VALUES(?,?,?,?)",
                               (document_id, clean(match.group(1)), page["page_number"], evidence_snippet(page["raw_text"], match.group(0), 220)))
    details = {"documents": len(documents), "classifications": db.execute("SELECT count(*) FROM classifications").fetchone()[0],
               "citations": db.execute("SELECT count(*) FROM citations").fetchone()[0], "ruleVersion": version}
    finish_run(db, run_id, details)
    return details


def canonical_url(value: str) -> str:
    if not value:
        return ""
    parsed = urllib.parse.urlsplit(value.strip())
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def research(db: sqlite3.Connection) -> dict:
    run_id = start_run(db, "research", load_rules()["version"])
    topics = {
        "צנרת ומים": "צנרת מים נזילה בית משותף", "חצר או גינה צמודה": "חצר גינה הצמדה שימוש ייחודי",
        "מרזבים וניקוז": "מרזב ניקוז רטיבות בית משותף", "מצלמות ופרטיות": "מצלמה פרטיות רכוש משותף",
        "מזגנים": "מזגן מעבה רעש רכוש משותף",
    }
    sources = ("site:gov.il", "site:free-justice.openapi.gov.il", "site:judgments.org.il", "site:psakdin.co.il")
    imported = 0
    with db:
        for row in read_csv(EXTERNAL):
            url = canonical_url(row.get("קישור מקור") or row.get("קישור PDF", ""))
            indexed = db.execute("SELECT id FROM documents WHERE case_number=? AND decision_date=? LIMIT 1",
                                 (clean(row.get("מספר תיק")), row.get("תאריך", ""))).fetchone()
            status = "already_indexed" if indexed else "candidate"
            content_hash = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            db.execute("""INSERT INTO research_candidates
              (run_id,topic,query_text,case_number,decision_date,source_name,canonical_url,status,content_sha256,metadata_json)
              VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(canonical_url,case_number,decision_date) DO UPDATE SET
              run_id=excluded.run_id,topic=excluded.topic,source_name=excluded.source_name,status=excluded.status,
              content_sha256=excluded.content_sha256,metadata_json=excluded.metadata_json""", (run_id, row.get("תגיות נושא", ""), "", row.get("מספר תיק", ""), row.get("תאריך", ""),
              row.get("מקור", ""), url, status, content_hash, json.dumps(row, ensure_ascii=False)))
            if indexed:
                db.execute("UPDATE documents SET research_run_id=? WHERE id=?", (run_id, indexed["id"]))
            imported += 1
        for topic, terms in topics.items():
            for source in sources:
                query = f'{source} (אשדוד OR "לשכת רחובות") "מפקח על רישום מקרקעין" {terms}'
                url = "https://www.google.com/search?" + urllib.parse.urlencode({"q": query})
                db.execute("""INSERT INTO research_candidates
                  (run_id,topic,query_text,case_number,decision_date,source_name,canonical_url,status,content_sha256,metadata_json)
                  VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(canonical_url,case_number,decision_date) DO UPDATE SET
                  run_id=excluded.run_id,topic=excluded.topic,query_text=excluded.query_text,status=excluded.status""",
                  (run_id, topic, query, "", "", source, url, "query_ready", "", "{}"))
    csv_path = REPOSITORY / "research_candidates.csv"
    rows = db.execute("""SELECT topic,query_text,case_number,decision_date,source_name,canonical_url,status,
                                content_sha256,run_id FROM research_candidates ORDER BY status,topic,decision_date""").fetchall()
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("topic", "query", "case_number", "decision_date", "source", "url", "status", "sha256", "run_id"))
        writer.writerows(tuple(row) for row in rows)
    details = {"importedExternal": imported, "queries": len(topics) * len(sources),
               "candidates": len(rows), "csv": str(csv_path)}
    finish_run(db, run_id, details)
    return details


def export_public(db: sqlite3.Connection, destination: Path) -> dict:
    run_id = start_run(db, "export", load_rules()["version"])
    records = {}
    for document in db.execute("SELECT * FROM documents"):
        classifications = db.execute("SELECT * FROM classifications WHERE document_id=? ORDER BY score DESC", (document["id"],)).fetchall()
        evidence_pages = sorted({page for row in classifications for page in json.loads(row["evidence_pages_json"])})
        snippets = []
        for row in classifications:
            snippets.extend(json.loads(row["evidence_snippets_json"]))
        citations = [dict(row) for row in db.execute("SELECT cited_case_number,page_number FROM citations WHERE source_document_id=? ORDER BY page_number LIMIT 30", (document["id"],))]
        strongest = classifications[0] if classifications else None
        records[document["id"]] = {
            "classificationConfidence": strongest["confidence"] if strongest else "",
            "sectionRole": ROLE_LABELS.get(strongest["section_role"], "") if strongest else "",
            "evidencePages": evidence_pages[:20], "evidenceSnippets": list(dict.fromkeys(snippets))[:5],
            "ruleVersion": strongest["rule_version"] if strongest else "", "citationLinks": citations,
            "researchRunId": document["research_run_id"], "documentAvailability": document["document_availability"],
            "documentUrl": document["source_url"], "classifications": [
                {"topic": row["topic"], "confidence": row["confidence"], "score": row["score"],
                 "sectionRole": ROLE_LABELS.get(row["section_role"], row["section_role"]),
                 "pages": json.loads(row["evidence_pages_json"])} for row in classifications
            ],
        }
    payload = {"version": load_rules()["version"], "generatedAt": datetime.now().astimezone().isoformat(), "records": records}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    details = {"records": len(records), "bytes": destination.stat().st_size, "path": str(destination)}
    finish_run(db, run_id, details)
    return details


def make_gold_sample(db: sqlite3.Connection, path: Path, size: int = 150) -> int:
    ashdod_ids = []
    known = {row.get("מזהה רשומה", "") for row in read_csv(ASHDOD)}
    for row in db.execute("SELECT id,metadata_json FROM documents ORDER BY decision_date DESC"):
        metadata = json.loads(row["metadata_json"])
        if row["id"] in known or "אשדוד" in json.dumps(metadata, ensure_ascii=False):
            ashdod_ids.append(row["id"])
    candidates = db.execute("""SELECT document_id,topic,confidence,section_role FROM classifications
                               WHERE document_id IN (%s) ORDER BY topic,score DESC""" % ",".join("?" * len(ashdod_ids)), ashdod_ids).fetchall() if ashdod_ids else []
    selected = list(candidates[:size])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("document_id", "topic", "predicted_confidence", "predicted_role", "expected", "expected_role", "reviewer", "notes"))
        for row in selected:
            writer.writerow((row["document_id"], row["topic"], row["confidence"], row["section_role"], "", "", "", ""))
    return len(selected)


def evaluate(db: sqlite3.Connection) -> dict:
    run_id = start_run(db, "evaluate", load_rules()["version"])
    labels_path = EVALUATION_DIR / "gold_labels.csv"
    template_path = EVALUATION_DIR / "gold_sample.csv"
    if not labels_path.exists():
        count = make_gold_sample(db, template_path)
        report = {"status": "needs_gold_labels", "sampleItems": count, "template": str(template_path),
                  "note": "נדרשת בדיקה אנושית; לא חושבו מדדי דיוק ללא תוויות אמת."}
    else:
        labels = read_csv(labels_path)
        tp = fp = fn = role_ok = role_total = 0
        for label in labels:
            predicted = db.execute("SELECT confidence,section_role FROM classifications WHERE document_id=? AND topic=?",
                                   (label["document_id"], label["topic"])).fetchone()
            expected = label.get("expected") in {"1", "true", "כן"}
            high = bool(predicted and predicted["confidence"] == "גבוהה")
            tp += high and expected; fp += high and not expected; fn += expected and not predicted
            if expected and predicted and label.get("expected_role"):
                role_total += 1; role_ok += predicted["section_role"] == label["expected_role"]
        precision = tp / (tp + fp) if tp + fp else 0
        recall = tp / (tp + fn) if tp + fn else 0
        role_accuracy = role_ok / role_total if role_total else 0
        report = {"status": "passed" if precision >= .9 and recall >= .8 and role_accuracy >= .9 else "threshold_not_met",
                  "labels": len(labels), "highPrecision": precision, "recall": recall, "roleAccuracy": role_accuracy,
                  "thresholds": {"highPrecision": .9, "recall": .8, "roleAccuracy": .9}}
    report_path = EVALUATION_DIR / "evaluation-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finish_run(db, run_id, report, "complete" if report["status"] == "passed" else "review")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Local deterministic legal classification pipeline")
    parser.add_argument("command", choices=("ingest", "classify", "research", "evaluate", "export", "all"))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--public-export", type=Path, default=DEFAULT_PUBLIC_EXPORT)
    args = parser.parse_args()
    db = connect(args.db)
    create_schema(db)
    results = {}
    if args.command in {"ingest", "all"}: results["ingest"] = ingest(db)
    if args.command in {"classify", "all"}: results["classify"] = classify(db)
    if args.command in {"research", "all"}: results["research"] = research(db)
    if args.command in {"export", "all"}: results["export"] = export_public(db, args.public_export)
    if args.command in {"evaluate", "all"}: results["evaluate"] = evaluate(db)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
