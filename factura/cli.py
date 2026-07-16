"""CLI entry point for AFIP invoice tool.

Usage: python -m factura <proyecto> <pago#> [--dry-run] [--homo] [--tipo 011|211]
"""
import argparse
import datetime
import sys
from pathlib import Path

from factura.config import (
    load_config,
    load_registry,
    find_project,
    find_payment,
    CONFIG_PATH,
)
from factura.ledger import (
    check_already_emitted,
    record_invoice,
    append_audit_log,
    get_annual_total,
)
from factura.exchange_rate import get_exchange_rate
from factura.pdf_gen import (
    check_weasyprint,
    render_invoice_pdf,
    build_invoice_filename,
    format_number,
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="AFIP invoice automation tool",
        usage="python -m factura <proyecto> <pago#> [options]",
    )
    parser.add_argument("proyecto", help="Project slug (e.g., acme, globex)")
    parser.add_argument("pago", type=int, help="Payment number (e.g., 3)")
    parser.add_argument("--dry-run", action="store_true", help="Generate PDF without CAE (no AFIP connection)")
    parser.add_argument("--homo", action="store_true", help="Use AFIP homologación environment")
    parser.add_argument("--tipo", type=int, help="Override comprobante type (e.g., 011, 211)")
    parser.add_argument("--desde", help="Service period start (YYYYMMDD)")
    parser.add_argument("--hasta", help="Service period end (YYYYMMDD)")
    parser.add_argument("--vto", help="Payment due date (YYYYMMDD)")
    parser.add_argument("--concepto-override", help="Override invoice description")
    parser.add_argument("--monto", type=float, help="Override amount (ARS). Forces MonId=PES.")
    parser.add_argument("--nc", action="store_true", help="Emit Nota de Crédito for existing factura")
    parser.add_argument("--cotizacion", type=float, help="Override exchange rate (e.g., MEP rate). Skips BNA/AFIP lookup.")
    return parser.parse_args()


def format_date_display(yyyymmdd: str) -> str:
    """Convert YYYYMMDD to DD/MM/YYYY for display."""
    if len(yyyymmdd) == 10 and "-" in yyyymmdd:
        # Already YYYY-MM-DD
        parts = yyyymmdd.split("-")
        return f"{parts[2]}/{parts[1]}/{parts[0]}"
    if len(yyyymmdd) == 8:
        return f"{yyyymmdd[6:8]}/{yyyymmdd[4:6]}/{yyyymmdd[0:4]}"
    return yyyymmdd


