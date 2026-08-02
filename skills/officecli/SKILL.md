---
name: officecli
title: Office CLI (Word, Excel, PowerPoint)
description: Create, analyze, proofread, and modify Office documents (.docx, .xlsx, .pptx) using the single-binary officecli tool.
keywords: [officecli, docx, xlsx, pptx, word, excel, powerpoint, office document, presentation, spreadsheet]
packages: []
---

# Office CLI Skill

AI-friendly CLI for `.docx`, `.xlsx`, `.pptx`. Single binary, zero external Python libraries, no Microsoft Office installation required.

## Quick CLI Usage Reference

### PowerPoint (.pptx):
```bash
officecli create slides.pptx
officecli add slides.pptx / --type slide --prop title="Q4 Report" --prop background=1A1A2E
officecli add slides.pptx '/slide[1]' --type shape --prop text="Revenue grew 25%" --prop x=2cm --prop y=5cm --prop font=Arial --prop size=24 --prop color=FFFFFF
```

### Word Document (.docx):
```bash
officecli create report.docx
officecli add report.docx /body --type paragraph --prop text="Executive Summary" --prop style=Heading1
officecli add report.docx /body --type paragraph --prop text="Revenue increased by 25% year-over-year."
```

### Excel Spreadsheet (.xlsx):
```bash
officecli create data.xlsx
officecli set data.xlsx /Sheet1/A1 --prop value="Metric" --prop bold=true
officecli set data.xlsx /Sheet1/B1 --prop value="Value" --prop bold=true
officecli set data.xlsx /Sheet1/A2 --prop value="Revenue"
officecli set data.xlsx /Sheet1/B2 --prop value=50000 --prop type=Number
```

## Inspection & Extraction:
```bash
officecli view report.docx outline
officecli view report.docx stats
officecli view report.docx text
officecli query report.docx 'paragraph[style=Heading1]'
```
