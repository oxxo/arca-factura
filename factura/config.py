"""Configuration loader for AFIP invoice tool.

Reads factura-config.yaml (emisor data) and projects-registry.yaml (client data).
Registry is READ-ONLY — never written programmatically.
"""
import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import yaml


# Resolve paths relative to the repo root
TOOL_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = TOOL_ROOT / "factura-config.yaml"
INVOICES_PATH = TOOL_ROOT / "invoices.yaml"


@dataclass
class Emisor:
    nombre: str
    cuit: str
    condicion_iva: str
    domicilio: str
    punto_venta: int
    inicio_actividades: str


@dataclass
class Certificado:
    key_path: str
    crt_path: str

    @property
    def key_resolved(self) -> Path:
        return Path(os.path.expanduser(self.key_path))

    @property
    def crt_resolved(self) -> Path:
        return Path(os.path.expanduser(self.crt_path))


@dataclass
class Pago:
    cbu: str
    alias: str
    condicion: str


@dataclass
class PdfConfig:
    output_base: str  # Empty = use project path


@dataclass
class AppConfig:
    emisor: Emisor
    certificado: Certificado
    ambiente: str  # "homologacion" | "produccion"
    pago: Pago
    pdf: PdfConfig
    registry_path: str


@dataclass
class Billing:
    razon_social: str
    cuit: str
    condicion_iva: str
    domicilio: str = ""
    tipo_comprobante: int = 11
    referencia_comercial: str = ""
    opcion_transferencia: str = "SCA"
    certificado_mipyme: bool = False


@dataclass
class Payment:
    number: int
    amount: float
    currency: str
    due: str = ""
    status: str = ""
    description: str = ""
    percentage: Optional[int] = None
    trigger: str = ""


@dataclass
class ProjectData:
    id: str
    name: str
    path: str
    billing: Billing
    payments: list  # list[Payment]
    currency: str = "USD"


def load_config(config_path: Path = CONFIG_PATH) -> AppConfig:
    """Load the main factura-config.yaml."""
    if not config_path.exists():
        sys.exit(f"Error: Config not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    return AppConfig(
        emisor=Emisor(**raw["emisor"]),
        certificado=Certificado(**raw["certificado"]),
        ambiente=raw.get("ambiente", "homologacion"),
        pago=Pago(**raw["pago"]),
        pdf=PdfConfig(**raw.get("pdf", {"output_base": ""})),
        registry_path=raw.get("registry_path", ""),
    )


def load_registry(registry_path: str) -> list:
    """Load the projects-registry.yaml (READ-ONLY)."""
    path = Path(registry_path)
    if not path.exists():
        sys.exit(f"Error: Registry not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    return raw.get("projects", [])


def find_project(registry_projects: list, slug: str) -> ProjectData:
    """Find a project by slug (matches against id prefix or name, case-insensitive).

    Prioritizes projects that have a billing block. Among matches,
    prefers the one whose id is closest to the slug.

    Examples:
        find_project(projects, "acme") → matches "acme-001"
        find_project(projects, "globex-app") → matches "globex-app-001"
    """
    slug_lower = slug.lower()

    # Collect all matches, prioritize those with billing
    candidates = []
    for proj in registry_projects:
        proj_id = proj.get("id", "").lower()
        proj_name = proj.get("name", "").lower()

        if proj_id.startswith(slug_lower) or slug_lower in proj_name:
            has_billing = bool(proj.get("billing"))
            # Score: prefer exact id prefix match + has billing + shorter id (more specific match)
            candidates.append((proj, has_billing, len(proj_id)))

    # Sort: has_billing first, then shortest id (closest match)
    candidates.sort(key=lambda x: (not x[1], x[2]))

    if not candidates:
        sys.exit(f"Error: Project '{slug}' not found in registry.")

    proj = candidates[0][0]

    # Extract billing data
    billing_raw = proj.get("billing")
    if not billing_raw:
        sys.exit(
            f"Error: Project '{proj['id']}' has no 'billing' block in registry.\n"
            f"Add a billing section with: razon_social, cuit, condicion_iva\n"
            f"See plan for schema."
        )

    billing = Billing(
        razon_social=billing_raw["razon_social"],
        cuit=billing_raw["cuit"],
        condicion_iva=billing_raw.get("condicion_iva", ""),
        domicilio=billing_raw.get("domicilio", ""),
        tipo_comprobante=billing_raw.get("tipo_comprobante", 11),
        referencia_comercial=billing_raw.get("referencia_comercial", ""),
        opcion_transferencia=billing_raw.get("opcion_transferencia", "SCA"),
        certificado_mipyme=billing_raw.get("certificado_mipyme", False),
    )

    # Extract payments
    currency = proj.get("financials", {}).get("currency", "USD")
    payments_raw = proj.get("payments", [])
    payments = []
    for p in payments_raw:
        payments.append(Payment(
            number=p["number"],
            amount=p["amount"],
            currency=currency,
            due=str(p.get("due", "")),
            status=p.get("status", ""),
            description=p.get("description", ""),
            percentage=p.get("percentage"),
            trigger=p.get("trigger", ""),
        ))

    return ProjectData(
        id=proj["id"],
        name=proj["name"],
        path=proj.get("path", ""),
        billing=billing,
        payments=payments,
        currency=currency,
    )


def find_payment(project: ProjectData, payment_number: int) -> Payment:
    """Find a payment by number within a project."""
    for p in project.payments:
        if p.number == payment_number:
            return p

    available = [str(p.number) for p in project.payments]
    sys.exit(
        f"Error: Payment #{payment_number} not found for '{project.name}'.\n"
        f"Available payments: {', '.join(available)}"
    )
