from __future__ import annotations

import csv
from pathlib import Path

from docx import Document
from docx.shared import Pt
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from pipeline.models import ClassifiedCompany

COLUMNS = [
    "Company Name", "Website", "HQ Country", "Target Geography Presence",
    "Category", "Subcategory", "Confidence", "Matched Products",
    "Evidence Source", "Source URL", "Reason",
]


def _row(c: ClassifiedCompany) -> list:
    return [
        c.company_name,
        c.website,
        c.hq_country,
        "Yes" if c.operates_in_target_geography else "No",
        c.category,
        c.subcategory,
        c.confidence,
        "; ".join(c.matched_products),
        c.evidence_source,
        c.source_url,
        c.reason,
    ]


def export_csv(companies: list[ClassifiedCompany], market_name: str, geography: str, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([f"{market_name} — {geography}"])
        writer.writerow(["#", *COLUMNS])
        for i, c in enumerate(companies, start=1):
            writer.writerow([i, *_row(c)])


def export_xlsx(companies: list[ClassifiedCompany], market_name: str, geography: str, path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Universe"

    ws.merge_cells("A1:L1")
    title_cell = ws["A1"]
    title_cell.value = f"{market_name} — {geography}"
    title_cell.font = Font(bold=True, size=14)
    title_cell.alignment = Alignment(horizontal="center")

    ws.merge_cells("A2:L2")
    summary_cell = ws["A2"]
    summary_cell.value = f"Geography: {geography}    Total Companies: {len(companies)}"
    summary_cell.font = Font(italic=True)

    header_row = 4
    ws.cell(row=header_row, column=1, value="#")
    for col_idx, col_name in enumerate(COLUMNS, start=2):
        cell = ws.cell(row=header_row, column=col_idx, value=col_name)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="4472C4")
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

    row_idx = header_row + 1
    for i, c in enumerate(companies, start=1):
        ws.cell(row=row_idx, column=1, value=i)
        for col_idx, value in enumerate(_row(c), start=2):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            if col_idx == 3:  # Website column
                cell.hyperlink = c.source_url or f"https://{c.website}"
                cell.font = Font(color="0563C1", underline="single")
        row_idx += 1

    for col_idx in range(1, len(COLUMNS) + 2):
        ws.column_dimensions[get_column_letter(col_idx)].width = 22

    wb.save(path)


def export_docx(companies: list[ClassifiedCompany], market_name: str, geography: str, path: Path) -> None:
    doc = Document()
    doc.add_heading(f"{market_name} — {geography}", level=1)
    doc.add_paragraph(f"Total companies: {len(companies)}")

    by_category: dict[str, list[ClassifiedCompany]] = {}
    for c in companies:
        by_category.setdefault(c.category, []).append(c)

    for category, group in by_category.items():
        doc.add_heading(f"{category} ({len(group)})", level=2)
        for c in group:
            p = doc.add_paragraph(style="List Number")
            run = p.add_run(f"{c.company_name} — {c.website} ")
            run.bold = True
            p.add_run(f"[{c.subcategory or c.category}]").italic = True
            if c.reason:
                detail = doc.add_paragraph(c.reason)
                detail.paragraph_format.left_indent = Pt(18)

    doc.save(path)


def export_all(companies: list[ClassifiedCompany], market_name: str, geography: str, out_dir: Path, basename: str) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{basename}.csv"
    xlsx_path = out_dir / f"{basename}.xlsx"
    docx_path = out_dir / f"{basename}.docx"

    export_csv(companies, market_name, geography, csv_path)
    export_xlsx(companies, market_name, geography, xlsx_path)
    export_docx(companies, market_name, geography, docx_path)

    return {"csv": csv_path, "xlsx": xlsx_path, "docx": docx_path}
