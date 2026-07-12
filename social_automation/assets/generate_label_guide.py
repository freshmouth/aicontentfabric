from __future__ import annotations

from pathlib import Path

from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


OUTPUT = Path(__file__).with_name("salad_dressing_label_guide.pdf")


def build() -> Path:
    navy = HexColor("#17212B")
    green = HexColor("#2D6A4F")
    mint = HexColor("#E9F4EE")
    gray = HexColor("#5B6570")
    line = HexColor("#D9E2E8")

    doc = SimpleDocTemplate(
        str(OUTPUT),
        pagesize=letter,
        rightMargin=0.62 * inch,
        leftMargin=0.62 * inch,
        topMargin=0.58 * inch,
        bottomMargin=0.52 * inch,
        title="The 30-Second Salad Dressing Label Check",
        author="AI Content Factory",
    )
    styles = getSampleStyleSheet()
    title = ParagraphStyle("Title", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=28, leading=32, textColor=navy, alignment=TA_CENTER, spaceAfter=16)
    subtitle = ParagraphStyle("Subtitle", parent=styles["BodyText"], fontName="Helvetica", fontSize=13, leading=18, textColor=gray, alignment=TA_CENTER)
    heading = ParagraphStyle("Heading", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=green, spaceAfter=8)
    body = ParagraphStyle("Body", parent=styles["BodyText"], fontName="Helvetica", fontSize=11.2, leading=16, textColor=navy, alignment=TA_LEFT)
    small = ParagraphStyle("Small", parent=body, fontSize=8.5, leading=12, textColor=gray)
    number = ParagraphStyle("Number", parent=body, fontName="Helvetica-Bold", fontSize=20, leading=22, textColor=white, alignment=TA_CENTER)

    story = [Spacer(1, 0.55 * inch)]
    story.append(Paragraph("THE 30-SECOND", subtitle))
    story.append(Spacer(1, 0.08 * inch))
    story.append(Paragraph("Salad Dressing<br/>Label Check", title))
    story.append(Paragraph("Three things to compare before a bottle goes in your cart", subtitle))
    story.append(Spacer(1, 0.38 * inch))

    cover_rows = [
        [Paragraph("1", number), Paragraph("Read the first ingredients", heading)],
        [Paragraph("2", number), Paragraph("Check added sugar per serving", heading)],
        [Paragraph("3", number), Paragraph("Reality-check sodium and serving size", heading)],
    ]
    cover = Table(cover_rows, colWidths=[0.6 * inch, 5.75 * inch], rowHeights=[0.72 * inch] * 3, hAlign="CENTER")
    cover.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), green),
        ("BACKGROUND", (1, 0), (1, -1), mint),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (1, 0), (1, -1), 16),
        ("BOX", (0, 0), (-1, -1), 0.75, line),
        ("INNERGRID", (0, 0), (-1, -1), 0.75, line),
    ]))
    story.extend([cover, Spacer(1, 0.36 * inch), Paragraph("Save this guide on your phone and compare two similar bottles side by side. The better choice is usually easier to spot when you compare, rather than judge one label in isolation.", body), PageBreak()])

    sections = [
        ("1", "Start with the ingredient list", "Ingredients are listed from greatest to least by weight. Identify the primary oil and notice where sweeteners appear. Prefer a label you can understand and that fits how you plan to use the dressing. A long ingredient list is not automatically bad, and a short one is not automatically healthy."),
        ("2", "Compare added sugar", "Use the Nutrition Facts panel, not front-label phrases. Compare added sugar in the same serving size across similar dressings. A small difference matters less than the amount you actually pour, so estimate whether your usual portion is one serving or more."),
        ("3", "Check sodium against your real portion", "Look at sodium per serving, then multiply it by the amount you normally use. Compare similar products and consider the rest of the meal. A dressing can fit your routine while still being worth measuring once or twice so the listed serving becomes meaningful."),
    ]
    for index, (num, head, text) in enumerate(sections):
        badge = Table([[Paragraph(num, number)]], colWidths=[0.52 * inch], rowHeights=[0.52 * inch])
        badge.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), green), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
        row = Table([[badge, [Paragraph(head, heading), Paragraph(text, body)]]], colWidths=[0.68 * inch, 5.75 * inch])
        row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 14)]))
        story.append(row)
        if index < len(sections) - 1:
            story.append(Table([[""]], colWidths=[6.43 * inch], rowHeights=[0.02 * inch], style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), line)])))
            story.append(Spacer(1, 0.18 * inch))

    story.append(Spacer(1, 0.18 * inch))
    story.append(Paragraph("QUICK CART TEST", heading))
    quick = Table(
        [
            ["CHECK", "BOTTLE A", "BOTTLE B"],
            ["Primary oil / first ingredients", "__________", "__________"],
            ["Added sugar per serving", "__________", "__________"],
            ["Sodium per real portion", "__________", "__________"],
        ],
        colWidths=[3.15 * inch, 1.65 * inch, 1.65 * inch],
        rowHeights=[0.38 * inch, 0.47 * inch, 0.47 * inch, 0.47 * inch],
    )
    quick.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), navy),
        ("TEXTCOLOR", (0, 0), (-1, 0), white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (1, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.75, line),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.extend([quick, Spacer(1, 0.26 * inch), Paragraph("This checklist is educational and is not medical or individualized nutrition advice. Follow guidance from your clinician when you have a diagnosed condition or prescribed dietary limits.", small)])
    doc.build(story)
    return OUTPUT


if __name__ == "__main__":
    print(build())
