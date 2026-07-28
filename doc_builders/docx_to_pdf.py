"""Convert the generated .docx guides to PDF (for reading on a reMarkable etc.).

Reads each .docx with python-docx and re-renders it with fpdf2 -- no Word or
LibreOffice needed. Preserves headings, paragraphs, inline bold/colour runs,
bullets, numbered lists, tables and page breaks. Text is transliterated to
plain ASCII so the built-in font renders every glyph cleanly.

    python docx_to_pdf.py                 # convert all guides in working_version
    python docx_to_pdf.py Foo.docx        # convert one
"""
import sys
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from fpdf import FPDF

WORKING = Path(__file__).resolve().parent.parent
DOCS = [
    "Odins_Watchlist_Beginner_Guide.docx",
    "Odins_Watchlist_Guide.docx",
    "Odins_Watchlist_UseCases.docx",
    "Odins_Watchlist_UseCase_Examples.docx",
]

ASCII = {
    "—": "-", "–": "-", "‘": "'", "’": "'", "“": '"', "”": '"', "≥": ">=", "≤": "<=",
    "−": "-", "±": "+/-", "×": "x", "✕": "x", "✖": "x", "≈": "~", "…": "...", "→": "->",
    "↑": "^", "★": "*", "☆": "*", "▲": "^", "▼": "v", "₹": "Rs ", "·": "-", "÷": "/",
    "•": "-", "↗": "^", "↘": "v", "Δ": "delta ", "①": "1.", "②": "2.", "③": "3.",
    "④": "4.", "⑤": "5.", " ": " ", "‑": "-", "‑": "-",
}


def ascii_text(s: str) -> str:
    for k, v in ASCII.items():
        s = s.replace(k, v)
    return s.encode("latin-1", "replace").decode("latin-1")


def rgb(run, default=(38, 38, 38)):
    try:
        c = run.font.color
        if c is not None and c.rgb is not None:
            v = c.rgb
            return (v[0], v[1], v[2])
    except Exception:
        pass
    return default


def has_page_break(par: Paragraph) -> bool:
    xml = par._p.xml
    return 'w:type="page"' in xml


class PDF(FPDF):
    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 8, f"{self.page_no()}", align="C")


def style_size(style_name, run):
    if run is not None and run.font is not None and run.font.size is not None:
        return run.font.size.pt
    return {"Title": 22, "Heading 1": 16, "Heading 2": 13.5, "Heading 3": 11.5}.get(style_name, 10.5)


def render_paragraph(pdf: PDF, par: Paragraph):
    name = par.style.name if par.style else "Normal"
    text = ascii_text(par.text)
    if has_page_break(par) and pdf.get_y() > pdf.t_margin + 4:
        pdf.add_page()
    if not text.strip():
        pdf.ln(3)
        return

    heading = name in ("Title", "Heading 1", "Heading 2", "Heading 3")
    is_bullet = name == "List Bullet"
    is_number = name == "List Number"

    if name in ("Heading 1", "Title"):
        pdf.ln(3)
    elif name in ("Heading 2", "Heading 3"):
        pdf.ln(2)

    left = pdf.l_margin + (6 if (is_bullet or is_number) else 0)
    pdf.set_left_margin(left)
    pdf.set_x(left)

    if is_bullet:
        pdf.set_font("Helvetica", "", 10.5)
        pdf.set_text_color(122, 84, 23)
        pdf.write(5.2, "-  ")
    elif is_number:
        pdf.set_font("Helvetica", "B", 10.5)
        pdf.set_text_color(122, 84, 23)
        pdf.write(5.2, f"{render_paragraph.counter}.  ")
        render_paragraph.counter += 1

    if heading:
        size = style_size(name, par.runs[0] if par.runs else None)
        pdf.set_font("Helvetica", "B", size)
        pdf.set_text_color(28, 27, 23)
        pdf.write(size * 0.52, text)
    else:
        for run in par.runs:
            t = ascii_text(run.text)
            if not t:
                continue
            size = style_size(name, run)
            bold = bool(run.bold)
            italic = bool(run.italic)
            st = ("B" if bold else "") + ("I" if italic else "")
            pdf.set_font("Helvetica", st, size)
            pdf.set_text_color(*rgb(run))
            pdf.write(5.2, t)
    pdf.ln(7.5 if heading else 5.6)
    pdf.set_left_margin(pdf.l_margin if not (is_bullet or is_number) else left - 6)
    # restore base margin
    pdf.set_left_margin(BASE_MARGIN)
    if not (is_bullet or is_number):
        render_paragraph.counter = 1


def render_table(pdf: PDF, tbl: Table):
    pdf.ln(1)
    rows = [[ascii_text(c.text) for c in r.cells] for r in tbl.rows]
    if not rows:
        return
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(38, 38, 38)
    with pdf.table(first_row_as_headings=True, line_height=5.2,
                   headings_style=__import__("fpdf").fonts.FontFace(emphasis="BOLD", fill_color=(239, 226, 198)),
                   width=pdf.epw, text_align="LEFT") as table:
        for r in rows:
            row = table.row()
            for cell in r:
                row.cell(cell)
    pdf.ln(2)


def convert(docx_path: Path, pdf_path: Path):
    doc = Document(str(docx_path))
    pdf = PDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(True, margin=15)
    global BASE_MARGIN
    BASE_MARGIN = pdf.l_margin
    pdf.add_page()
    render_paragraph.counter = 1

    body = doc.element.body
    from docx.oxml.ns import qn
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            render_paragraph(pdf, Paragraph(child, doc))
        elif child.tag == qn("w:tbl"):
            render_table(pdf, Table(child, doc))
    pdf.output(str(pdf_path))
    return pdf.page_no()


def main():
    targets = sys.argv[1:] or DOCS
    for name in targets:
        src = Path(name) if Path(name).is_absolute() else WORKING / name
        if not src.exists():
            print("skip (missing):", src.name)
            continue
        out = src.with_suffix(".pdf")
        pages = convert(src, out)
        print(f"OK  {out.name}  ({pages} pages, {out.stat().st_size//1024} KB)")


if __name__ == "__main__":
    main()
