---
name: powerpoint_maker
title: 📊 PowerPoint Presentation Deck Maker
description: Generate rich, modern, professional PowerPoint (.pptx) presentation decks with custom layouts, auto-fitting text boxes, clean typography, and RTL/Farsi support.
keywords: [powerpoint, pptx, presentation, slides, deck, slide deck, keynote, ppt, اسلاید, پاورپوینت]
packages: [python-pptx]
---

# PowerPoint Presentation Generator Skill

Creates presentation decks with `python-pptx`.

## Core Guidelines & Best Practices

1. **Slide Dimensions & Geometry**:
   - Default to widescreen 16:9 layout:
     ```python
     prs = Presentation()
     prs.slide_width = Inches(13.333)
     prs.slide_height = Inches(7.5)
     ```
2. **Modern Card Layouts & Contrast**:
   - Use high-contrast color palettes (e.g. Dark Navy `#0F172A`, Accent Blue `#3B82F6`, Soft Gray `#F8FAFC`, Emerald `#10B981`).
   - Group slide content into visual cards/containers with shapes and subtle backgrounds.
3. **Typography & Auto-Fit**:
   - Ensure text frames have word wrap enabled: `tf.word_wrap = True`.
   - Set clean fonts (e.g. Arial, Calibri, Tahoma, or Vazirmatn for Persian/Arabic).
4. **Bilingual & RTL Handling**:
   - When generating slides with Persian or Arabic text:
     - Set right-aligned text alignment: `p.alignment = PP_ALIGN.RIGHT`.
     - Mirror brackets and punctuation when adjacent to English text.

## Minimal Working Example

```python
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN

prs = Presentation()
prs.slide_width = Inches(13.333)
prs.slide_height = Inches(7.5)
blank_layout = prs.slide_layouts[6]
slide = prs.slides.add_slide(blank_layout)

# Add Title Box
tb = slide.shapes.add_textbox(Inches(1.0), Inches(0.8), Inches(11.333), Inches(1.5))
tf = tb.text_frame
tf.word_wrap = True
p = tf.paragraphs[0]
p.text = "Autonomous AI Presentation"
p.font.size = Pt(40)
p.font.bold = True
p.font.color.rgb = RGBColor(15, 23, 42)

prs.save("presentation.pptx")
print("✅ Saved presentation.pptx")
```
