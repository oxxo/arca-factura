"""Invoice ledger — local tracking of emitted comprobantes.

Reads/writes invoices.yaml. Provides idempotency checks.
"""
import datetime
from pathlib import Path
from typing import Optional

import yaml

from factura.config import INVOICES_PATH


def load_ledger(path: Path = INVOICES_PATH) -> dict:
    """Load the invoices.yaml ledger."""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def save_ledger(data: dict, path: Path = INVOICES_PATH) -> None:
    """Save the invoices.yaml ledger."""
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def ledger_key(project_slug: str, payment_number: int, suffix: str = "") -> str:
    """Generate the ledger key for a project+payment.

    suffix="nc" for nota de crédito entries.
    """
    key = f"{project_slug}-{payment_number}"
    if suffix:
        key += f"-{suffix}"
    return key


def check_already_emitted(
    project_slug: str, payment_number: int, path: Path = INVOICES_PATH, suffix: str = ""
) -> Optional[dict]:
    """Check if a comprobante was already emitted for this project+payment.

    Returns the existing entry if found, None otherwise.
    Use suffix="nc" to check for nota de crédito.
    """
    ledger = load_ledger(path)
    key = ledger_key(project_slug, payment_number, suffix)
    return ledger.get(key)


def get_annual_total(year: int = None, path: Path = INVOICES_PATH) -> float:
    """Sum all ARS amounts emitted in the given year (default: current year).

    For USD invoices, uses amount * cotizacion.
    Excludes NC entries (tipo 013, 213).
    """
    if year is None:
        year = datetime.date.today().year

    ledger = load_ledger(path)
    total = 0.0
    for key, entry in ledger.items():
        if "-nc" in key:
            continue
        tipo = entry.get("tipo", 0)
        if tipo in (13, 213):
            continue
        emitted = entry.get("emitted", "")
        if emitted.startswith(str(year)):
            monto = entry.get("monto", 0)
            moneda = entry.get("moneda", "PES")
            cotiz = entry.get("cotizacion", 1)
            if moneda == "DOL":
                total += monto * cotiz
            else:
                total += monto
    return total


def record_invoice(
    project_slug: str,
    payment_number: int,
    comprobante: str,
    tipo: int,
    cae: str,
    cae_vto: str,
    monto: float,
    moneda: str,
    cotizacion: float,
    concepto: str,
    filename: str,
    suffix: str = "",
    path: Path = INVOICES_PATH,
) -> None:
    """Record a newly emitted invoice in the ledger."""
    ledger = load_ledger(path)
    key = ledger_key(project_slug, payment_number, suffix)

    ledger[key] = {
        "comprobante": comprobante,
        "tipo": tipo,
        "cae": cae,
        "cae_vto": cae_vto,
        "monto": monto,
        "moneda": moneda,
        "cotizacion": cotizacion,
        "concepto": concepto,
        "filename": filename,
        "emitted": datetime.date.today().isoformat(),
    }

    save_ledger(ledger, path)


def append_audit_log(message: str) -> None:
    """Append a line to the audit log."""
    log_path = Path(__file__).resolve().parent / "audit.log"
    timestamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{timestamp} {message}\n")
