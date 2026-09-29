#!/usr/bin/env python3
"""Create editable DOCX copies from the locally extracted public decisions.

The Ministry of Justice ``TabuSrc`` links are labelled DOCX in the API, but the
sample already downloaded locally contains PDF bytes.  This exporter therefore
accepts an original file only after strict OOXML validation and otherwise builds
an explicitly labelled converted copy from the existing page-separated text.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import re
import zipfile
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


SITE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SITE_ROOT.parents[2]
DATABASE_ROOT = PROJECT_ROOT / "outputs/legal_decisions_database"
EXPORT_CSV = DATABASE_ROOT / "full_export/complete_export.csv"
DEFAULT_OUTPUT = DATABASE_ROOT / "full_export/docx"
DEFAULT_MANIFEST = DATABASE_ROOT / "full_export/docx_export.csv"
ASHDOD_DOWNLOADS = DATABASE_ROOT / "ashdod/official_documents"


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def safe(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-zא-ת._-]+", "-", value or "").strip("-.")
    return value[:100] or "decision"


def clean(value: object) -> str:
    return " ".join(str(value or "").replace("\u200f", "").replace("\u200e", "").split())


def xml_safe(value: str) -> str:
    return "".join(char for char in value if char in "\t\n\r" or 0x20 <= ord(char) <= 0xD7FF or 0xE000 <= ord(char) <= 0xFFFD)


def split_pages(text: str) -> list[tuple[int, str]]:
    matches = list(re.finditer(r"=== PDF PAGE (\d+) ===", text))
    if matches:
        return [(int(match.group(1)), text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)].strip())
                for index, match in enumerate(matches)]
    parts = text.split("\f")
    return [(index, part.strip()) for index, part in enumerate(parts, 1) if part.strip()] or [(1, text.strip())]


def is_real_docx(path: Path) -> bool:
    if not path.is_file() or not zipfile.is_zipfile(path):
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            return "[Content_Types].xml" in names and "word/document.xml" in names
    except (OSError, zipfile.BadZipFile):
        return False


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def set_rtl(paragraph) -> None:
    properties = paragraph._p.get_or_add_pPr()
    bidi = properties.find(qn("w:bidi"))
    if bidi is None:
        properties.append(OxmlElement("w:bidi"))
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT


def remove_paragraph_border(paragraph_or_style) -> None:
    properties = paragraph_or_style._element.get_or_add_pPr()
    border = properties.find(qn("w:pBdr"))
    if border is not None:
        properties.remove(border)


def set_font(run, size: float, bold: bool = False, color: RGBColor | None = None) -> None:
    run.font.name = "Arial"
    run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), "Arial")
    run._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), "Arial")
    run._element.get_or_add_rPr().rFonts.set(qn("w:cs"), "Arial")
    run.font.size = Pt(size)
    run.bold = bold
    if color:
        run.font.color.rgb = color


def configure_document(document: Document) -> None:
    section = document.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(0.65)
    section.bottom_margin = Inches(0.65)
    section.left_margin = Inches(0.75)
    section.right_margin = Inches(0.75)
    normal = document.styles["Normal"]
    normal.font.name = "Arial"
    normal._element.rPr.rFonts.set(qn("w:ascii"), "Arial")
    normal._element.rPr.rFonts.set(qn("w:hAnsi"), "Arial")
    normal._element.rPr.rFonts.set(qn("w:cs"), "Arial")
    normal.font.size = Pt(10.5)
    title = document.styles["Title"]
    title.font.name = "Arial"
    title._element.rPr.rFonts.set(qn("w:ascii"), "Arial")
    title._element.rPr.rFonts.set(qn("w:hAnsi"), "Arial")
    title._element.rPr.rFonts.set(qn("w:cs"), "Arial")
    title.font.size = Pt(17)
    title.font.color.rgb = RGBColor(0, 0, 0)
    remove_paragraph_border(title)


def add_source_page(document: Document, page_number: int, text: str, first: bool) -> None:
    if not first:
        document.add_page_break()
    label = document.add_paragraph()
    set_rtl(label)
    label.paragraph_format.space_after = Pt(5)
    run = label.add_run(f"עמוד מקור {page_number}")
    set_font(run, 8.5, bold=True, color=RGBColor(90, 103, 112))
    blocks = [block.strip() for block in re.split(r"\n\s*\n", xml_safe(text)) if block.strip()]
    if not blocks:
        blocks = [line.strip() for line in xml_safe(text).splitlines() if line.strip()]
    for block in blocks:
        paragraph = document.add_paragraph()
        set_rtl(paragraph)
        paragraph.paragraph_format.space_after = Pt(3)
        paragraph.paragraph_format.line_spacing = 1.08
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        run = paragraph.add_run("\n".join(lines))
        set_font(run, 10.5)


def destination(output_root: Path, row: dict) -> Path:
    filename = "_".join((safe(row.get("מזהה רשומה", "")), safe(row.get("מספר תיק", "")), safe(row.get("תאריך", "")))) + ".docx"
    return output_root / safe(row.get("לשכה", "לשכה-לא-ידועה")) / safe(row.get("שנה", "ללא-שנה")) / filename


def build_converted_docx(row: dict, text_path: Path, output_path: Path) -> tuple[int, str]:
    full_text = text_path.read_text(encoding="utf-8", errors="replace")
    pages = split_pages(full_text)
    document = Document()
    configure_document(document)
    properties = document.core_properties
    properties.title = f"תיק {clean(row.get('מספר תיק'))}"
    properties.subject = "עותק Word שנוצר אוטומטית מטקסט שחולץ ממסמך ציבורי"
    properties.comments = "יש לאמת ציטוטים ומספרי עמודים מול PDF המקור"
    title = document.add_paragraph(style="Title")
    set_rtl(title)
    remove_paragraph_border(title)
    title.add_run(f"תיק {clean(row.get('מספר תיק')) or clean(row.get('מזהה רשומה'))}")
    metadata = document.add_paragraph()
    set_rtl(metadata)
    metadata.paragraph_format.space_after = Pt(4)
    meta_parts = [clean(row.get("סוג החלטה")), clean(row.get("תאריך")), clean(row.get("לשכה")), clean(row.get("מפקח/ת"))]
    run = metadata.add_run(" · ".join(part for part in meta_parts if part))
    set_font(run, 10, bold=True)
    warning = document.add_paragraph()
    set_rtl(warning)
    warning.paragraph_format.space_after = Pt(9)
    run = warning.add_run("עותק זה נוצר אוטומטית מטקסט שחולץ מן המסמך הציבורי. הוא אינו DOCX רשמי ויש לאמת כל ציטוט ומספר עמוד מול ה־PDF.")
    set_font(run, 9, color=RGBColor(92, 73, 32))
    for index, (page_number, page_text) in enumerate(pages):
        add_source_page(document, page_number, page_text, first=index == 0)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(".docx.part")
    document.save(temporary)
    if not is_real_docx(temporary):
        temporary.unlink(missing_ok=True)
        raise RuntimeError("generated file failed OOXML validation")
    temporary.replace(output_path)
    return len(pages), "converted-from-extracted-text"


def process(row: dict, output_root: Path, force: bool) -> dict:
    output_path = destination(output_root, row)
    text_value = row.get("טקסט מקומי", "")
    text_path = Path(text_value) if text_value else None
    result = {
        "מזהה רשומה": row.get("מזהה רשומה", ""), "מספר תיק": row.get("מספר תיק", ""),
        "תאריך": row.get("תאריך", ""), "לשכה": row.get("לשכה", ""), "סטטוס מקור": row.get("סטטוס", ""),
        "קישור DOCX מדווח": row.get("קישור DOCX", ""), "סוג DOCX": "", "סטטוס יצירה": "",
        "נתיב DOCX": "", "מספר עמודי מקור": "", "גודל בבייטים": "", "SHA256": "", "שגיאה": "",
    }
    try:
        if output_path.exists() and is_real_docx(output_path) and not force:
            pages = len(split_pages(text_path.read_text(encoding="utf-8", errors="replace"))) if text_path and text_path.is_file() else ""
            origin = "existing-valid-docx"
        elif text_path and text_path.is_file() and text_path.stat().st_size > 20:
            pages, origin = build_converted_docx(row, text_path, output_path)
        else:
            result.update({"סטטוס יצירה": "missing-full-text", "שגיאה": "אין טקסט מלא זמין; לא נוצר תוכן משוער"})
            return result
        result.update({"סוג DOCX": origin, "סטטוס יצירה": "available", "נתיב DOCX": str(output_path.resolve()),
                       "מספר עמודי מקור": pages, "גודל בבייטים": output_path.stat().st_size, "SHA256": sha256(output_path)})
    except Exception as exc:
        result.update({"סטטוס יצירה": "failed", "שגיאה": str(exc)})
    return result


def audit_report(rows: list[dict], output_root: Path) -> dict:
    mislabeled = list(ASHDOD_DOWNLOADS.glob("*.docx")) if ASHDOD_DOWNLOADS.exists() else []
    valid_originals = [path for path in mislabeled if is_real_docx(path)]
    available = [row for row in rows if row["סטטוס יצירה"] == "available"]
    return {
        "records": len(rows), "availableDocx": len(available),
        "missingFullText": sum(row["סטטוס יצירה"] == "missing-full-text" for row in rows),
        "failed": sum(row["סטטוס יצירה"] == "failed" for row in rows),
        "reportedDocxLinks": sum(bool(row["קישור DOCX מדווח"]) for row in rows),
        "downloadedFilesNamedDocx": len(mislabeled), "validDownloadedDocx": len(valid_originals),
        "downloadedFilesActuallyNotDocx": len(mislabeled) - len(valid_originals),
        "outputBytes": sum(int(row["גודל בבייטים"] or 0) for row in available),
        "outputRoot": str(output_root.resolve()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Create editable DOCX copies for the public decision corpus")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    source_rows = read_csv(EXPORT_CSV)
    if len(source_rows) != 1799:
        raise RuntimeError(f"Expected 1,799 records, found {len(source_rows)}")
    work_rows = source_rows[:args.limit] if args.limit else source_rows
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(lambda row: process(row, args.output, args.force), work_rows))
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    with args.manifest.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)
    report = audit_report(results, args.output)
    report_path = args.manifest.with_suffix(".report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
