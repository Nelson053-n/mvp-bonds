"""PDF report generation for portfolios."""
import io
from datetime import date

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api.deps import get_current_user, get_portfolio_or_403
from app.services.portfolio_service import portfolio_service

router = APIRouter(tags=["reports"])


@router.get("/portfolios/{portfolio_id}/report.pdf")
async def export_portfolio_pdf(
    portfolio_id: int,
    current_user: dict = Depends(get_current_user),
) -> StreamingResponse:
    """Export portfolio as PDF report."""
    portfolio = await get_portfolio_or_403(portfolio_id, current_user)
    rows = await portfolio_service.get_table(portfolio_id)

    pdf_bytes = _generate_pdf(portfolio["name"], rows)

    filename = f"portfolio_{portfolio_id}_{date.today().isoformat()}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _generate_pdf(portfolio_name: str, rows) -> bytes:
    try:
        return _generate_pdf_reportlab(portfolio_name, rows)
    except ImportError:
        return _generate_pdf_fpdf(portfolio_name, rows)


def _generate_pdf_reportlab(portfolio_name: str, rows) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    import os

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
    )

    # Try to register a Unicode font for Cyrillic support
    font_name = "Helvetica"  # fallback
    for font_path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]:
        if os.path.exists(font_path):
            try:
                pdfmetrics.registerFont(TTFont("CyrillicFont", font_path))
                font_name = "CyrillicFont"
            except Exception:
                pass
            break

    elements = []

    # Title
    title_style = ParagraphStyle("title", fontName=font_name, fontSize=16, spaceAfter=6)
    sub_style = ParagraphStyle("sub", fontName=font_name, fontSize=11, spaceAfter=3, textColor=colors.grey)

    elements.append(Paragraph("Bond AI — Portfolio Report", title_style))
    elements.append(Paragraph(f"Portfolio: {portfolio_name}", sub_style))
    elements.append(Paragraph(f"Generated: {date.today().isoformat()}", sub_style))
    elements.append(Spacer(1, 0.4 * cm))

    # Calculate summary
    total_value = sum(r.current_value or 0 for r in rows)
    total_cost = sum((r.purchase_price or 0) * (r.quantity or 0) for r in rows)
    total_profit = total_value - total_cost
    bonds = [r for r in rows if r.type == "bond"]
    annual_coupon = sum(
        (r.coupon or 0) * (r.quantity or 0) * (round(365 / (r.coupon_period or 182)) if (r.coupon_period or 0) > 0 else 2)
        for r in bonds
    )

    # Summary table
    pnl_sign = "+" if total_profit >= 0 else ""
    summary_data = [
        ["Metric", "Value"],
        ["Total value", f"{total_value:,.2f} RUB"],
        ["Total cost", f"{total_cost:,.2f} RUB"],
        ["P&L", f"{pnl_sign}{total_profit:,.2f} RUB"],
        ["Annual coupon income", f"{annual_coupon:,.2f} RUB"],
        ["Instruments count", str(len(rows))],
    ]

    summary_table = Table(summary_data, colWidths=[6 * cm, 6 * cm])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("PADDING", (0, 0), (-1, -1), 5),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 0.5 * cm))

    # Instruments table
    headers = ["#", "Ticker", "Name", "Type", "Qty", "Buy Price", "Cur. Price", "Value", "P&L", "Coupon"]
    table_data = [headers]

    for i, row in enumerate(rows, 1):
        profit = round((row.current_price or 0) - (row.purchase_price or 0), 2) * (row.quantity or 0)
        table_data.append([
            str(i),
            str(row.ticker or ""),
            str(row.name or "")[:30],  # Truncate long names
            str(row.type or ""),
            str(row.quantity or ""),
            f"{row.purchase_price or 0:.2f}",
            f"{row.current_price or 0:.2f}",
            f"{row.current_value or 0:.2f}",
            f"{profit:+.2f}",
            f"{row.coupon or 0:.2f}" if row.type == "bond" else "-",
        ])

    col_widths = [0.7 * cm, 2.2 * cm, 6 * cm, 1.8 * cm, 1.5 * cm, 2.5 * cm, 2.5 * cm, 2.5 * cm, 2.5 * cm, 2 * cm]
    inst_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    inst_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e40af")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e2e8f0")),
        ("PADDING", (0, 0), (-1, -1), 4),
        ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
    ]))
    elements.append(inst_table)

    doc.build(elements)
    return buf.getvalue()


def _generate_pdf_fpdf(portfolio_name: str, rows) -> bytes:
    """Fallback using fpdf2."""
    from fpdf import FPDF

    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.add_page()
    pdf.set_font("Helvetica", size=16)
    pdf.cell(0, 10, "Bond AI — Portfolio Report", ln=True)
    pdf.set_font("Helvetica", size=11)
    pdf.cell(0, 8, f"Portfolio: {portfolio_name}", ln=True)
    pdf.cell(0, 8, f"Date: {date.today().isoformat()}", ln=True)
    pdf.ln(5)

    # Simple table
    pdf.set_font("Helvetica", "B", size=9)
    headers = ["#", "Ticker", "Name", "Qty", "Buy Price", "Cur. Price", "Value", "P&L"]
    widths = [10, 20, 70, 15, 25, 25, 25, 25]
    for h, w in zip(headers, widths):
        pdf.cell(w, 7, h, border=1)
    pdf.ln()

    pdf.set_font("Helvetica", size=8)
    for i, row in enumerate(rows, 1):
        profit = round(((row.current_price or 0) - (row.purchase_price or 0)) * (row.quantity or 0), 2)
        vals = [
            str(i),
            str(row.ticker or ""),
            str(row.name or "")[:35],
            str(row.quantity or ""),
            f"{row.purchase_price or 0:.2f}",
            f"{row.current_price or 0:.2f}",
            f"{row.current_value or 0:.2f}",
            f"{profit:+.2f}",
        ]
        for v, w in zip(vals, widths):
            pdf.cell(w, 6, v, border=1)
        pdf.ln()

    return pdf.output()
