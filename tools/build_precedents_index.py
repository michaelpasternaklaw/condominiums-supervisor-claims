#!/usr/bin/env python3
"""Build a local, evidence-backed index of authorities cited by supervisors.

The script is deliberately deterministic.  It reads the already-extracted text,
finds Israeli case citations, keeps a short page-level evidence window, merges
typographic variants and ranks authorities by their use across the corpus.  It
does not make network requests and does not send document text to a model.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path


SITE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SITE_ROOT.parents[2]
DATA_ROOT = PROJECT_ROOT / "outputs/legal_decisions_database"
DEFAULT_SOURCE = DATA_ROOT / "full_export/complete_export.csv"
DEFAULT_OUTPUT = DATA_ROOT / "precedents"

BIDI_RE = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
SPACE_RE = re.compile(r"\s+")

# Longer prefixes must precede shorter ones.  Text is typographically normalized
# before matching, so Hebrew gershayim are represented by an ASCII double quote.
PREFIX_PATTERN = r"""
    דנג"ץ|דנ"א|דנ"פ|רע"א|רע"פ|עע"מ|עע"ם|בר"מ|בג"ץ|עמ"ש|רמ"ש|
    עת"מ|עש"א|עמ"נ|ע"א|ע"פ|ע"ש|ע"ר|ר"ע|תפ"ח|ת"א|ת"ק|ת"פ|ה"פ|
    דנגץ|דנא|דנפ|רעא|רעפ|עעמ|ברמ|בגץ|עמש|רמש|עתמ|עשא|עמנ|עא|עפ|עש|ער|רע|תפח|תא|תק|תפ|הפ|
    תיק\s*(?:\([^\n)]{1,45}\))?|ת['.] 
""".replace("\n", "").replace(" ", "")

NUMBER_PATTERN = r"\d{1,6}\s*[-/]\s*\d{1,5}(?:\s*[-/]\s*\d{2,4})?"
CITATION_RE = re.compile(
    rf"(?<![\w\u0590-\u05ff])(?:\d{{1,2}})?(?:[בלכמ])?(?P<prefix>{PREFIX_PATTERN})"
    rf"(?:\s*\([^\n)]{{1,40}}\))?\s*(?:מס(?:פר|['.])?\s*)?"
    rf"(?P<number>{NUMBER_PATTERN})(?!\d)",
    re.IGNORECASE | re.VERBOSE,
)
NEXT_CITATION_RE = re.compile(
    rf"(?:\d{{1,2}})?(?:[בלכמ])?(?:{PREFIX_PATTERN})(?:\s*\([^\n)]{{1,40}}\))?\s*(?:מס(?:פר|['.])?\s*)?{NUMBER_PATTERN}",
    re.IGNORECASE | re.VERBOSE,
)

# A citation sometimes loses its proceeding prefix during PDF extraction.  These
# are accepted only when the number is immediately followed by named opposing
# parties and the nearby text contains a case-law cue.
BARE_CASE_RE = re.compile(
    rf"(?<!\d)(?P<number>\d{{3,6}}\s*/\s*\d{{2,4}})(?!\d)"
    rf"(?P<tail>\s*[,:;]?\s*[^\n]{{0,120}}?\s+נ(?:גד|['\"])?\s+[^\n]{{2,100}})",
    re.IGNORECASE,
)

CASE_CUES = re.compile(r"(?:ראו|ראה|השוו|עניין|הלכת|נפסק|נקבע|פסק\s+דין|אסמכת)")
PARTIES_RE = re.compile(
    r"(?P<parties>[\u0590-\u05ffA-Za-z0-9 .,'\"()\-]{2,100}?\s+נ(?:גד|['\"])?\s+"
    r"[\u0590-\u05ffA-Za-z0-9 .,'\"()\-]{2,110}?)"
    r"(?=\s*(?:\(|\[|\{|,|;|תק-|פד|נבו|מיום|$))",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"(?<!\d)([0-3]?\d[./-][01]?\d[./-](?:19|20)\d{2})(?!\d)")

PREFIX_MAP = {
    "דנגץ": 'דנג"ץ', "דנא": 'דנ"א', "דנפ": 'דנ"פ', "רעא": 'רע"א',
    "רעפ": 'רע"פ', "עעמ": 'עע"מ', "ברמ": 'בר"מ', "בגץ": 'בג"ץ',
    "עמש": 'עמ"ש', "רמש": 'רמ"ש', "עתמ": 'עת"מ', "עשא": 'עש"א',
    "עמנ": 'עמ"נ', "עא": 'ע"א', "עפ": 'ע"פ', "עש": 'ע"ש', "ער": 'ע"ר',
    "רע": 'ר"ע', "תפח": 'תפ"ח', "תא": 'ת"א', "תק": 'ת"ק', "תפ": 'ת"פ',
    "הפ": 'ה"פ', "ת": "תיק",
}

SUPREME_PREFIXES = {'דנג"ץ', 'דנ"א', 'דנ"פ', 'רע"א', 'רע"פ', 'עע"מ', 'בר"מ', 'בג"ץ'}
DISTRICT_PREFIXES = {'עמ"ש', 'רמ"ש', 'עת"מ', 'עש"א', 'עמ"נ'}
TRIAL_PREFIXES = {'ת"א', 'ת"ק', 'ת"פ', 'תפ"ח', 'ה"פ'}

TOPIC_TERMS = {
    "רכוש משותף": ("רכוש משותף", "רכוש המשותף"),
    "הצמדות": ("הצמדה", "הצמדות", "מוצמד"),
    "שימוש ייחודי": ("שימוש ייחודי", "שימוש יחודי", "החזקה ייחודית", "חזקה ייחודית", "החזקה בלעדית"),
    "סילוק יד והסגת גבול": ("סילוק יד", "הסגת גבול", "פלישה"),
    "חצרות וגינות": ("חצר", "גינה"),
    "גגות ופרגולות": ("גג", "גגון", "פרגולה", "מצללה"),
    "הרחבת דירה ושינוי": ("הרחבת דירה", "הרחבה", "שינוי ברכוש"),
    "רישיון והרשאה": ("רישיון מכללא", "רשיון מכללא", "הרשאה", "בר רשות"),
    "שיהוי והתיישנות": ("שיהוי", "התיישנות"),
    "דמי שימוש ופיצויים": ("דמי שימוש", "פיצויים", "פיצוי"),
    "סמכות ופיצול סעדים": ("סמכות המפקח", "סמכות עניינית", "פיצול סעדים", "חוסר סמכות"),
    "תקנון ונציגות": ("תקנון מוסכם", "נציגות הבית", "נציגות"),
    "הוצאות תחזוקה": ("הוצאות תחזוקה", "הוצאות החזקה", "אחזקה", "תחזוקה"),
    "מים וניקוז": ("רטיבות", "נזילה", "מים", "צנרת", "מרזב", "ניקוז"),
    "מזגנים ורעש": ("מזגן", "מיזוג", "רעש", "מטרד"),
    "חום ורטט": ("רטט", "פליטת חום", "אוויר חם", "עומס חום", "טמפרטורה"),
    "מצלמות ופרטיות": ("מצלמה", "מצלמות", "פרטיות"),
    "האזנת סתר": ("האזנת סתר", "הקלטת קול", "הקלטת שמע", "מיקרופון"),
    "יונים ומפגעים": ("יונה", "יונים", "לשלשת יונים", "רשת יונים", "דוקרנים"),
    "מניעת כפל": ("מניעת כפל", "כפל פיצוי", "פיצוי כפול", "כפל סעדים", "כפל סעד", "כפל תרופה"),
    "תמ״א 38 והתחדשות": ("תמא 38", 'תמ"א 38', "התחדשות עירונית"),
    "סעיף 55": ("סעיף 55", "ס' 55"),
    "סעיף 58": ("סעיף 58", "ס' 58"),
    "סעיף 59": ("סעיף 59", "ס' 59"),
    "סעיף 72": ("סעיף 72", "ס' 72"),
}

FOCUS_TOPICS = {
    "רכוש משותף", "שימוש ייחודי", "סילוק יד והסגת גבול", "גגות ופרגולות",
    "דמי שימוש ופיצויים", "סמכות ופיצול סעדים", "מים וניקוז", "מזגנים ורעש",
    "חום ורטט", "מצלמות ופרטיות", "האזנת סתר", "יונים ומפגעים", "מניעת כפל",
    "סעיף 55", "סעיף 58", "סעיף 59", "סעיף 72",
}


@dataclass
class Mention:
    citation_key: str
    proceeding_type: str
    cited_case_number: str
    normalized_number: str
    location_hint: str
    title_candidate: str
    cited_date_candidate: str
    source_document_id: str
    source_case_number: str
    source_date: str
    source_office: str
    source_supervisor: str
    source_city: str
    source_url: str
    page_number: int
    section_role: str
    citation_cue: str
    topics: str
    snippet: str


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def clean_text(value: str) -> str:
    value = unicodedata.normalize("NFC", value or "")
    value = BIDI_RE.sub("", value)
    value = value.replace("״", '"').replace("׳", "'").replace("’", "'")
    value = value.replace("–", "-").replace("—", "-").replace("־", "-")
    return value


def compact(value: str) -> str:
    return SPACE_RE.sub(" ", clean_text(value)).strip()


def split_pages(text: str) -> list[tuple[int, str]]:
    marker = re.compile(r"=== PDF PAGE (\d+) ===")
    matches = list(marker.finditer(text))
    if matches:
        return [
            (int(match.group(1)), text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)])
            for index, match in enumerate(matches)
        ]
    pages = text.split("\f")
    return [(index, page) for index, page in enumerate(pages, 1) if page.strip()] or [(1, text)]


def normalize_number(number: str) -> tuple[str, str]:
    raw = re.sub(r"\s+", "", number)
    parts = re.split(r"[-/]", raw)
    normalized_parts = [str(int(part)) for part in parts]
    # OCR occasionally reverses traditional citations (93/7112 instead of
    # 7112/93). A four-or-more-digit docket followed by a two-digit year is
    # the canonical Israeli form.
    if len(normalized_parts) == 2 and int(normalized_parts[0]) <= 99 and int(normalized_parts[1]) >= 1000:
        normalized_parts.reverse()
    normalized = "|".join(normalized_parts)
    separator = "-" if "-" in raw and len(parts) == 3 else "/"
    display_parts = list(normalized_parts)
    if len(display_parts) == 2 and len(display_parts[1]) < 2:
        display_parts[1] = display_parts[1].zfill(2)
    if len(display_parts) == 3:
        display_parts[1:] = [part.zfill(2) if len(part) < 2 else part for part in display_parts[1:]]
    display = separator.join(display_parts)
    return normalized, display


def normalize_prefix(prefix: str) -> tuple[str, str]:
    prefix = compact(prefix)
    location = ""
    if prefix.startswith("תיק"):
        location_match = re.search(r"\(([^)]+)\)", prefix)
        location = compact(location_match.group(1)) if location_match else ""
        return "תיק מפקח" if location else "תיק", location
    key = re.sub(r"[^\u0590-\u05ff]", "", prefix)
    return PREFIX_MAP.get(key, prefix), location


def section_role(page: str, start: int) -> str:
    context = compact(page[max(0, start - 500):start + 80])
    if re.search(r"(?:סוף דבר|אשר על כן|לאור כל האמור|התביעה (?:מתקבלת|נדחית))", context):
        return "הכרעה אופרטיבית"
    if re.search(r"(?:אני קובע|אני קובעת|הלכה|נקבע|נפסק|קבע בית המשפט|דיון והכרעה)", context):
        return "דיון או קביעה"
    if re.search(r"(?:לטענת|טוענ|הפנה|הפנתה|לשיטת)", context):
        return "טענת צד"
    if re.search(r"(?:רקע|העובדות|פתח דבר)", context):
        return "רקע"
    return "דיון"


def cue_for(page: str, start: int) -> str:
    before = compact(page[max(0, start - 130):start])
    cues = (
        ("הלכה מחייבת", r"הלכה|הלכת"),
        ("קביעה מצוטטת", r"נקבע|נפסק|קבע(?:ה)? בית המשפט|פסק בית המשפט"),
        ("הסתמכות", r"הסתמכ|בהתאם|מכוח"),
        ("השוואה", r"השוו"),
        ("הפניה", r"ראו|ראה|עיין|עניין"),
        ("טענת צד", r"לטענת|הפנה|הפנתה|טוענ"),
    )
    for label, pattern in cues:
        if re.search(pattern, before):
            return label
    return "אזכור"


def extract_title(text: str, end: int) -> str:
    after = compact(text[end:end + 260])
    after = re.sub(r"^[\s,:;\-\[\]{}()]+", "", after)
    match = PARTIES_RE.search(after)
    if not match or match.start() > 45:
        return ""
    title = compact(match.group("parties"))
    title = re.sub(r"^(?:ראו|ראה|השוו|עניין|בעניין)\s*[:,-]?\s*", "", title)
    title = re.sub(r"^\d{1,4}(?=[\u0590-\u05ff])", "", title)
    title = re.split(r"\s+(?:פ[\"']?ד(?:י)?|תק-(?:על|מח|של)|נבו)\b", title, maxsplit=1)[0]
    title = re.sub(r"\s+נ\s*[.]\s*", " נ' ", title)
    return title[:180].strip(" ,;:-()[]{}")


def extract_date(text: str, end: int) -> str:
    tail = clean_text(text[end:end + 240])
    # Do not borrow the date of the next authority in a citation string.
    next_citation = NEXT_CITATION_RE.search(tail)
    if next_citation:
        tail = tail[:next_citation.start()]
    match = DATE_RE.search(compact(tail))
    return match.group(1) if match else ""


def snippet_around(page: str, start: int, end: int, radius: int = 260) -> str:
    left = max(0, start - radius)
    right = min(len(page), end + radius)
    snippet = compact(page[left:right])
    return ("…" if left else "") + snippet + ("…" if right < len(page) else "")


def detect_topics(snippet: str, source_tags: str) -> list[str]:
    # Nearby text is intentionally preferred over document-wide tags. A
    # broadly tagged decision may cite a precedent for one narrow proposition.
    probe = compact(snippet).casefold()
    topics = []
    for topic, terms in TOPIC_TERMS.items():
        matched = False
        for term in terms:
            normalized_term = compact(term).casefold()
            if " " in normalized_term:
                matched = normalized_term in probe
            else:
                matched = bool(re.search(
                    rf"(?<![\u0590-\u05ff])(?:[בלכמוהש])?{re.escape(normalized_term)}(?![\u0590-\u05ff])",
                    probe,
                ))
            if matched:
                break
        if matched:
            topics.append(topic)
    return topics


def is_self_citation(source_case: str, normalized_number: str) -> bool:
    try:
        own, _ = normalize_number(source_case)
    except (ValueError, TypeError):
        return False
    return own == normalized_number


def make_mention(
    row: dict[str, str], page_number: int, page: str, start: int, end: int,
    raw_prefix: str, raw_number: str,
) -> Mention | None:
    try:
        normalized_number, display_number = normalize_number(raw_number)
    except ValueError:
        return None
    proceeding_type, location = normalize_prefix(raw_prefix)
    if is_self_citation(row.get("מספר תיק", ""), normalized_number):
        return None
    title = extract_title(page, end)
    snippet = snippet_around(page, start, end)
    topics = detect_topics(snippet, row.get("תגיות נושא", ""))
    key = f"{proceeding_type}|{normalized_number}"
    return Mention(
        citation_key=key,
        proceeding_type=proceeding_type,
        cited_case_number=display_number,
        normalized_number=normalized_number,
        location_hint=location,
        title_candidate=title,
        cited_date_candidate=extract_date(page, end),
        source_document_id=row.get("מזהה רשומה", ""),
        source_case_number=row.get("מספר תיק", ""),
        source_date=row.get("תאריך", ""),
        source_office=row.get("לשכה", ""),
        source_supervisor=row.get("מפקח/ת", ""),
        source_city=row.get("עיר", ""),
        source_url=row.get("קישור מקור") or row.get("קישור PDF", ""),
        page_number=page_number,
        section_role=section_role(page, start),
        citation_cue=cue_for(page, start),
        topics="; ".join(topics),
        snippet=snippet,
    )


def scan_document(row: dict[str, str], include_bare: bool = False) -> list[Mention]:
    text_path = Path(row.get("טקסט מקומי", ""))
    if not text_path.is_file():
        return []
    text = clean_text(text_path.read_text(encoding="utf-8", errors="replace"))
    found: list[Mention] = []
    occupied: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for page_number, page in split_pages(text):
        for match in CITATION_RE.finditer(page):
            mention = make_mention(
                row, page_number, page, match.start(), match.end(),
                match.group("prefix"), match.group("number"),
            )
            if mention:
                found.append(mention)
                occupied[page_number].append(match.span())
        if include_bare:
            for match in BARE_CASE_RE.finditer(page):
                if any(not (match.end() <= start or match.start() >= end) for start, end in occupied[page_number]):
                    continue
                if not CASE_CUES.search(compact(page[max(0, match.start() - 120):match.start()])):
                    continue
                mention = make_mention(
                    row, page_number, page, match.start(), match.end(),
                    "מספר ללא סוג הליך", match.group("number"),
                )
                if mention:
                    found.append(mention)
    # PDF extraction can duplicate the same printed line.  Keep one identical
    # evidence item per source decision and page.
    unique = {}
    for mention in found:
        dedupe_key = (mention.citation_key, mention.source_document_id, mention.page_number, mention.snippet)
        unique[dedupe_key] = mention
    return list(unique.values())


def title_tokens(value: str) -> set[str]:
    return {
        token for token in re.findall(r"[\u0590-\u05ffA-Za-z]{2,}", compact(value).casefold())
        if token not in {"נגד", "ואח", "בעמ", "חברה", "נציגות", "הבית", "המשותף"}
    }


def canonicalize_mentions(mentions: list[Mention]) -> None:
    """Merge safe prefix omissions and obvious OCR prefix variants in place."""
    by_number: dict[str, dict[str, list[Mention]]] = defaultdict(lambda: defaultdict(list))
    for item in mentions:
        by_number[item.normalized_number][item.citation_key].append(item)
    for groups in by_number.values():
        known = {key: items for key, items in groups.items() if items[0].proceeding_type != "מספר ללא סוג הליך"}
        bare = [item for key, items in groups.items() if items[0].proceeding_type == "מספר ללא סוג הליך" for item in items]
        if len(known) == 1:
            canonical_key, canonical_items = next(iter(known.items()))
            canonical_type = canonical_items[0].proceeding_type
            for item in bare:
                item.citation_key = canonical_key
                item.proceeding_type = canonical_type


def court_level(proceeding_type: str, locations: set[str], snippets: list[str]) -> tuple[str, int]:
    text = " ".join(snippets[:20])
    if proceeding_type in SUPREME_PREFIXES or "בית המשפט העליון" in text:
        return "בית המשפט העליון", 12
    if proceeding_type in DISTRICT_PREFIXES or "בית המשפט המחוזי" in text:
        return "בית המשפט המחוזי", 8
    if proceeding_type == "תיק מפקח":
        return "המפקח על רישום מקרקעין", 7
    if proceeding_type in TRIAL_PREFIXES or "בית משפט השלום" in text:
        return "בית משפט דיוני", 4
    if proceeding_type == 'ע"א' and re.search(r"פ[\"']?ד(?:י)?\b", text):
        return "בית המשפט העליון", 12
    if proceeding_type in {'ע"א', 'ע"פ', 'ע"ש', 'ע"ר', 'ר"ע'}:
        return "ערכאת ערעור — דרוש אימות", 6
    return "לא זוהה — דרוש אימות", 2


def best_candidate(values: list[str]) -> str:
    values = [compact(value) for value in values if compact(value)]
    if not values:
        return ""
    counts = Counter(values)
    return max(counts, key=lambda value: (counts[value], len(value)))


def best_date_candidate(values: list[str]) -> str:
    all_values = [compact(value) for value in values]
    nonempty = [value for value in all_values if value]
    if not nonempty:
        return ""
    candidate, count = Counter(nonempty).most_common(1)[0]
    # A date is published only when it is attached consistently to this
    # authority, not merely found after it in a string of several citations.
    return candidate if count / len(all_values) >= 0.5 else ""


def rank_precedents(mentions: list[Mention]) -> list[dict]:
    grouped: dict[str, list[Mention]] = defaultdict(list)
    for mention in mentions:
        grouped[mention.citation_key].append(mention)
    rows = []
    for key, group in grouped.items():
        documents = {item.source_document_id for item in group}
        offices = sorted({item.source_office for item in group if item.source_office})
        supervisors = sorted({item.source_supervisor for item in group if item.source_supervisor})
        locations = {item.location_hint for item in group if item.location_hint}
        topics = sorted({topic.strip() for item in group for topic in item.topics.split(";") if topic.strip()})
        roles = Counter(item.section_role for item in group)
        cues = Counter(item.citation_cue for item in group)
        level, authority_weight = court_level(group[0].proceeding_type, locations, [item.snippet for item in group])
        analytical_docs = {
            item.source_document_id for item in group
            if item.section_role in {"דיון או קביעה", "הכרעה אופרטיבית"}
            or item.citation_cue in {"הלכה מחייבת", "קביעה מצוטטת", "הסתמכות"}
        }
        distinct_count = len(documents)
        score = round(
            distinct_count * 10
            + min(10, len(group))
            + len(analytical_docs) * 4
            + authority_weight
            + min(8, len(topics) * 2)
            + min(4, len(offices)),
            1,
        )
        if distinct_count >= 10 or (distinct_count >= 5 and authority_weight >= 8):
            tier = "מרכזית"
        elif distinct_count >= 3 or (distinct_count >= 2 and authority_weight >= 10) or (distinct_count >= 2 and analytical_docs and authority_weight >= 7):
            tier = "חשובה"
        elif distinct_count >= 2 or analytical_docs:
            tier = "שימושית"
        else:
            tier = "לבדיקה"
        title = best_candidate([item.title_candidate for item in group])
        cited_date = best_date_candidate([item.cited_date_candidate for item in group])
        best_example = max(
            group,
            key=lambda item: (
                item.section_role in {"דיון או קביעה", "הכרעה אופרטיבית"},
                item.citation_cue in {"הלכה מחייבת", "קביעה מצוטטת", "הסתמכות"},
                len(item.title_candidate),
            ),
        )
        reason = f"מצוטטת ב-{distinct_count} החלטות מפקח"
        if analytical_docs:
            reason += f", מהן {len(analytical_docs)} בדיון/קביעה או בהסתמכות"
        if level != "לא זוהה — דרוש אימות":
            reason += f"; {level}"
        if topics:
            reason += "; נושאים: " + ", ".join(topics[:4])
        years = sorted(item.source_date for item in group if item.source_date)
        verification = "מוכן לבדיקה אנושית" if title else "חסר שם הליך — לבדיקה"
        procedural_pattern = re.compile(r"(?:הוצאות|שכר טרחה|ראיות|נטל|סדר דין|התיישנות|שיהוי)")
        procedural_mentions = sum(bool(procedural_pattern.search(item.snippet)) for item in group)
        if procedural_mentions / len(group) >= 0.5:
            nature = "דיונית או ראייתית"
        elif any(topic in topics for topic in (
            "רכוש משותף", "הצמדות", "שימוש ייחודי", "חצרות וגינות", "גגות ופרגולות",
            "מים וניקוז", "מזגנים ורעש", "חום ורטט", "מצלמות ופרטיות",
            "האזנת סתר", "יונים ומפגעים", "מניעת כפל",
        )):
            nature = "מהותית לבתים משותפים"
        elif any(topic in topics for topic in ("סמכות ופיצול סעדים", "סעיף 72")):
            nature = "סמכות וסעדים"
        elif procedural_mentions:
            nature = "דיונית או ראייתית"
        else:
            nature = "כללית — לבדיקה"
        rows.append({
            "מפתח אסמכתה": key,
            "סוג הליך": group[0].proceeding_type,
            "מספר הליך": group[0].cited_case_number,
            "שם הליך שחולץ": title,
            "תאריך הליך שחולץ": cited_date,
            "ערכאה משוערת": level,
            "רמז מקום": "; ".join(sorted(locations)),
            "מספר החלטות מפקח מצטטות": distinct_count,
            "מספר אזכורים": len(group),
            "מספר החלטות עם שימוש אנליטי": len(analytical_docs),
            "לשכות מצטטות": "; ".join(offices),
            "מפקחים מצטטים": "; ".join(supervisors),
            "נושאים": "; ".join(topics),
            "אופי אסמכתה": nature,
            "תפקידי אזכור": "; ".join(f"{name}: {count}" for name, count in roles.most_common()),
            "סוגי שימוש": "; ".join(f"{name}: {count}" for name, count in cues.most_common()),
            "ציון חשיבות": score,
            "דרגת חשיבות": tier,
            "סיבת דירוג": reason,
            "החלטה מצטטת לדוגמה": best_example.source_case_number,
            "מזהה החלטה לדוגמה": best_example.source_document_id,
            "עמוד לדוגמה": best_example.page_number,
            "קטע לדוגמה": best_example.snippet,
            "קישור להחלטה המצטטת": best_example.source_url,
            "אזכור ראשון בקורפוס": years[0] if years else "",
            "אזכור אחרון בקורפוס": years[-1] if years else "",
            "מצב אימות": verification,
        })
    rows.sort(key=lambda row: (-float(row["ציון חשיבות"]), -int(row["מספר החלטות מפקח מצטטות"]), row["מפתח אסמכתה"]))
    return rows


def write_csv(path: Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_sqlite(path: Path, precedents: list[dict], mentions: list[Mention]) -> None:
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    with db:
        db.executescript("""
        CREATE TABLE precedents (
          citation_key TEXT PRIMARY KEY, proceeding_type TEXT, case_number TEXT, title TEXT,
          cited_date TEXT, court_level TEXT, location_hint TEXT, citing_decisions INTEGER,
          mentions INTEGER, analytical_decisions INTEGER, citing_offices TEXT,
          citing_supervisors TEXT, topics TEXT, authority_nature TEXT, citation_roles TEXT, citation_uses TEXT,
          importance_score REAL, importance_tier TEXT, ranking_reason TEXT,
          example_source_case TEXT, example_source_id TEXT, example_page INTEGER,
          example_snippet TEXT, example_source_url TEXT, first_corpus_citation TEXT,
          last_corpus_citation TEXT, verification_status TEXT
        );
        CREATE TABLE citation_mentions (
          id INTEGER PRIMARY KEY, citation_key TEXT NOT NULL REFERENCES precedents(citation_key),
          proceeding_type TEXT, cited_case_number TEXT, normalized_number TEXT,
          location_hint TEXT, title_candidate TEXT, cited_date_candidate TEXT,
          source_document_id TEXT, source_case_number TEXT, source_date TEXT,
          source_office TEXT, source_supervisor TEXT, source_city TEXT, source_url TEXT,
          page_number INTEGER, section_role TEXT, citation_cue TEXT, topics TEXT, snippet TEXT
        );
        CREATE INDEX citation_mentions_key_idx ON citation_mentions(citation_key);
        CREATE INDEX citation_mentions_source_idx ON citation_mentions(source_document_id,page_number);
        CREATE VIRTUAL TABLE precedent_fts USING fts5(citation_key UNINDEXED, title, topics, snippets, tokenize='unicode61');
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
        """)
        columns = [
            "מפתח אסמכתה", "סוג הליך", "מספר הליך", "שם הליך שחולץ", "תאריך הליך שחולץ",
            "ערכאה משוערת", "רמז מקום", "מספר החלטות מפקח מצטטות", "מספר אזכורים",
            "מספר החלטות עם שימוש אנליטי", "לשכות מצטטות", "מפקחים מצטטים", "נושאים",
            "אופי אסמכתה", "תפקידי אזכור", "סוגי שימוש", "ציון חשיבות", "דרגת חשיבות", "סיבת דירוג",
            "החלטה מצטטת לדוגמה", "מזהה החלטה לדוגמה", "עמוד לדוגמה", "קטע לדוגמה",
            "קישור להחלטה המצטטת", "אזכור ראשון בקורפוס", "אזכור אחרון בקורפוס", "מצב אימות",
        ]
        placeholders = ",".join("?" for _ in columns)
        db.executemany(f"INSERT INTO precedents VALUES({placeholders})", ([row[column] for column in columns] for row in precedents))
        mention_columns = list(Mention.__dataclass_fields__)
        db.executemany(
            f"INSERT INTO citation_mentions({','.join(mention_columns)}) VALUES({','.join('?' for _ in mention_columns)})",
            ([getattr(item, column) for column in mention_columns] for item in mentions),
        )
        snippets_by_key = defaultdict(list)
        for item in mentions:
            snippets_by_key[item.citation_key].append(item.snippet)
        db.executemany(
            "INSERT INTO precedent_fts VALUES(?,?,?,?)",
            ((row["מפתח אסמכתה"], row["שם הליך שחולץ"], row["נושאים"], " ".join(snippets_by_key[row["מפתח אסמכתה"]])) for row in precedents),
        )
        db.executemany("INSERT INTO metadata VALUES(?,?)", (
            ("generated_at", datetime.now().astimezone().isoformat()),
            ("source_documents", str(len({item.source_document_id for item in mentions}))),
            ("precedents", str(len(precedents))),
            ("mentions", str(len(mentions))),
            ("method", "deterministic-local-regex-v1"),
        ))
    db.close()


def build(source: Path, output: Path, include_bare: bool = False) -> dict:
    rows = read_csv(source)
    mentions: list[Mention] = []
    scanned = 0
    missing_text = 0
    for row in rows:
        text_path = Path(row.get("טקסט מקומי", ""))
        if not text_path.is_file():
            missing_text += 1
            continue
        scanned += 1
        mentions.extend(scan_document(row, include_bare=include_bare))
    canonicalize_mentions(mentions)
    precedents = rank_precedents(mentions)
    output.mkdir(parents=True, exist_ok=True)
    mention_rows = [asdict(item) for item in mentions]
    mention_rows.sort(key=lambda row: (row["citation_key"], row["source_date"], row["source_document_id"], row["page_number"]))
    write_csv(output / "precedents_index.csv", precedents)
    write_csv(output / "precedent_citations.csv", mention_rows, list(Mention.__dataclass_fields__))
    review = [row for row in precedents if row["דרגת חשיבות"] != "לבדיקה" and (not row["שם הליך שחולץ"] or "דרוש אימות" in row["ערכאה משוערת"])]
    write_csv(output / "precedents_review_queue.csv", review, list(precedents[0]) if precedents else [])
    mentions_by_key: dict[str, list[Mention]] = defaultdict(list)
    for item in mentions:
        mentions_by_key[item.citation_key].append(item)

    def citing_decisions(row: dict) -> list[dict]:
        """Keep one best evidence item per citing decision to prevent duplication."""
        best: dict[str, Mention] = {}
        for item in mentions_by_key[row["מפתח אסמכתה"]]:
            prior = best.get(item.source_document_id)
            item_rank = (
                item.section_role in {"דיון או קביעה", "הכרעה אופרטיבית"},
                item.citation_cue in {"הלכה מחייבת", "קביעה מצוטטת", "הסתמכות"},
                len(item.snippet),
            )
            prior_rank = (-1, -1, -1) if prior is None else (
                prior.section_role in {"דיון או קביעה", "הכרעה אופרטיבית"},
                prior.citation_cue in {"הלכה מחייבת", "קביעה מצוטטת", "הסתמכות"},
                len(prior.snippet),
            )
            if item_rank > prior_rank:
                best[item.source_document_id] = item
        return [{
            "sourceId": item.source_document_id, "sourceCase": item.source_case_number,
            "sourceDate": item.source_date, "office": item.source_office,
            "page": item.page_number, "sectionRole": item.section_role,
            "citationCue": item.citation_cue, "snippet": item.snippet,
            "sourceUrl": item.source_url,
        } for item in sorted(best.values(), key=lambda value: (value.source_date, value.source_document_id), reverse=True)]

    public_rows = [{
        "key": row["מפתח אסמכתה"], "type": row["סוג הליך"], "number": row["מספר הליך"],
        "title": row["שם הליך שחולץ"], "date": row["תאריך הליך שחולץ"],
        "court": row["ערכאה משוערת"], "citingDecisions": row["מספר החלטות מפקח מצטטות"],
        "mentions": row["מספר אזכורים"], "analyticalDecisions": row["מספר החלטות עם שימוש אנליטי"],
        "topics": [item.strip() for item in row["נושאים"].split(";") if item.strip()],
        "authorityNature": row["אופי אסמכתה"],
        "score": row["ציון חשיבות"], "importance": row["דרגת חשיבות"],
        "reason": row["סיבת דירוג"], "example": {
            "sourceId": row["מזהה החלטה לדוגמה"], "sourceCase": row["החלטה מצטטת לדוגמה"],
            "page": row["עמוד לדוגמה"], "snippet": row["קטע לדוגמה"],
            "sourceUrl": row["קישור להחלטה המצטטת"],
        }, "verificationStatus": row["מצב אימות"],
        "citingDecisionEvidence": citing_decisions(row),
    } for row in precedents]
    payload = {
        "version": 1,
        "generatedAt": datetime.now().astimezone().isoformat(),
        "method": "deterministic-local-regex-v1",
        "includeBareNumbers": include_bare,
        "counts": {"sourceRows": len(rows), "scannedDocuments": scanned, "missingText": missing_text,
                   "precedents": len(precedents), "mentions": len(mentions)},
        "precedents": public_rows,
    }
    (output / "precedents_index.json").write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    focused_rows = [row for row in precedents if row["דרגת חשיבות"] in {"מרכזית", "חשובה"}
                    and FOCUS_TOPICS.intersection(item.strip() for item in row["נושאים"].split(";") if item.strip())]
    write_csv(output / "precedents_focus.csv", focused_rows, list(precedents[0]) if precedents else [])
    public_by_key = {row["key"]: row for row in public_rows}
    focused_public = [public_by_key[row["מפתח אסמכתה"]] for row in focused_rows]
    topic_counts = Counter(topic for row in focused_public for topic in row["topics"] if topic in FOCUS_TOPICS)
    focus_payload = {
        "version": 1, "generatedAt": payload["generatedAt"],
        "method": "deterministic-local-regex-v1-deduped-by-citation-and-source",
        "counts": {"precedents": len(focused_public), "topics": dict(sorted(topic_counts.items()))},
        "focusTopics": sorted(FOCUS_TOPICS), "precedents": focused_public,
    }
    (output / "precedents_focus.json").write_text(json.dumps(focus_payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    write_sqlite(output / "precedents.sqlite", precedents, mentions)
    summary = {
        **payload["counts"],
        "importance": dict(Counter(row["דרגת חשיבות"] for row in precedents)),
        "reviewQueue": len(review),
        "focusedImportantPrecedents": len(focused_rows),
        "focusedTopicCounts": dict(sorted(topic_counts.items())),
        "topPrecedents": [{key: row[key] for key in (
            "סוג הליך", "מספר הליך", "שם הליך שחולץ", "ערכאה משוערת",
            "מספר החלטות מפקח מצטטות", "מספר אזכורים", "ציון חשיבות", "דרגת חשיבות",
        )} for row in precedents[:30]],
        "files": {
            "indexCsv": str(output / "precedents_index.csv"),
            "mentionsCsv": str(output / "precedent_citations.csv"),
            "reviewCsv": str(output / "precedents_review_queue.csv"),
            "json": str(output / "precedents_index.json"),
            "focusCsv": str(output / "precedents_focus.csv"),
            "focusJson": str(output / "precedents_focus.json"),
            "sqlite": str(output / "precedents.sqlite"),
        },
    }
    (output / "precedents_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a local index of cases cited by land-registration supervisors")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--include-bare", action="store_true", help="Include lower-confidence case numbers without a proceeding prefix")
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output, include_bare=args.include_bare), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
