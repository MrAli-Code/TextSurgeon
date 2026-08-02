---
name: docx_report_builder
title: 📑 Word & Document Report Generator
description: Create styled Microsoft Word (.docx) documents, technical reports, cover pages, formatted tables, headers/footers, and clean document layouts using python-docx.
keywords: [docx, word, report, document, doc, ms word, paper, thesis, گواهی, گزارش, ورد]
packages: [python-docx]
---

# Word & Document Report Generator Skill

Creates structured `.docx` documents using `python-docx`.

## Core Guidelines & Best Practices

1. **Document Structure & Margins**:
   - Set standard 1-inch margins:
     ```python
     from docx import Document
     from docx.shared import Inches, Pt, RGBColor
     from docx.enum.text import WD_ALIGN_PARAGRAPH

     doc = Document()
     sections = doc.sections
     for s in sections:
         s.top_margin = Inches(1.0)
         s.bottom_margin = Inches(1.0)
         s.left_margin = Inches(1.0)
         s.right_margin = Inches(1.0)
     ```
2. **Typography Hierarchy**:
   - Title: 24-28 pt bold.
   - Heading 1: 18 pt bold.
   - Heading 2: 14 pt bold.
   - Body: 11 pt regular, 1.15 line spacing, 6 pt after paragraph.
3. **Tables**:
   - Use built-in table styles (e.g. `'Table Grid'`, `'Light Shading Accent 1'`).
   - Format header row with bold text and background fill.

## Minimal Working Example

```python
from docx import Document
from docx.shared import Inches, Pt, RGBColor

doc = Document()
h1 = doc.add_heading("Executive Project Report", level=0)
p = doc.add_paragraph("This automated report details key performance indicators and system telemetry.")

table = doc.add_table(rows=1, cols=3)
table.style = 'Table Grid'
hdr_cells = table.rows[0].cells
hdr_cells[0].text = 'Metric'
hdr_cells[1].text = 'Target'
hdr_cells[2].text = 'Actual'

data = [("Latency (ms)", "< 100", "42"), ("Uptime (%)", "99.9", "99.98")]
for row in data:
    row_cells = table.add_row().cells
    for i, val in enumerate(row):
        row_cells[i].text = val

doc.save("report.docx")
print("✅ Saved report.docx")
```
