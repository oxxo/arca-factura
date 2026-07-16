"""PDF generation for AFIP invoices.

Uses Jinja2 templates + WeasyPrint for HTML→PDF conversion.
"""
import base64
import io
import json
import sys
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Comprobante type mappings
TIPO_NOMBRES = {
    11: "FACTURA C",
    13: "NOTA DE CRÉDITO C",
    211: "FACTURA DE CRÉDITO ELECTRÓNICA MiPyMEs C",
    213: "NOTA DE CRÉDITO ELECTRÓNICA MiPyMEs C",
}

TIPO_LETRAS = {
    11: "C",
    13: "C",
    211: "C",
    213: "C",
}


def check_weasyprint():
    """Verify WeasyPrint can render. Fail fast if deps are missing."""
    try:
        from weasyprint import HTML
        HTML(string="<p>test</p>").write_pdf()
    except Exception as e:
        sys.exit(
            f"WeasyPrint not working: {e}\n"
            f"Install: https://doc.courtbouillon.org/weasyprint/stable/first_steps.html"
        )


def generate_qr_data(
    fecha: str,
    cuit_emisor: str,
    punto_venta: int,
    tipo_comprobante: int,
    nro_comprobante: int,
    importe: float,
    moneda: str,
    cotizacion: float,
    cuit_receptor: str,
    cae: str,
) -> str:
    """Generate the AFIP QR code data URL.

    Returns a data URI for the QR image (base64 PNG).
    """
    # AFIP QR JSON payload
    qr_payload = {
        "ver": 1,
        "fecha": fecha,  # YYYY-MM-DD
        "cuit": int(cuit_emisor),
        "ptoVta": punto_venta,
        "tipoCmp": tipo_comprobante,
        "nroCmp": nro_comprobante,
        "importe": importe,
        "moneda": "PES" if moneda == "PES" else "DOL",
        "ctz": cotizacion,
        "tipoDocRec": 80,  # CUIT
        "nroDocRec": int(cuit_receptor),
        "tipoCodAut": "E",  # CAE
        "codAut": int(cae),
    }

    # Base64 encode for AFIP QR URL
    json_str = json.dumps(qr_payload)
    b64 = base64.b64encode(json_str.encode()).decode()

    # Generate QR code image
    try:
        import qrcode
        qr_url = f"https://www.afip.gob.ar/fe/qr/?p={b64}"
        qr = qrcode.make(qr_url)
        buf = io.BytesIO()
        qr.save(buf, format="PNG")
        buf.seek(0)
        img_b64 = base64.b64encode(buf.read()).decode()
        return f"data:image/png;base64,{img_b64}"
    except ImportError:
        return ""


def format_number(value: float, decimals: int = 2) -> str:
    """Format number with thousands separator (Argentine style)."""
    formatted = f"{value:,.{decimals}f}"
    # Swap . and , for Argentine format
    formatted = formatted.replace(",", "X").replace(".", ",").replace("X", ".")
    return formatted


def build_invoice_filename(
    cuit_emisor: str,
    tipo: int,
    punto_venta: int,
    nro_comprobante: int,
) -> str:
    """Build the standard AFIP invoice filename."""
    return f"{cuit_emisor}_{tipo:03d}_{punto_venta:05d}_{nro_comprobante:08d}.pdf"


def render_invoice_pdf(
    emisor: dict,
    receptor: dict,
    tipo_comprobante: int,
    punto_venta: int,
    comprobante_nro: str,
    fecha_emision: str,
    fecha_servicio_desde: str,
    fecha_servicio_hasta: str,
    fecha_vto_pago: str,
    concepto: str,
    importe_total: float,
    moneda: str,
    cotizacion: float,
    condicion_venta: str,
    referencia_comercial: str = "",
    observaciones: str = "",
    cae: str = "",
    cae_vto: str = "",
    pago: Optional[dict] = None,
    output_path: Optional[Path] = None,
) -> bytes:
    """Render an invoice to PDF.

    Args:
        emisor: dict with nombre, cuit, condicion_iva, domicilio, inicio_actividades
        receptor: dict with razon_social, cuit, condicion_iva, domicilio
        tipo_comprobante: AFIP code (11, 211, etc.)
        punto_venta: int
        comprobante_nro: formatted string "00000093"
        fecha_emision: DD/MM/YYYY
        fecha_servicio_desde/hasta: DD/MM/YYYY
        fecha_vto_pago: DD/MM/YYYY
        concepto: description text
        importe_total: float
        moneda: "PES" or "DOL"
        cotizacion: exchange rate (1 for PES)
        condicion_venta: "Transferencia Bancaria" etc.
        referencia_comercial: contract ref
        observaciones: free text
        cae: CAE number (empty for dry-run)
        cae_vto: CAE expiry date
        pago: dict with cbu, alias (for FCE)
        output_path: optional file path to save PDF

    Returns:
        PDF bytes
    """
    from weasyprint import HTML

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    template = env.get_template("factura_c.html")

    # Determine display values
    tipo_letra = TIPO_LETRAS.get(tipo_comprobante, "C")
    comprobante_nombre = TIPO_NOMBRES.get(tipo_comprobante, f"COMPROBANTE (Cód. {tipo_comprobante})")
    moneda_simbolo = "$" if moneda == "PES" else "USD"
    show_pago_info = tipo_comprobante in (211, 213) and pago

    # Build QR if we have CAE
    qr_image = ""
    if cae:
        # Parse fecha_emision from DD/MM/YYYY to YYYY-MM-DD for QR
        parts = fecha_emision.split("/")
        fecha_iso = f"{parts[2]}-{parts[1]}-{parts[0]}" if len(parts) == 3 else fecha_emision
        qr_image = generate_qr_data(
            fecha=fecha_iso,
            cuit_emisor=emisor["cuit"],
            punto_venta=punto_venta,
            tipo_comprobante=tipo_comprobante,
            nro_comprobante=int(comprobante_nro),
            importe=importe_total,
            moneda=moneda,
            cotizacion=cotizacion,
            cuit_receptor=receptor["cuit"],
            cae=cae,
        )

    # Build item list (single line item for services)
    items = [{
        "codigo": "",
        "descripcion": concepto,
        "cantidad": "1",
        "unidad": "otros",
        "precio_unit": f"{moneda_simbolo} {format_number(importe_total)}",
        "subtotal": f"{moneda_simbolo} {format_number(importe_total)}",
    }]

    # Render HTML
    html_content = template.render(
        emisor=emisor,
        receptor=receptor,
        tipo_letra=tipo_letra,
        tipo_comprobante=tipo_comprobante,
        comprobante_nombre=comprobante_nombre,
        punto_venta=punto_venta,
        comprobante_nro=comprobante_nro,
        fecha_emision=fecha_emision,
        fecha_servicio_desde=fecha_servicio_desde,
        fecha_servicio_hasta=fecha_servicio_hasta,
        fecha_vto_pago=fecha_vto_pago,
        condicion_venta=condicion_venta,
        items=items,
        moneda=moneda,
        moneda_simbolo=moneda_simbolo,
        cotizacion=cotizacion,
        importe_neto=importe_total,
        importe_total=importe_total,
        referencia_comercial=referencia_comercial,
        observaciones=observaciones,
        cae=cae,
        cae_vto=cae_vto,
        qr_image=qr_image,
        show_pago_info=show_pago_info,
        pago=pago or {},
    )

    # Render PDF
    pdf_bytes = HTML(string=html_content).write_pdf()

    # Save if output path given
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(pdf_bytes)

    return pdf_bytes
