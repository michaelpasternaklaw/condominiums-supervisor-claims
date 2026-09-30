#!/usr/bin/env python3
"""Build the public, static litigation research workbench."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import unicodedata
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path


SITE_ROOT = Path(__file__).resolve().parent
PUBLIC_DIST = SITE_ROOT / "dist"
DIST = SITE_ROOT / ".dist-staging"
PROJECT_ROOT = SITE_ROOT.parents[2]
REPOSITORY = SITE_ROOT.parent / "repository"
MASTER = PROJECT_ROOT / "outputs/legal_decisions_database/master_index.csv"
DUPLICATES = PROJECT_ROOT / "outputs/legal_decisions_database/duplicates.csv"
ASHDOD = PROJECT_ROOT / "outputs/legal_decisions_database/ashdod/ashdod_index.csv"
FULL_EXPORT = PROJECT_ROOT / "outputs/legal_decisions_database/full_export/complete_export.csv"
DOCX_EXPORT = PROJECT_ROOT / "outputs/legal_decisions_database/full_export/docx_export.csv"
FULL_CLASSIFICATION = PROJECT_ROOT / "outputs/legal_decisions_database/full_text_classification.csv"
PIPELINE_EXPORT = REPOSITORY / "pipeline-public.json"
FOCUS_EXPORT = REPOSITORY / "focus-public.json"
TOKEN_BUCKETS = 64
PAGE_SHARDS = 64
EVIDENCE_SHARDS = 64
MAX_SITE_BYTES = 850 * 1024 * 1024
MAX_FILE_BYTES = 90 * 1024 * 1024
RESERVED_NON_PDF_BYTES = 260 * 1024 * 1024

ALLOWED_PDF_ROOTS = (
    PROJECT_ROOT / "legal_library/case_law/tabu_relevant/files",
    PROJECT_ROOT / "legal_library/case_law/sources",
    PROJECT_ROOT / "tabu_download_test",
    PROJECT_ROOT / "outputs/legal_decisions_database/full_export/pdf",
)
ALLOWED_TEXT_ROOTS = (
    REPOSITORY / "texts",
    PROJECT_ROOT / "outputs/legal_decisions_database/full_export/text",
)
ALLOWED_DOCX_ROOTS = (
    PROJECT_ROOT / "outputs/legal_decisions_database/full_export/docx",
)
BLOCKED_PUBLICATION_TERMS = (
    "סמטת יהואש 2",
    "יהואש 2 אשדוד",
    "מיכאל פסטרנק",
    "יעל אביטבול פסטרנק",
    "זינובי ונטליה שוורצבורד",
)


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_curated() -> list[dict]:
    path = REPOSITORY / "metadata.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def clean(value) -> str:
    return " ".join(str(value or "").replace("\u200f", "").replace("\u200e", "").split())


def split_terms(value: str) -> list[str]:
    return sorted({clean(item) for item in re.split(r"[;,|]", value or "") if clean(item)})


def normalize_case(value: str) -> str:
    parts = re.findall(r"\d+", value or "")
    return "/".join(str(int(part)) for part in parts[:3]) if parts else clean(value).casefold()


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = re.sub(r"[\u0591-\u05C7]", "", value)
    value = re.sub(r"[\u200e\u200f\u202a-\u202e]", "", value)
    value = re.sub(r"[^\w\u0590-\u05ff]+", " ", value, flags=re.UNICODE)
    return " ".join(value.casefold().split())


def stable_bucket(value: str, count: int) -> int:
    hash_value = 2166136261
    for char in value:
        hash_value ^= ord(char)
        hash_value = (hash_value * 16777619) & 0xFFFFFFFF
    return hash_value % count


def is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    return any(resolved == root.resolve() or resolved.is_relative_to(root.resolve()) for root in roots)


def copy_public_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_public_docx(path: Path, expected_sha256: str = "") -> None:
    if not is_within(path, ALLOWED_DOCX_ROOTS):
        raise RuntimeError(f"Public build blocked: DOCX outside approved roots: {path}")
    if not path.is_file() or not zipfile.is_zipfile(path):
        raise RuntimeError(f"Public build blocked: invalid DOCX container: {path}")
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            raise RuntimeError(f"Public build blocked: incomplete DOCX package: {path}")
        document_xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
        normalized_xml = normalize_text(document_xml)
        for term in BLOCKED_PUBLICATION_TERMS:
            if normalize_text(term) in normalized_xml:
                raise RuntimeError(f"Public build blocked: private term in DOCX: {term}")
    if expected_sha256 and file_sha256(path) != expected_sha256:
        raise RuntimeError(f"Public build blocked: DOCX hash mismatch: {path}")


def clean_stale(directory: Path, expected: set[str], pattern: str = "*") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for path in directory.glob(pattern):
        if path.is_file() and path.name not in expected:
            path.unlink()


def split_pages(full_text: str) -> list[tuple[int, str]]:
    matches = list(re.finditer(r"=== PDF PAGE (\d+) ===", full_text))
    if not matches:
        parts = full_text.split("\f")
        if len(parts) > 1 and not parts[-1].strip():
            parts.pop()
        return [(index + 1, text.strip()) for index, text in enumerate(parts)] or [(1, full_text)]
    pages = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(full_text)
        pages.append((int(match.group(1)), full_text[start:end].strip()))
    return pages


OPERATIVE_HEADINGS = (
    r"סיכום\s+והוצאות",
    r"סיכומו\s+של\s+דבר",
    r"סוף\s+דבר",
)
OPERATIVE_CONCLUSIONS = (
    r"אשר\s+על\s+כן",
    r"לאור\s+(?:כל\s+)?האמור(?:\s+לעיל)?",
)
OPERATIVE_DIRECT = (
    r"דינה\s+של\s+התביעה\s+להתקבל",
    r"התביעה\s+(?:מתקבלת|נדחית)",
    r"אני\s+(?:מקבל|מקבלת|דוחה|מורה|מחייב|מחייבת)\b",
)


def scrub_operative_page(value: str) -> str:
    """Remove line numbers and repeating PDF furniture without rewriting the judgment."""
    kept = []
    furniture = re.compile(
        r"^(?:מדינת\s+ישראל|משרד\s+המשפטים|לשכת\s+המפקח.*|"
        r"מפקח(?:ת)?\s+על\s+רישום.*|בסמכות\s+שופט.*|לפי\s+סעיף.*|"
        r"מס['׳]?\s*תיק.*|עמוד\s+\d+.*|קבלת\s+קהל.*|[!•]?\s*ת[\"'׳]ד\s+\d+.*)$"
    )
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if (
            not line or furniture.match(line) or "℡" in line or "☎" in line
            or line in {"אשדוד", "באר שבע", "חולון", "חיפה", "ירושלים", "נצרת", "נתניה", "עכו", "פתח תקווה", "רחובות", "תל אביב"}
            or "מגדל העיר" in line or "תיבת דואר" in line
        ):
            continue
        line = re.sub(r"^\d{1,3}\s+(?=\D)", "", line)
        line = re.sub(r"(?<!\d)\.(\d{1,3})(?=[א-ת])", r" [סעיף \1] ", line)
        line = re.sub(r"(?<=\d)(?=[א-ת])|(?<=[א-ת])(?=\d)", " ", line)
        kept.append(line)
    return clean(" ".join(kept))


def outcome_and_relief(excerpt: str) -> tuple[str, str]:
    normalized = normalize_text(excerpt)
    opening = normalized[:700]
    if re.search(r"(?:מקבל|מקבלת) את התביעה בעיקרה", opening):
        outcome = "התביעה התקבלה בעיקרה"
    elif re.search(r"(?:מקבל|מקבלת) את התביעה בחלקה|התביעה מתקבלת בחלקה", opening):
        outcome = "התביעה התקבלה בחלקה"
    elif re.search(r"דוחה את התביעה|התביעה נדחית|דינה של התביעה להידחות", opening):
        outcome = "התביעה נדחתה"
    elif re.search(r"דינה של התביעה להתקבל|התביעה מתקבלת|(?:מקבל|מקבלת) את התביעה", opening):
        outcome = "התביעה התקבלה"
    elif re.search(r"דוחה את בקשת הפסילה|בקשת הפסילה נדחית", opening):
        outcome = "בקשת הפסלות נדחתה"
    elif re.search(r"דוחה את הבקשה|דוחה את בקשת|הבקשה נדחית", opening):
        outcome = "הבקשה נדחתה"
    elif re.search(r"מקבל את הבקשה|מקבלת את הבקשה|הבקשה מתקבלת", opening):
        outcome = "הבקשה התקבלה"
    else:
        outcome = "הכרעה אופרטיבית אותרה"

    reliefs = []
    no_expenses = bool(re.search(r"אין צו להוצאות|אי צו להוצאות", normalized))
    relief_patterns = (
        (r"צו מניעה|להימנע מ", "צו מניעה"),
        (r"סילוק יד|לסלק את יד", "סילוק יד"),
        (r"להרוס|לפרק|להסיר|להשיב את המצב|יושב המצב לקדמותו", "הריסה / הסרה / השבת מצב"),
        (r"לתקן|ביצוע התיקון|עבודות התיקון", "צו תיקון / צו עשה"),
        (r"דמי שימוש", "דמי שימוש"),
        (r"פיצוי|עוגמת נפש", "פיצוי"),
        (r"לשלם|מחייב", "חיוב כספי"),
        (r"הוצאות|שכר טרחה|שכ ט", "ללא צו להוצאות" if no_expenses else "הוצאות ושכר טרחה"),
    )
    for pattern, label in relief_patterns:
        if re.search(pattern, normalized) and label not in reliefs:
            reliefs.append(label)
    return outcome, "; ".join(reliefs)


def extract_operative(parsed_pages: list[tuple[int, str]]) -> dict:
    """Locate the dispositive section near the end and retain its real PDF page references."""
    segments = []
    cursor = 0
    for page_number, page_text in parsed_pages:
        cleaned = scrub_operative_page(page_text)
        if not cleaned:
            continue
        start = cursor
        segments.append((start, start + len(cleaned), page_number, cleaned))
        cursor += len(cleaned) + 1
    combined = "\n".join(segment[3] for segment in segments)
    if not combined:
        return {}

    search_from = int(len(combined) * .48)
    candidates = []
    for pattern in OPERATIVE_HEADINGS:
        candidates.extend((match.start(), "גבוהה") for match in re.finditer(pattern, combined[search_from:]) )
    candidates = [(position + search_from, confidence) for position, confidence in candidates]
    if not candidates:
        conclusion_candidates = []
        for pattern in OPERATIVE_CONCLUSIONS:
            conclusion_candidates.extend(match.start() + search_from for match in re.finditer(pattern, combined[search_from:]))
        if conclusion_candidates:
            candidates.append((max(conclusion_candidates), "גבוהה"))
    if not candidates:
        direct_from = int(len(combined) * .64)
        for pattern in OPERATIVE_DIRECT:
            candidates.extend((match.start() + direct_from, "בינונית") for match in re.finditer(pattern, combined[direct_from:]))
    if not candidates:
        return {}

    start, confidence = min(candidates, key=lambda item: item[0])
    tail = combined[start:]
    signature = re.search(r"\sנית(?:ן|&|ה)?\s*,?\s*(?:היום|היו|בהעדר|ביום)\b", tail)
    end = start + (signature.start() if signature else len(tail))
    excerpt = clean(combined[start:end]).strip(" ;:")
    if len(excerpt) < 45:
        return {}
    if len(excerpt) > 4200:
        excerpt = excerpt[:4200].rsplit(".", 1)[0].strip() + "."
        end = start + len(excerpt)

    used_pages = [number for seg_start, seg_end, number, _ in segments if seg_end >= start and seg_start <= end]
    outcome, relief = outcome_and_relief(excerpt)
    return {
        "excerpt": excerpt,
        "pages": sorted(set(used_pages)),
        "confidence": confidence,
        "outcome": outcome,
        "relief": relief,
    }


def curated_lookup(records: list[dict]) -> tuple[dict, dict]:
    exact: dict[tuple[str, str], list[dict]] = defaultdict(list)
    by_case: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        case = normalize_case(record.get("primary_case_number", ""))
        exact[(case, record.get("decision_date", ""))].append(record)
        by_case[case].append(record)
    return exact, by_case


def choose_curated(row: dict, exact: dict, by_case: dict, used: set[str]) -> dict | None:
    case = normalize_case(row.get("מספר תיק", ""))
    candidates = exact.get((case, row.get("תאריך", "")), []) or by_case.get(case, [])
    for candidate in candidates:
        if candidate["document_id"] not in used:
            used.add(candidate["document_id"])
            return candidate
    return None


def source_strength(row: dict, curated: dict | None, status: str, has_text: bool = False) -> tuple[int, str]:
    searchable = " ".join([row.get("מקור", ""), row.get("מספר תיק", ""), row.get("הערות", "")])
    if status == "ליד מחקרי":
        return 1, "ליד מחקרי"
    if "משוחזר" in status or "חיצוני" in status:
        if status == "מקור חיצוני" and re.search(r"עליון|מחוזי|ערעור|רע[א״']|עש[א״']|עמ[ש״']", searchable):
            return 6, "פסק דין בערעור או בבית משפט"
        return 2, "מקור חיצוני או שחזור"
    review = (curated or {}).get("review_status", "")
    if curated and "manually reviewed" in review:
        return 5, "מקור רשמי שנבדק ידנית"
    if curated or has_text:
        return 4, "מקור רשמי עם טקסט מלא"
    return 3, "מקור רשמי המבוסס על מטא־דאטה"


def assert_public_source(record: dict, source_pdf: Path | None, source_text: Path | None) -> None:
    if source_pdf and not is_within(source_pdf, ALLOWED_PDF_ROOTS):
        raise RuntimeError(f"Public build blocked: PDF outside approved roots: {source_pdf}")
    if source_text and not is_within(source_text, ALLOWED_TEXT_ROOTS):
        raise RuntimeError(f"Public build blocked: text outside approved root: {source_text}")
    searchable = normalize_text(json.dumps(record, ensure_ascii=False))
    for term in BLOCKED_PUBLICATION_TERMS:
        if normalize_text(term) in searchable:
            raise RuntimeError(f"Public build blocked: private term in public metadata: {term}")


def prepare_staging() -> None:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)
    for name in ("index.html", "styles.css", "app.js", "search-worker.js", ".nojekyll"):
        source = PUBLIC_DIST / name
        if not source.is_file():
            raise RuntimeError(f"Static site asset is missing: {source}")
        shutil.copy2(source, DIST / name)


def publish_staging() -> None:
    backup = SITE_ROOT / ".dist-previous"
    if backup.exists():
        shutil.rmtree(backup)
    if PUBLIC_DIST.exists():
        PUBLIC_DIST.rename(backup)
    try:
        DIST.rename(PUBLIC_DIST)
    except Exception:
        if backup.exists() and not PUBLIC_DIST.exists():
            backup.rename(PUBLIC_DIST)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def main() -> None:
    prepare_staging()
    master_rows = read_csv(MASTER)
    duplicate_rows = read_csv(DUPLICATES)
    ashdod_rows = read_csv(ASHDOD)
    export_rows = read_csv(FULL_EXPORT)
    docx_rows = read_csv(DOCX_EXPORT)
    classification_rows = read_csv(FULL_CLASSIFICATION)
    curated_records = load_curated()
    pipeline = json.loads(PIPELINE_EXPORT.read_text(encoding="utf-8")) if PIPELINE_EXPORT.exists() else {"version": "", "records": {}}
    pipeline_by_id = pipeline.get("records", {})
    focus = json.loads(FOCUS_EXPORT.read_text(encoding="utf-8")) if FOCUS_EXPORT.exists() else {"version": "", "records": {}, "counts": {}}
    focus_by_id = focus.get("records", {})

    if len(master_rows) != 1799:
        raise RuntimeError(f"Expected 1,799 master rows, found {len(master_rows)}")
    if len(docx_rows) != 1799:
        raise RuntimeError(f"Expected 1,799 DOCX manifest rows, found {len(docx_rows)}")
    available_docx_count = sum(row.get("סטטוס יצירה") == "available" for row in docx_rows)
    if available_docx_count != 1763:
        raise RuntimeError(f"Expected 1,763 available DOCX files, found {available_docx_count}")
    official_count = sum(row.get("סטטוס") == "רשמי" for row in master_rows)
    if official_count != 1765:
        raise RuntimeError(f"Expected 1,765 official rows, found {official_count}")
    if focus.get("documents") != 1763 or not focus.get("version"):
        raise RuntimeError("Focused classification export is missing or stale")

    duplicate_by_id = {row["מזהה רשומה"]: row.get("קבוצת כפילות", "") for row in duplicate_rows}
    export_by_id = {row["מזהה רשומה"]: row for row in export_rows}
    docx_by_id = {row["מזהה רשומה"]: row for row in docx_rows}
    classification_by_id = {row["מזהה רשומה"]: row for row in classification_rows}
    ashdod_by_case: dict[str, list[dict]] = defaultdict(list)
    for row in ashdod_rows:
        ashdod_by_case[normalize_case(row.get("מספר תיק", ""))].append(row)

    exact_curated, case_curated = curated_lookup(curated_records)
    used_curated: set[str] = set()
    catalog: list[dict] = []
    expected_pdfs: set[str] = set()
    expected_docx: set[str] = set()
    token_index: list[dict[str, dict[str, set[int]]]] = [defaultdict(lambda: defaultdict(set)) for _ in range(TOKEN_BUCKETS)]
    page_shards: list[dict[str, list[list]]] = [dict() for _ in range(PAGE_SHARDS)]
    evidence_shards: list[dict[str, dict]] = [dict() for _ in range(EVIDENCE_SHARDS)]
    doc_shards: dict[str, int] = {}
    evidence_doc_shards: dict[str, int] = {}
    copied_pdf_bytes = 0
    copied_docx_bytes = 0

    pdf_dir = DIST / "pdfs"
    docx_dir = DIST / "docx"
    search_dir = DIST / "data/search"
    data_dir = DIST / "data"
    for directory in (pdf_dir, docx_dir, search_dir, data_dir):
        directory.mkdir(parents=True, exist_ok=True)

    for row in master_rows:
        curated = choose_curated(row, exact_curated, case_curated, used_curated)
        case_key = normalize_case(row.get("מספר תיק", ""))
        ashdod_candidates = ashdod_by_case.get(case_key, [])
        ashdod = next((item for item in ashdod_candidates if (
            clean(item.get("מספר תיק")) == clean(row.get("מספר תיק"))
            and row.get("תאריך") in {item.get("תאריך API"), item.get("תאריך קובע")}
        )), None)
        status = (ashdod or {}).get("סטטוס") or row.get("סטטוס", "")
        exported = export_by_id.get(row["מזהה רשומה"], {})
        docx_exported = docx_by_id.get(row["מזהה רשומה"], {})
        classified = classification_by_id.get(row["מזהה רשומה"], {})
        pipeline_record = pipeline_by_id.get(row["מזהה רשומה"], {})
        focus_items = focus_by_id.get(row["מזהה רשומה"], [])
        focus_summaries = [{key: item.get(key) for key in ("key", "label", "confidence", "score", "sectionRole", "pages", "reason")}
                           for item in focus_items]
        exported_pdf = Path(exported["PDF מקומי מלא"]) if exported.get("PDF מקומי מלא") else None
        exported_text = Path(exported["טקסט מקומי"]) if exported.get("טקסט מקומי") else None
        rank, verification = source_strength(row, curated, status, bool(exported_text and exported_text.is_file()))
        duplicate_group = duplicate_by_id.get(row.get("מזהה רשומה", ""), "")
        quality = (ashdod or {}).get("איכות רשומה", "")
        is_test = "רשומת בדיקה" in quality or "בדיקה" in " ".join([
            row.get("מספר תיק", ""), row.get("תובעים", ""), row.get("נתבעים", "")
        ])

        local_pdf = ""
        local_docx = ""
        operative = ""
        operative_pages: list[int] = []
        operative_confidence = ""
        outcome = ""
        relief = ""
        review_status = "מטא־דאטה בלבד"
        pages = 0
        source_pdf = None
        source_text = None
        if curated:
            source_pdf = PROJECT_ROOT / curated["source_files"][0]
            source_text = PROJECT_ROOT / curated["text_path"]
        else:
            source_pdf = exported_pdf if exported_pdf and exported_pdf.is_file() else None
            source_text = exported_text if exported_text and exported_text.is_file() else None
        if source_pdf or source_text:
            assert_public_source(curated, source_pdf, source_text)
            if source_pdf:
                public_stem = curated["document_id"] if curated else row["מזהה רשומה"]
                pdf_name = f"{public_stem}.pdf"
                pdf_size = source_pdf.stat().st_size
                if pdf_size <= MAX_FILE_BYTES and copied_pdf_bytes + pdf_size <= MAX_SITE_BYTES - RESERVED_NON_PDF_BYTES:
                    copy_public_file(source_pdf, pdf_dir / pdf_name)
                    expected_pdfs.add(pdf_name)
                    local_pdf = f"pdfs/{pdf_name}"
                    copied_pdf_bytes += pdf_size
            if source_text:
                review_status = clean((curated or {}).get("review_status", "")) or clean(classified.get("מצב אימות הסיווג", ""))
                full_text = source_text.read_text(encoding="utf-8", errors="replace")
                parsed_pages = split_pages(full_text)
                pages = len(parsed_pages)
                operative_data = extract_operative(parsed_pages)
                operative = operative_data.get("excerpt", "") or clean(classified.get("קטע אופרטיבי אוטומטי", ""))
                operative_pages = operative_data.get("pages", []) or [int(value) for value in re.findall(r"\d+", classified.get("עמודי הכרעה", ""))]
                operative_confidence = operative_data.get("confidence", "") or "אוטומטית"
                outcome = operative_data.get("outcome", "") or clean(classified.get("תוצאה אוטומטית", ""))
                if outcome == "לא סווג אוטומטית":
                    outcome = ""
                relief = operative_data.get("relief", "") or clean(classified.get("סעדים שאותרו", ""))
                shard = stable_bucket(row["מזהה רשומה"], PAGE_SHARDS)
                doc_shards[row["מזהה רשומה"]] = shard
                page_shards[shard][row["מזהה רשומה"]] = [[number, text] for number, text in parsed_pages]
                for number, page_text in parsed_pages:
                    tokens = {token for token in normalize_text(page_text).split() if 1 < len(token) <= 40}
                    for token in tokens:
                        token_index[stable_bucket(token, TOKEN_BUCKETS)][token][row["מזהה רשומה"]].add(number)

        if docx_exported.get("סטטוס יצירה") == "available" and docx_exported.get("נתיב DOCX"):
            source_docx = Path(docx_exported["נתיב DOCX"])
            validate_public_docx(source_docx, docx_exported.get("SHA256", ""))
            docx_name = f"{row['מזהה רשומה']}.docx"
            docx_size = source_docx.stat().st_size
            if docx_size > MAX_FILE_BYTES:
                raise RuntimeError(f"DOCX exceeds the public per-file limit: {source_docx}")
            copy_public_file(source_docx, docx_dir / docx_name)
            expected_docx.add(docx_name)
            local_docx = f"docx/{docx_name}"
            copied_docx_bytes += docx_size

        categories = split_terms(classified.get("תגיות נושא מלאות") or row.get("תגיות נושא", ""))
        keywords = split_terms(row.get("מילות מפתח משפטיות", ""))
        defense_topics = split_terms(classified.get("טענות הגנה שאותרו", ""))
        legal_principles = split_terms(classified.get("עקרונות משפטיים", ""))
        source_url = row.get("קישור מקור") or row.get("קישור PDF", "")
        pdf_url = local_pdf or row.get("קישור PDF") or source_url
        address = " ".join(part for part in [clean(row.get("רחוב")), clean(row.get("מספר בית"))] if part)
        ashdod_relation = (ashdod or {}).get("זיקה לאשדוד", "")
        record = {
            "id": row["מזהה רשומה"], "caseNumber": clean(row.get("מספר תיק")),
            "date": row.get("תאריך", ""), "year": row.get("שנה", ""),
            "type": clean(row.get("סוג החלטה")), "office": clean(row.get("לשכה")),
            "adjudicator": clean(row.get("מפקח/ת")), "municipality": clean(row.get("עיר")),
            "address": address, "block": clean(row.get("גוש")), "plot": clean(row.get("חלקה")),
            "plaintiffs": clean(row.get("תובעים")), "defendants": clean(row.get("נתבעים")),
            "representatives": clean(row.get("באי כוח")), "categories": categories, "keywords": keywords,
            "defenseTopics": defense_topics, "legalPrinciples": legal_principles,
            "topicPages": clean(classified.get("עמודי נושא", "")),
            "defensePages": clean(classified.get("עמודי טענות הגנה", "")),
            "summary": clean(row.get("תקציר")), "operativeExcerpt": operative,
            "operativePages": operative_pages, "operativeConfidence": operative_confidence,
            "sourceName": clean(row.get("מקור")), "sourceStatus": clean(status), "sourceUrl": source_url,
            "pdf": pdf_url, "docx": local_docx, "text": "", "hasFullText": bool(source_text),
            "hasLocalPdf": bool(local_pdf), "hasDocx": bool(local_docx),
            "docxOrigin": clean(docx_exported.get("סוג DOCX", "")),
            "pages": pages, "verification": verification, "verificationRank": rank,
            "reviewStatus": review_status, "classificationBasis": clean(row.get("בסיס הסיווג")),
            "ashdodRelation": clean(ashdod_relation),
            "duplicateGroup": duplicate_group or (ashdod or {}).get("קבוצת כפילות", ""),
            "isTest": is_test, "certainty": clean((ashdod or {}).get("רמת ודאות", "")),
            "notes": clean(row.get("הערות", "")), "outcome": outcome, "relief": relief, "appealLinks": [],
            "textMethod": clean(classified.get("שיטת המרה", "")),
            "classificationConfidence": pipeline_record.get("classificationConfidence", ""),
            "sectionRole": pipeline_record.get("sectionRole", ""),
            "evidencePages": pipeline_record.get("evidencePages", []),
            "ruleVersion": pipeline_record.get("ruleVersion", ""),
            "researchRunId": pipeline_record.get("researchRunId"),
            "documentAvailability": "עותק מקומי" if local_pdf else ("מסמך מלא במקור הציבורי" if pdf_url else "מטא־דאטה בלבד"),
            "documentUrl": pipeline_record.get("documentUrl") or source_url,
            "localAssetUrl": local_pdf,
            "focusClassifications": focus_summaries,
            "focusTopics": [item.get("key") for item in focus_summaries],
            "focusRuleVersion": focus.get("version", ""),
        }
        if pipeline_record:
            evidence_shard = stable_bucket(row["מזהה רשומה"], EVIDENCE_SHARDS)
            evidence_doc_shards[row["מזהה רשומה"]] = evidence_shard
            evidence_shards[evidence_shard][row["מזהה רשומה"]] = {
                "evidenceSnippets": pipeline_record.get("evidenceSnippets", []),
                "classifications": pipeline_record.get("classifications", []),
                "citationLinks": pipeline_record.get("citationLinks", []),
                "focusEvidence": focus_items,
            }
        catalog.append(record)

    clean_stale(pdf_dir, expected_pdfs, "*.pdf")
    clean_stale(docx_dir, expected_docx, "*.docx")
    catalog.sort(key=lambda item: (item["date"], item["caseNumber"]), reverse=True)

    payload = {
        "generatedAt": datetime.now().astimezone().isoformat(), "totalDocuments": len(catalog),
        "officialDocuments": official_count, "externalDocuments": len(catalog) - official_count,
        "fullTextDocuments": sum(item["hasFullText"] for item in catalog),
        "localPdfDocuments": sum(item["hasLocalPdf"] for item in catalog),
        "docxDocuments": sum(item["hasDocx"] for item in catalog),
        "ocrDocuments": sum(item["textMethod"] == "OCR" for item in catalog),
        "ashdodOfficial": sum(bool(item["sourceStatus"] == "רשמי" and item["ashdodRelation"]) for item in catalog),
        "offices": sorted({item["office"] for item in catalog if item["office"]}),
        "municipalities": sorted({item["municipality"] for item in catalog if item["municipality"]}),
        "adjudicators": sorted({item["adjudicator"] for item in catalog if item["adjudicator"]}),
        "categories": sorted({category for item in catalog for category in item["categories"]}),
        "years": sorted({item["year"] for item in catalog if item["year"]}, reverse=True),
        "decisionTypes": sorted({item["type"] for item in catalog if item["type"]}),
        "sourceStatuses": sorted({item["sourceStatus"] for item in catalog if item["sourceStatus"]}),
        "classificationConfidences": [value for value in ("גבוהה", "סבירה", "לבדיקה") if any(item["classificationConfidence"] == value for item in catalog)],
        "documentAvailabilities": sorted({item["documentAvailability"] for item in catalog if item["documentAvailability"]}),
        "focusRuleVersion": focus.get("version", ""),
        "focusCounts": focus.get("counts", {}),
        "records": catalog,
    }
    (data_dir / "catalog.json").write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    expected_search: set[str] = set()
    token_files = []
    for index, bucket in enumerate(token_index):
        name = f"token-{index:02d}.json"
        compact = {
            token: [[doc, sorted(bucket[token][doc])] for doc in sorted(bucket[token])]
            for token in sorted(bucket)
        }
        (search_dir / name).write_text(json.dumps(compact, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        expected_search.add(name)
        token_files.append(f"data/search/{name}")
    page_files = []
    for index, shard in enumerate(page_shards):
        name = f"pages-{index:02d}.json"
        (search_dir / name).write_text(json.dumps(shard, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        expected_search.add(name)
        page_files.append(f"data/search/{name}")
    evidence_files = []
    for index, shard in enumerate(evidence_shards):
        name = f"evidence-{index:02d}.json"
        (search_dir / name).write_text(json.dumps(shard, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        expected_search.add(name)
        evidence_files.append(f"data/search/{name}")
    clean_stale(search_dir, expected_search, "*.json")
    manifest = {
        "version": datetime.now().astimezone().isoformat(), "ruleVersion": pipeline.get("version", ""),
        "tokenBucketCount": TOKEN_BUCKETS, "pageShardCount": PAGE_SHARDS, "evidenceShardCount": EVIDENCE_SHARDS,
        "tokenFiles": token_files, "pageFiles": page_files,
        "evidenceFiles": evidence_files, "documentShards": doc_shards,
        "documentEvidenceShards": evidence_doc_shards, "documents": len(doc_shards),
    }
    (data_dir / "search-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    text_extensions = {".html", ".js", ".css", ".json", ".md", ".txt"}
    blocked = []
    for path in DIST.rglob("*"):
        if path.is_file() and path.suffix.lower() in text_extensions:
            value = normalize_text(path.read_text(encoding="utf-8", errors="ignore"))
            for term in BLOCKED_PUBLICATION_TERMS:
                if normalize_text(term) in value:
                    blocked.append({"file": str(path.relative_to(DIST)), "term": term})
    if blocked:
        raise RuntimeError("Publication safety scan failed: " + json.dumps(blocked, ensure_ascii=False))

    files = [path for path in DIST.rglob("*") if path.is_file()]
    oversized = [(str(path.relative_to(DIST)), path.stat().st_size) for path in files if path.stat().st_size > MAX_FILE_BYTES]
    if oversized:
        raise RuntimeError("Publication size scan failed; files exceed 90 MiB: " + json.dumps(oversized, ensure_ascii=False))
    dist_bytes = sum(path.stat().st_size for path in files)
    if dist_bytes > MAX_SITE_BYTES:
        raise RuntimeError(f"Publication size scan failed: {dist_bytes} bytes exceeds 850 MiB")
    linked = [str(path.relative_to(DIST)) for path in files if path.stat().st_nlink > 1]
    if linked:
        raise RuntimeError("Publication contains hard links: " + json.dumps(linked[:20], ensure_ascii=False))

    ashdod_official = sum(bool(item["sourceStatus"] == "רשמי" and item["ashdodRelation"]) for item in catalog)
    ashdod_reconstructed = sum(bool(item["sourceStatus"] == "משוחזר ממקור מאוחר" and item["ashdodRelation"]) for item in catalog)
    ashdod_leads = sum(bool(item["sourceStatus"] == "ליד מחקרי" and item["ashdodRelation"]) for item in catalog)
    if (ashdod_official, ashdod_reconstructed, ashdod_leads) != (58, 4, 1):
        raise RuntimeError(f"Ashdod validation failed: {(ashdod_official, ashdod_reconstructed, ashdod_leads)}")

    report = {
        "documents": len(catalog), "official": official_count, "external": len(catalog) - official_count,
        "fullText": len(doc_shards), "pdfs": len(expected_pdfs), "docx": len(expected_docx),
        "pdfBytes": copied_pdf_bytes, "docxBytes": copied_docx_bytes, "tokenBuckets": TOKEN_BUCKETS,
        "pageShards": PAGE_SHARDS, "evidenceShards": EVIDENCE_SHARDS,
        "focusRuleVersion": focus.get("version", ""), "focusCounts": focus.get("counts", {}),
        "distBytes": dist_bytes, "maxFileBytes": max(path.stat().st_size for path in files),
        "limits": {"siteBytes": MAX_SITE_BYTES, "fileBytes": MAX_FILE_BYTES},
        "ashdod": {"official": ashdod_official, "reconstructed": ashdod_reconstructed, "leads": ashdod_leads},
        "publicationSafety": {"privateTerms": list(BLOCKED_PUBLICATION_TERMS), "blockedMatches": 0},
    }
    (SITE_ROOT / "build-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    publish_staging()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
