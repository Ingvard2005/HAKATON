"""Local printable export; no external requests or mutation of agreement data."""
import io
from pathlib import Path
from xml.sax.saxutils import escape
from services.dates import local_now


def agreements_pdf(items, settings, *, filters="", generated_at=None):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle
    font = "CallMindDejaVu"
    if font not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font, str(Path(__file__).resolve().parent.parent / "assets/fonts/DejaVuSans.ttf")))
    red = colors.HexColor("#E30611")
    body = ParagraphStyle("body", fontName=font, fontSize=8.5, leading=13, textColor=colors.HexColor("#182230"))
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=12, textColor=colors.HexColor("#485566"))
    title = ParagraphStyle("title", parent=body, fontSize=23, leading=30, spaceAfter=9)
    def para(value, style=body):
        return Paragraph(escape(str(value if value is not None else "")).replace("\n", "<br/>"), style)
    output = io.BytesIO()
    width, height = landscape(A4)
    margin = 36
    doc = SimpleDocTemplate(output, pagesize=(width, height), leftMargin=margin, rightMargin=margin,
        topMargin=32, bottomMargin=54, title="CallMind - Договорённости", author="CallMind")
    def footer(canvas, document):
        canvas.saveState()
        canvas.setFillColor(red)
        canvas.rect(0, 0, width, 31, stroke=0, fill=1)
        x, y = margin + 8, 15
        path = canvas.beginPath()
        path.moveTo(x, y + 10)
        path.curveTo(x + 4, y + 10, x + 8, y - 1, x + 8, y - 4)
        path.curveTo(x + 8, y - 12, x - 8, y - 12, x - 8, y - 4)
        path.curveTo(x - 8, y - 1, x - 4, y + 10, x, y + 10)
        path.close()
        canvas.setFillColor(colors.white)
        canvas.drawPath(path, stroke=0, fill=1)
        canvas.setFont(font, 9)
        canvas.drawString(margin + 26, 12, "CallMind / Договорённости")
        canvas.drawRightString(width - margin, 12, f"Страница {document.page}")
        canvas.restoreState()
    generated_at = generated_at or local_now(settings["timezone"])
    story = [para("CALLMIND / ЭКСПОРТ РАБОЧЕГО СПИСКА", small), Spacer(1, 8), para("Договорённости", title),
        para(f"Сформировано: {generated_at:%d.%m.%Y, %H:%M} · {settings['timezone']} · Записей: {len(items)}", small)]
    if filters:
        story += [Spacer(1, 4), para("Фильтры: " + filters, small)]
    story.append(Spacer(1, 14))
    if not items:
        story.append(para("В текущем списке нет договорённостей. Измените фильтры или добавьте звонок."))
    else:
        sides = {"manager": "Менеджер", "client": "Клиент", "unknown": "Нужна проверка"}
        priorities = {"low": "Низкий", "normal": "Обычный", "high": "Высокий"}
        rows = [[para(label, small) for label in ("ID", "Клиент / телефон", "Договорённость", "Сторона", "Срок", "Статус", "Приоритет")]]
        for item in items:
            due = item.get("deadline")
            deadline = due.strftime("%d.%m.%Y" if item.get("date_only") else "%d.%m.%Y\n%H:%M") if due else "Без срока"
            client = item.get("client_name") or "Клиент не указан"
            if item.get("client_phone"):
                client += "\n" + item["client_phone"]
            state = "Выполнено" if item.get("status") == "done" else "В работе"
            if item.get("review_reasons"):
                state += "\nТребует проверки"
            rows.append([para(v) for v in (item.get("id"), client, item.get("description"),
                sides.get(item.get("responsible"), "Нужна проверка"), deadline, state,
                priorities.get(item.get("priority"), item.get("priority") or "Не указан"))])
        table = LongTable(rows, colWidths=[(width - 2 * margin) * v for v in (.045, .18, .315, .10, .12, .14, .10)],
            repeatRows=1, splitByRow=1, splitInRow=1, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF0F3")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FAFBFC")]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("LINEBELOW", (0, 0), (-1, 0), 1, red),
            ("LINEBELOW", (0, 1), (-1, -1), .4, colors.HexColor("#E2E6EC")),
        ]))
        story.append(table)
    story += [Spacer(1, 12), para("Сторона обязательства не является назначением задачи в CRM. Статус «Выполнено» не означает выигрыш сделки.", small)]
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