def main():
    args = parse_args()

    # Step 1: Load config
    print(f"\n{'='*60}")
    print(f"  AFIP Invoice Tool {'(DRY-RUN)' if args.dry_run else ''}")
    print(f"{'='*60}\n")

    config = load_config(CONFIG_PATH)
    print(f"Emisor: {config.emisor.nombre} (CUIT {config.emisor.cuit})")
    print(f"Ambiente: {config.ambiente}")

    # Step 2: Load registry and find project
    registry = load_registry(config.registry_path)
    project = find_project(registry, args.proyecto)
    payment = find_payment(project, args.pago)

    print(f"\nProyecto: {project.name} ({project.id})")
    print(f"Pago #{payment.number}: {payment.currency} {payment.amount}")
    if payment.description:
        print(f"Descripción: {payment.description}")

    # Step 3: Check idempotency
    if args.nc:
        # For NC: original factura MUST exist
        existing = check_already_emitted(args.proyecto, args.pago)
        if not existing:
            sys.exit(
                f"Error: No factura found for {args.proyecto} pago #{args.pago}.\n"
                f"Cannot create Nota de Crédito without original factura."
            )
        # Check NC not already emitted
        nc_existing = check_already_emitted(args.proyecto, args.pago, suffix="nc")
        if nc_existing:
            print(f"\n*** NC YA EMITIDA ***")
            print(f"Comprobante: {nc_existing['comprobante']}")
            print(f"CAE: {nc_existing.get('cae', 'N/A')}")
            print(f"Fecha: {nc_existing.get('emitted', 'N/A')}")
            sys.exit(0)
    else:
        existing = check_already_emitted(args.proyecto, args.pago)
        if existing:
            print(f"\n*** YA EMITIDO ***")
            print(f"Comprobante: {existing['comprobante']}")
            print(f"CAE: {existing.get('cae', 'N/A')}")
            print(f"Fecha: {existing.get('emitted', 'N/A')}")
            print(f"Archivo: {existing.get('filename', 'N/A')}")
            sys.exit(0)

    # Step 4: Determine comprobante type
    tipo = args.tipo or project.billing.tipo_comprobante
    print(f"\nTipo comprobante: {tipo}")

    # Step 5: Exchange rate / amount override
    moneda = "PES"
    cotizacion = 1.0
    importe_ars = payment.amount

    if args.monto:
        # Manual ARS override — skip exchange rate lookup
        moneda = "PES"
        cotizacion = 1.0
        importe_ars = args.monto
        print(f"\nMonto override: ${format_number(importe_ars)} ARS (MonId=PES)")
    elif payment.currency == "USD":
        moneda = "DOL"
        if args.cotizacion:
            cotizacion = args.cotizacion
            print(f"\nMoneda: USD — cotización manual: {cotizacion}")
        else:
            print(f"\nMoneda: USD — obteniendo cotización BNA oficial...")
            try:
                cotizacion = get_exchange_rate()
                print(f"Cotización BNA vendedor: {cotizacion}")
            except RuntimeError as e:
                print(f"Warning: {e}")
                print("Usando cotización manual. Ingrese cotización USD→ARS:")
                cotizacion = float(input("> "))
        importe_ars = payment.amount * cotizacion
        print(f"Equivalente ARS: ${format_number(importe_ars)}")

    # Step 6: Determine dates
    today = datetime.date.today()
    today_fmt = today.strftime("%Y%m%d")

    desde = args.desde or today_fmt
    hasta = args.hasta or today_fmt
    vto = args.vto or (today + datetime.timedelta(days=10)).strftime("%Y%m%d")

    # Step 7: Build concepto
    total_payments = len(project.payments)
    concepto = args.concepto_override or (
        f"{payment.description or project.name} - Pago {payment.number}/{total_payments}"
    )

    # Step 8: Determine ambiente
    ambiente = "homologacion" if args.homo else config.ambiente

    # Step 9: Common data structures
    emisor_dict = {
        "nombre": config.emisor.nombre,
        "cuit": config.emisor.cuit,
        "condicion_iva": config.emisor.condicion_iva,
        "domicilio": config.emisor.domicilio,
        "inicio_actividades": config.emisor.inicio_actividades,
    }

    receptor_dict = {
        "razon_social": project.billing.razon_social,
        "cuit": project.billing.cuit,
        "condicion_iva": project.billing.condicion_iva,
        "domicilio": project.billing.domicilio,
    }

    pago_dict = {
        "cbu": config.pago.cbu,
        "alias": config.pago.alias,
    } if tipo in (211, 213) else None

    # Output directory
    if config.pdf.output_base:
        output_dir = Path(config.pdf.output_base)
    else:
        output_dir = Path(project.path) / "docs" / "facturas"

    # Nota de crédito mode
    if args.nc:
        _run_nota_credito(
            args, config, project, payment, tipo, moneda, cotizacion,
            importe_ars, desde, hasta, vto, concepto, emisor_dict,
            receptor_dict, pago_dict, output_dir, ambiente,
        )
        return

    if args.dry_run:
        _run_dry_run(
            args, config, project, payment, tipo, moneda, cotizacion,
            importe_ars, desde, hasta, vto, concepto, emisor_dict,
            receptor_dict, pago_dict, output_dir,
        )
    else:
        _run_live(
            args, config, project, payment, tipo, moneda, cotizacion,
            importe_ars, desde, hasta, vto, concepto, emisor_dict,
            receptor_dict, pago_dict, output_dir, ambiente,
        )


