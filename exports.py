"""CSV / Excel / PDF exports and the GST invoice PDF. (PDF fonts have no rupee glyph, so PDFs say 'Rs'.)"""
import csv
import io
from decimal import Decimal
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer
import engine as E


def _cell(v):
    if isinstance(v, Decimal):
        return E.fmt(v) if v == v.to_integral_value() or len(str(v).split(".")[-1]) <= 3 else str(E.q2(v))
    return "" if v is None else v


def to_csv(rep):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(rep["headers"])
    for r in rep["rows"]:
        w.writerow([_cell(c) for c in r])
    return buf.getvalue().encode("utf-8-sig")


def to_xlsx(rep):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = rep["title"][:30].replace(":", "")
    ws.append(rep["headers"])
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rep["rows"]:
        ws.append([c if isinstance(c, (Decimal, int)) else _cell(c) for c in r])
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = min(34, max(len(str(c.value or "")) for c in col) + 2)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def to_pdf(rep, subtitle=""):
    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm)
    st = getSampleStyleSheet()
    data = [rep["headers"]] + [[str(_cell(c)) for c in r] for r in rep["rows"]]
    t = Table(data, repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#6d28d9")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                           ("FONTSIZE", (0, 0), (-1, -1), 7), ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                           ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f0fb")])]))
    doc.build([Paragraph(rep["title"].replace("₹", "Rs "), st["Title"]), Paragraph(subtitle, st["Normal"]), Spacer(1, 6), t])
    return out.getvalue()


def invoice_pdf(db, invoice_id):
    inv = db.execute("SELECT i.*, c.name cname, c.gstin cgstin, c.state cstate, c.state_code ccode FROM invoices i JOIN customers c ON c.id=i.customer_id WHERE i.id=?", (invoice_id,)).fetchone()
    lines = db.execute("SELECT l.*, i.name FROM invoice_lines l JOIN items i ON i.id=l.item_id WHERE invoice_id=?", (invoice_id,)).fetchall()
    S = lambda k: E.setting(db, k)
    st = getSampleStyleSheet()
    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm)
    m = lambda v: f"{E.q2(v):,.2f}"
    story = [Paragraph("TAX INVOICE" + ("  (CANCELLED - Credit note " + inv["cn_no"] + ")" if inv["status"] == "CANCELLED" else ""), st["Title"]),
             Paragraph(f"<b>{S('company')}</b><br/>{S('address')}<br/>GSTIN: {S('gstin')} &nbsp; State: {S('state')} ({S('state_code')})", st["Normal"]), Spacer(1, 6),
             Paragraph(f"<b>Invoice no:</b> {inv['inv_no']} &nbsp;&nbsp; <b>Date:</b> {inv['date']}<br/>"
                       f"<b>Bill to:</b> {inv['cname']}<br/>GSTIN: {inv['cgstin'] or 'Unregistered'}<br/>"
                       f"<b>Place of supply:</b> {inv['cstate']} ({inv['ccode']})", st["Normal"]), Spacer(1, 8)]
    head = ["#", "Description", "HSN", "Qty", "Rate", "Taxable", "GST %", "CGST", "SGST", "IGST"]
    rows = [head] + [[n + 1, l["name"], l["hsn"], E.fmt(l["qty"]), m(l["rate"]), m(l["taxable"]), E.fmt(l["gst_rate"]), m(l["cgst"]), m(l["sgst"]), m(l["igst"])]
                     for n, l in enumerate(lines)]
    rows.append(["", "Total", "", "", "", m(inv["taxable"]), "", m(inv["cgst"]), m(inv["sgst"]), m(inv["igst"])])
    t = Table(rows, repeatRows=1)
    t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 8), ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                           ("ALIGN", (3, 1), (-1, -1), "RIGHT")]))
    story += [t, Spacer(1, 8), Paragraph(f"<b>Invoice total: Rs {m(inv['total'])}</b>", st["Heading3"]),
              Paragraph("Computer-generated invoice for a demo business. Dummy data only.", st["Normal"])]
    doc.build(story)
    return out.getvalue()
