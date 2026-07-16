"""Certificate setup helper for AFIP digital certificate.

Generates CSR and provides step-by-step instructions.
Usage: python -m factura setup-cert
"""
import os
import subprocess
import sys
from pathlib import Path


AFIP_DIR = Path(os.path.expanduser("~/.afip"))
KEY_PATH = AFIP_DIR / "clave_privada.key"
CSR_PATH = AFIP_DIR / "solicitud.csr"
CRT_PATH = AFIP_DIR / "certificado.crt"


def main():
    print(f"\n{'='*60}")
    print(f"  AFIP Certificate Setup")
    print(f"{'='*60}\n")

    # Check if openssl is available
    try:
        result = subprocess.run(
            ["openssl", "version"],
            capture_output=True, text=True, timeout=10,
        )
        print(f"OpenSSL: {result.stdout.strip()}")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        sys.exit(
            "Error: openssl not found in PATH.\n"
            "Install OpenSSL or use Git Bash (which includes it)."
        )

    # Create ~/.afip directory
    AFIP_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Directory: {AFIP_DIR}")

    # Check existing files
    if KEY_PATH.exists():
        print(f"\nKey already exists: {KEY_PATH}")
        resp = input("Overwrite? (y/N): ").strip().lower()
        if resp != "y":
            print("Using existing key.")
            if CRT_PATH.exists():
                print(f"Certificate: {CRT_PATH}")
                _validate_existing()
                return
            else:
                print(f"Certificate not found. Upload CSR to ARCA and download .crt")
                _print_arca_instructions()
                return

    # Generate private key + CSR
    print(f"\nGenerating RSA 2048 key + CSR...")

    # CUIT and name come from factura-config.yaml — no fallback identity
    try:
        from factura.config import load_config, CONFIG_PATH
        if not CONFIG_PATH.exists():
            raise FileNotFoundError(f"Config not found: {CONFIG_PATH}")
        config = load_config(CONFIG_PATH)
        cuit = config.emisor.cuit
        nombre = config.emisor.nombre
    except Exception as e:
        sys.exit(
            f"Error: could not load emisor data from factura-config.yaml: {e}\n"
            f"Copy factura-config.example.yaml to factura-config.yaml and fill in your data."
        )

    subject = f"/CN={nombre}/serialNumber=CUIT {cuit}"
    print(f"Subject: {subject}")

    cmd = [
        "openssl", "req",
        "-new", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(KEY_PATH),
        "-out", str(CSR_PATH),
        "-subj", subject,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    if result.returncode != 0:
        print(f"Error: {result.stderr}")
        sys.exit(1)

    print(f"\nKey:  {KEY_PATH}")
    print(f"CSR:  {CSR_PATH}")
    print(f"\nCSR generated successfully.")

    _print_arca_instructions()


def _validate_existing():
    """Validate existing cert+key pair."""
    try:
        from factura.afip_auth import validate_certificate
        info = validate_certificate(KEY_PATH, CRT_PATH)
        print(f"\nCertificate valid:")
        print(f"  CN: {info['cn']}")
        print(f"  Expires: {info['not_valid_after']}")
        print(f"  Issuer: {info['issuer']}")
        print(f"\nCertificate is ready for use.")
    except SystemExit as e:
        print(f"\nCertificate validation failed: {e}")
    except Exception as e:
        print(f"\nCertificate validation error: {e}")


def _print_arca_instructions():
    """Print step-by-step ARCA instructions."""
    print(f"\n{'='*60}")
    print(f"  PASOS EN ARCA (portal AFIP)")
    print(f"{'='*60}")
    lines = [
        "",
        "1. Ir a https://auth.afip.gob.ar/contribuyente_/login.xhtml",
        "2. Ingresar con CUIT + Clave Fiscal (nivel 3+)",
        "",
        '3. Buscar: "Administracion de certificados digitales"',
        "   (Si no aparece -> Administrador de relaciones -> Adherir servicio)",
        "",
        '4. Click "Agregar alias del certificado"',
        '   - Alias: "factura-cli" (o cualquier nombre descriptivo)',
        "",
        '5. Click "Generar certificado"',
        '   - Seleccionar "Ingresar CSR manualmente"',
        f"   - Copiar y pegar el contenido de: {CSR_PATH}",
        '   - Click "Crear"',
        "",
        "6. Descargar el certificado (.crt)",
        f"   - Guardar como: {CRT_PATH}",
        "",
        "7. Adherir servicios al certificado:",
        '   - "Administrador de relaciones de clave fiscal"',
        '   - Agregar relacion -> buscar "AFIP" -> "Web Services"',
        '   - Seleccionar "wsfe" (Factura Electronica)',
        "   - Asociar al certificado recien creado",
        "",
        "8. Verificar con:",
        "   python -m factura setup-cert",
        '   (Deberia mostrar "Certificate is ready for use")',
        "",
        "9. Probar en homologacion:",
        "   python -m factura acme 1 --homo",
        "",
    ]
    for line in lines:
        print(line)
    print(f"Para ver el contenido del CSR:")
    print(f"  cat {CSR_PATH}")
    print(f"{'='*60}\n")