def _run_dry_run(
    args, config, project, payment, tipo, moneda, cotizacion,
    importe_ars, desde, hasta, vto, concepto, emisor_dict,
    receptor_dict, pago_dict, output_dir,
):
    """Dry-run mode: generate PDF without AFIP connection."""
    comprobante_nro = "XXXXXXXX"
    print(f"\n--- DRY-RUN: número de comprobante será asignado por AFIP ---")

    filename = build_invoice_filename(
        config.emisor.cuit, tipo, config.emisor.punto_venta, 0
    ).replace("_00000000.pdf", f"_{comprobante_nro}.pdf")
    output_path = output_dir / filename

    print(f"\nGenerando PDF...")
    check_weasyprint()

    today_fmt = datetime.date.today().strftime("%Y%m%d")
    pdf_bytes = render_invoice_pdf(
        emisor=emisor_dict,
        receptor=receptor_dict,
        tipo_comprobante=tipo,
        punto_venta=config.emisor.punto_venta,
        comprobante_nro=comprobante_nro,
        fecha_emision=format_date_display(today_fmt),
        fecha_servicio_desde=format_date_display(desde),
        fecha_servicio_hasta=format_date_display(hasta),
        fecha_vto_pago=format_date_display(vto),
        concepto=concepto,
        importe_total=payment.amount if moneda == "DOL" else importe_ars,
        moneda=moneda,
        cotizacion=cotizacion,
        condicion_venta=config.pago.condicion,
        referencia_comercial=project.billing.referencia_comercial,
        cae="",
        cae_vto="",
        pago=pago_dict,
        output_path=output_path,
    )

    print(f"PDF generado: {output_path}")
    print(f"Tamaño: {len(pdf_bytes)} bytes")

    print(f"\n{'='*60}")
    print(f"  RESUMEN (DRY-RUN — sin validez fiscal)")
    print(f"{'='*60}")
    print(f"Emisor:      {config.emisor.nombre} (CUIT {config.emisor.cuit})")
    print(f"Receptor:    {project.billing.razon_social} (CUIT {project.billing.cuit})")
    print(f"Tipo:        {tipo}")
    print(f"Concepto:    {concepto}")
    if moneda == "DOL":
        print(f"Importe:     USD {format_number(payment.amount)}")
        print(f"Cotización:  {cotizacion}")
        print(f"Equiv. ARS:  ${format_number(importe_ars)}")
    else:
        print(f"Importe:     ${format_number(importe_ars)}")
    print(f"Ref. Comercial: {project.billing.referencia_comercial}")
    print(f"PDF:         {output_path}")
    print(f"\nPara emitir con CAE real: quitar --dry-run")
    print(f"{'='*60}\n")

    append_audit_log(
        f"DRY_RUN {args.proyecto} pago={args.pago} tipo={tipo} "
        f"monto={payment.amount} {payment.currency} pdf={output_path.name}"
    )

    _print_annual_summary()


