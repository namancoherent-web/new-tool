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
    "Company", "Brand", "Parent_or_Independent", "Website", "Functionality",
    "Geography", "Is_Relevant", "Summary",
]


def _row(c: ClassifiedCompany) -> list:
    return [
        c.company_name,
        c.brand_name or c.company_name,
        c.parent_or_independent or "Independent",
        c.website,
        c.subcategory or c.category,
        c.hq_country,
        "yes" if c.is_relevant else "no",
        c.reason,
    ]


def _grouped_by_category(companies: list[ClassifiedCompany]) -> dict[str, list[ClassifiedCompany]]:
    """Group rows under whatever category each company was actually
    classified into -- the section names are never a fixed hardcoded list,
    since the categories themselves come from the user's own brief/prompt
    (applied by the classifier), not from this export step."""
    grouped: dict[str, list[ClassifiedCompany]] = {}
    for c in companies:
        grouped.setdefault(c.category or "Other", []).append(c)
    return grouped


def export_csv(companies: list[ClassifiedCompany], market_name: str, geography: str, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["#", *COLUMNS])
        for category, group in _grouped_by_category(companies).items():
            writer.writerow([f"=== {category} ({len(group)}) ==="])
            for i, c in enumerate(group, start=1):
                writer.writerow([i, *_row(c)])


def export_xlsx(companies: list[ClassifiedCompany], market_name: str, geography: str, path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Universe"

    ws.merge_cells("A1:I1")
    title_cell = ws["A1"]
    title_cell.value = f"{market_name} — {geography}"
    title_cell.font = Font(bold=True, size=14)
    title_cell.alignment = Alignment(horizontal="center")

    ws.merge_cells("A2:I2")
    summary_cell = ws["A2"]
    summary_cell.value = f"Geography: {geography}    Total Companies: {len(companies)}"
    summary_cell.font = Font(italic=True)

    row_idx = 4
    for category, group in _grouped_by_category(companies).items():
        ws.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=len(COLUMNS) + 1)
        section_cell = ws.cell(row=row_idx, column=1, value=f"=== {category} ({len(group)}) ===")
        section_cell.font = Font(bold=True, color="FFFFFF")
        section_cell.fill = PatternFill("solid", fgColor="4472C4")
        row_idx += 1

        ws.cell(row=row_idx, column=1, value="#").font = Font(bold=True)
        for col_idx, col_name in enumerate(COLUMNS, start=2):
            cell = ws.cell(row=row_idx, column=col_idx, value=col_name)
            cell.font = Font(bold=True)
        row_idx += 1

        for i, c in enumerate(group, start=1):
            ws.cell(row=row_idx, column=1, value=i)
            for col_idx, value in enumerate(_row(c), start=2):
                cell = ws.cell(row=row_idx, column=col_idx, value=value)
                if col_idx == 4:  # Website column
                    cell.hyperlink = c.source_url or f"https://{c.website}"
                    cell.font = Font(color="0563C1", underline="single")
            row_idx += 1

        row_idx += 1  # blank row between sections

    for col_idx in range(1, len(COLUMNS) + 2):
        ws.column_dimensions[get_column_letter(col_idx)].width = 22

    wb.save(path)


def export_docx(companies: list[ClassifiedCompany], market_name: str, geography: str, path: Path) -> None:
    doc = Document()
    doc.add_heading(f"{market_name} — {geography}", level=1)
    doc.add_paragraph(f"Total companies: {len(companies)}")

    for category, group in _grouped_by_category(companies).items():
        doc.add_heading(f"{category} ({len(group)})", level=2)
        for c in group:
            p = doc.add_paragraph(style="List Number")
            run = p.add_run(f"{c.company_name} — {c.website} ")
            run.bold = True
            p.add_run(f"[{c.parent_or_independent or 'Independent'}]").italic = True
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