def _run_live(
    args, config, project, payment, tipo, moneda, cotizacion,
    importe_ars, desde, hasta, vto, concepto, emisor_dict,
    receptor_dict, pago_dict, output_dir, ambiente,
):
    """Live mode: authenticate with WSAA, emit comprobante via WSFEv1, generate PDF with CAE."""
    from factura.afip_auth import authenticate
    from factura.afip_wsfe import WSFEClient

    today = datetime.date.today()
    today_fmt = today.strftime("%Y%m%d")

    # Step 1: Authenticate with WSAA
    print(f"\nAutenticando con WSAA ({ambiente})...")
    token_data = authenticate(
        key_path=config.certificado.key_resolved,
        crt_path=config.certificado.crt_resolved,
        ambiente=ambiente,
    )

    # Step 2: Create WSFEv1 client
    wsfe = WSFEClient(
        token=token_data["token"],
        sign=token_data["sign"],
        cuit=config.emisor.cuit,
        ambiente=ambiente,
    )

    # Step 3: Get official exchange rate if USD (skip if manual override)
    if moneda == "DOL" and not args.cotizacion:
        print(f"\nObteniendo cotización oficial AFIP...")
        try:
            afip_cotiz = wsfe.get_cotizacion("DOL")
            print(f"Cotización AFIP/BNA: {afip_cotiz}")
            cotizacion = afip_cotiz
            importe_ars = payment.amount * cotizacion
            print(f"Equivalente ARS: ${format_number(importe_ars)}")
        except Exception as e:
            print(f"Warning: no se pudo obtener cotización AFIP ({e}), usando dolarapi: {cotizacion}")
    elif moneda == "DOL" and args.cotizacion:
        print(f"\nUsando cotización manual: {cotizacion} (override --cotizacion)")

    # Step 4: Get last comprobante number
    print(f"\nConsultando último comprobante...")
    ultimo = wsfe.get_ultimo_autorizado(config.emisor.punto_venta, tipo)
    next_num = ultimo + 1
    comprobante_nro = f"{next_num:08d}"
    print(f"Último: {ultimo}, Próximo: {next_num}")

    # Step 5: Build opcionales for FCE
    opcionales = None
    if tipo in (211, 213):
        opcionales = [
            {"Id": 2101, "Valor": config.pago.cbu},
            {"Id": 2102, "Valor": config.pago.alias},
            {"Id": 27, "Valor": project.billing.opcion_transferencia},
        ]

    # Step 6: Emit comprobante
    print(f"\nEmitiendo comprobante {comprobante_nro} ({ambiente})...")
    imp_total = payment.amount if moneda == "DOL" else importe_ars

    result = wsfe.solicitar_cae(
        punto_venta=config.emisor.punto_venta,
        tipo_cbte=tipo,
        concepto=2,  # Servicios
        doc_tipo=80,  # CUIT
        doc_nro=int(project.billing.cuit),
        cbte_desde=next_num,
        cbte_hasta=next_num,
        cbte_fch=today_fmt,
        imp_total=imp_total,
        imp_neto=imp_total,
        mon_id=moneda,
        mon_cotiz=cotizacion,
        fch_serv_desde=desde,
        fch_serv_hasta=hasta,
        fch_vto_pago=vto,
        opcionales=opcionales,
    )

    cae = result["cae"]
    cae_vto_raw = result["cae_fch_vto"]  # YYYYMMDD
    print(f"CAE: {cae}")
    print(f"CAE Vto: {cae_vto_raw}")

    # Step 7: Generate PDF with CAE
    print(f"\nGenerando PDF con CAE...")
    check_weasyprint()

    filename = build_invoice_filename(
        config.emisor.cuit, tipo, config.emisor.punto_venta, next_num
    )
    output_path = output_dir / filename

    pdf_bytes = render_invoice_pdf(
        emisor=emisor_dict,
        receptor=receptor_dict,
        tipo_comprobante=tipo,
        punto_venta=config.emisor.punto_venta,
        comprobante_nro=comprobante_nro,
        fecha_emision=format_date_display(today_fmt),
        fecha_servicio_desde=format_date_display(desde),
        fecha_servicio_hasta=format_date_display(hasta),
        fecha_vto_pago=format_date_display(vto),
        concepto=concepto,
        importe_total=imp_total,
        moneda=moneda,
        cotizacion=cotizacion,
        condicion_venta=config.pago.condicion,
        referencia_comercial=project.billing.referencia_comercial,
        cae=cae,
        cae_vto=format_date_display(cae_vto_raw),
        pago=pago_dict,
        output_path=output_path,
    )

    print(f"PDF generado: {output_path}")
    print(f"Tamaño: {len(pdf_bytes)} bytes")

    # Step 8: Record in ledger
    # cae_vto_raw is YYYYMMDD, convert to YYYY-MM-DD for ledger
    cae_vto_iso = f"{cae_vto_raw[0:4]}-{cae_vto_raw[4:6]}-{cae_vto_raw[6:8]}" if len(cae_vto_raw) == 8 else cae_vto_raw

    record_invoice(
        project_slug=args.proyecto,
        payment_number=args.pago,
        comprobante=comprobante_nro,
        tipo=tipo,
        cae=cae,
        cae_vto=cae_vto_iso,
        monto=imp_total,
        moneda=moneda,
        cotizacion=cotizacion,
        concepto=concepto,
        filename=filename,
    )

    # Step 9: Audit log
    append_audit_log(
        f"EMITIDA {args.proyecto} pago={args.pago} cbte={comprobante_nro} "
        f"cae={cae} tipo={tipo} monto={payment.amount} {payment.currency} "
        f"ambiente={ambiente} pdf={filename}"
    )

    # Step 10: Summary
    print(f"\n{'='*60}")
    print(f"  FACTURA EMITIDA EXITOSAMENTE")
    print(f"{'='*60}")
    print(f"Emisor:      {config.emisor.nombre} (CUIT {config.emisor.cuit})")
    print(f"Receptor:    {project.billing.razon_social} (CUIT {project.billing.cuit})")
    print(f"Tipo:        {tipo}")
    print(f"Comprobante: {comprobante_nro}")
    print(f"CAE:         {cae}")
    print(f"CAE Vto:     {format_date_display(cae_vto_raw)}")
    print(f"Concepto:    {concepto}")
    if moneda == "DOL":
        print(f"Importe:     USD {format_number(payment.amount)}")
        print(f"Cotización:  {cotizacion}")
        print(f"Equiv. ARS:  ${format_number(importe_ars)}")
    else:
        print(f"Importe:     ${format_number(imp_total)}")
    print(f"Ref. Comercial: {project.billing.referencia_comercial}")
    print(f"PDF:         {output_path}")
    print(f"Ambiente:    {ambiente}")
    print(f"{'='*60}\n")

    _print_annual_summary()


def _nc_tipo(original_tipo: int) -> int:
    """Map factura type to its Nota de Crédito counterpart."""
    NC_MAP = {11: 13, 211: 213}
    nc = NC_MAP.get(original_tipo)
    if not nc:
        sys.exit(f"Error: No NC type mapping for tipo {original_tipo}")
    return nc


def _run_nota_credito(
    args, config, project, payment, tipo, moneda, cotizacion,
    importe_ars, desde, hasta, vto, concepto, emisor_dict,
    receptor_dict, pago_dict, output_dir, ambiente,
):
    """Emit a Nota de Crédito for an existing factura."""
    from factura.afip_auth import authenticate
    from factura.afip_wsfe import WSFEClient

    # Get original factura data from ledger
    original = check_already_emitted(args.proyecto, args.pago)
    original_tipo = original["tipo"]
    nc_tipo = _nc_tipo(original_tipo)
    original_nro = int(original["comprobante"])

    print(f"\n--- NOTA DE CRÉDITO ---")
    print(f"Factura original: {original['comprobante']} (tipo {original_tipo})")
    print(f"NC tipo: {nc_tipo}")
    print(f"Monto a acreditar: {payment.currency} {payment.amount}")

    today = datetime.date.today()
    today_fmt = today.strftime("%Y%m%d")

    # Build CbtesAsoc (required for NC)
    cbtes_asoc = [{
        "Tipo": original_tipo,
        "PtoVta": config.emisor.punto_venta,
        "Nro": original_nro,
        "Cuit": config.emisor.cuit,
        "CbteFch": original.get("emitted", today_fmt).replace("-", ""),
    }]

    if args.dry_run:
        # Dry-run NC
        comprobante_nro = "XXXXXXXX"
        filename = build_invoice_filename(
            config.emisor.cuit, nc_tipo, config.emisor.punto_venta, 0
        ).replace("_00000000.pdf", f"_{comprobante_nro}.pdf")
        output_path = output_dir / filename

        check_weasyprint()
        nc_concepto = f"NC: {concepto}"

        pdf_bytes = render_invoice_pdf(
            emisor=emisor_dict,
            receptor=receptor_dict,
            tipo_comprobante=nc_tipo,
            punto_venta=config.emisor.punto_venta,
            comprobante_nro=comprobante_nro,
            fecha_emision=format_date_display(today_fmt),
            fecha_servicio_desde=format_date_display(desde),
            fecha_servicio_hasta=format_date_display(hasta),
            fecha_vto_pago=format_date_display(vto),
            concepto=nc_concepto,
            importe_total=payment.amount if moneda == "DOL" else importe_ars,
            moneda=moneda,
            cotizacion=cotizacion,
            condicion_venta=config.pago.condicion,
            referencia_comercial=project.billing.referencia_comercial,
            cae="",
            cae_vto="",
            pago=pago_dict,
            output_path=output_path,
        )

        print(f"\nPDF NC generado (DRY-RUN): {output_path}")
        print(f"Tamaño: {len(pdf_bytes)} bytes")
        append_audit_log(
            f"DRY_RUN_NC {args.proyecto} pago={args.pago} nc_tipo={nc_tipo} "
            f"monto={payment.amount} {payment.currency} pdf={output_path.name}"
        )
        return

    # Live NC
    print(f"\nAutenticando con WSAA ({ambiente})...")
    token_data = authenticate(
        key_path=config.certificado.key_resolved,
        crt_path=config.certificado.crt_resolved,
        ambiente=ambiente,
    )

    wsfe = WSFEClient(
        token=token_data["token"],
        sign=token_data["sign"],
        cuit=config.emisor.cuit,
        ambiente=ambiente,
    )

    # Get last NC number
    ultimo = wsfe.get_ultimo_autorizado(config.emisor.punto_venta, nc_tipo)
    next_num = ultimo + 1
    comprobante_nro = f"{next_num:08d}"
    print(f"Último NC tipo {nc_tipo}: {ultimo}, Próximo: {next_num}")

    imp_total = payment.amount if moneda == "DOL" else importe_ars
    nc_concepto = f"NC: {concepto}"

    # NC MiPyME (213): only needs Id 22 "Anulación" = "S"
    # Must NOT include CBU (2101), Alias (2102), or Transferencia (27)
    opcionales = [{"Id": 22, "Valor": "S"}]

    print(f"\nEmitiendo NC {comprobante_nro} ({ambiente})...")
    result = wsfe.solicitar_cae(
        punto_venta=config.emisor.punto_venta,
        tipo_cbte=nc_tipo,
        concepto=2,
        doc_tipo=80,
        doc_nro=int(project.billing.cuit),
        cbte_desde=next_num,
        cbte_hasta=next_num,
        cbte_fch=today_fmt,
        imp_total=imp_total,
        imp_neto=imp_total,
        mon_id=moneda,
        mon_cotiz=cotizacion,
        fch_serv_desde=desde,
        fch_serv_hasta=hasta,
        fch_vto_pago=vto,
        opcionales=opcionales,
        cbtes_asoc=cbtes_asoc,
    )

    cae = result["cae"]
    cae_vto_raw = result["cae_fch_vto"]
    print(f"CAE: {cae}")
    print(f"CAE Vto: {cae_vto_raw}")

    # Generate PDF
    check_weasyprint()
    filename = build_invoice_filename(
        config.emisor.cuit, nc_tipo, config.emisor.punto_venta, next_num
    )
    output_path = output_dir / filename

    pdf_bytes = render_invoice_pdf(
        emisor=emisor_dict,
        receptor=receptor_dict,
        tipo_comprobante=nc_tipo,
        punto_venta=config.emisor.punto_venta,
        comprobante_nro=comprobante_nro,
        fecha_emision=format_date_display(today_fmt),
        fecha_servicio_desde=format_date_display(desde),
        fecha_servicio_hasta=format_date_display(hasta),
        fecha_vto_pago=format_date_display(vto),
        concepto=nc_concepto,
        importe_total=imp_total,
        moneda=moneda,
        cotizacion=cotizacion,
        condicion_venta=config.pago.condicion,
        referencia_comercial=project.billing.referencia_comercial,
        cae=cae,
        cae_vto=format_date_display(cae_vto_raw),
        pago=pago_dict,
        output_path=output_path,
    )

    # Record in ledger with nc suffix
    record_invoice(
        project_slug=args.proyecto,
        payment_number=args.pago,
        comprobante=comprobante_nro,
        tipo=nc_tipo,
        cae=cae,
        cae_vto=format_date_display(cae_vto_raw),
        monto=payment.amount,
        moneda=moneda,
        cotizacion=cotizacion,
        concepto=nc_concepto,
        filename=filename,
        suffix="nc",
    )

    append_audit_log(
        f"NC_EMITIDA {args.proyecto} pago={args.pago} cbte={comprobante_nro} "
        f"cae={cae} nc_tipo={nc_tipo} monto={payment.amount} {payment.currency} "
        f"ambiente={ambiente} pdf={filename}"
    )

    print(f"\n{'='*60}")
    print(f"  NOTA DE CRÉDITO EMITIDA EXITOSAMENTE")
    print(f"{'='*60}")
    print(f"Tipo NC:     {nc_tipo}")
    print(f"Comprobante: {comprobante_nro}")
    print(f"CAE:         {cae}")
    print(f"Ref. original: tipo {original_tipo} nro {original['comprobante']}")
    if moneda == "DOL":
        print(f"Importe:     USD {format_number(payment.amount)}")
    else:
        print(f"Importe:     ${format_number(imp_total)}")
    print(f"PDF:         {output_path}")
    print(f"{'='*60}\n")


def _print_annual_summary():
    """Print accumulated annual invoicing total with Monotributo warning."""
    total = get_annual_total()
    year = datetime.date.today().year

    # Monotributo category caps (servicios, 2025/2026 approximate)
    MONO_CAPS = {
        "H": 43_500_638,
        "I": 52_293_811,
    }

    print(f"\nFacturación acumulada {year}: ${format_number(total)}")

    # Warn at 80% of category H (highest common for services)
    cap_h = MONO_CAPS["H"]
    if total > cap_h * 0.8:
        pct = total / cap_h * 100
        print(f"*** ADVERTENCIA: {pct:.0f}% del tope categoría H (${format_number(cap_h)}) ***")
        if total > cap_h:
            print(f"*** SUPERASTE el tope categoría H. Verificar recategorización. ***")


if __name__ == "__main__":
    main()
