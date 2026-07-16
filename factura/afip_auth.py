"""WSAA (Web Service de Autenticación y Autorización) client.

Handles:
1. TRA XML generation
2. CMS/PKCS#7 signing with certificate
3. SOAP call to LoginCms
4. Token caching
"""
import base64
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs7, Encoding


# WSAA endpoints
WSAA_URLS = {
    "homologacion": "https://wsaahomo.afip.gov.ar/ws/services/LoginCms?WSDL",
    "produccion": "https://wsaa.afip.gob.ar/ws/services/LoginCms?WSDL",
}

TOKEN_CACHE_PATH = Path(os.path.expanduser("~/.afip/token_cache.json"))


def validate_certificate(key_path: Path, crt_path: Path) -> dict:
    """Validate the AFIP certificate and key pair.

    Returns dict with cert info. Exits on error.
    """
    if not key_path.exists():
        sys.exit(f"Error: Private key not found: {key_path}")
    if not crt_path.exists():
        sys.exit(f"Error: Certificate not found: {crt_path}")

    # Load and parse certificate
    crt_pem = crt_path.read_bytes()
    cert = x509.load_pem_x509_certificate(crt_pem)

    # Check expiry
    now = datetime.datetime.now(datetime.timezone.utc)
    if cert.not_valid_after_utc < now:
        sys.exit(
            f"Error: Certificate expired on {cert.not_valid_after_utc.isoformat()}\n"
            f"Generate a new one from ARCA portal."
        )

    # Load key
    key_pem = key_path.read_bytes()
    key = serialization.load_pem_private_key(key_pem, password=None)

    # Verify key matches cert
    cert_pubkey_bytes = cert.public_key().public_bytes(
        Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    key_pubkey_bytes = key.public_key().public_bytes(
        Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    if cert_pubkey_bytes != key_pubkey_bytes:
        sys.exit("Error: Private key does not match certificate.")

    cn = ""
    for attr in cert.subject:
        if attr.oid == x509.oid.NameOID.COMMON_NAME:
            cn = attr.value

    return {
        "cn": cn,
        "not_valid_after": cert.not_valid_after_utc.isoformat(),
        "issuer": cert.issuer.rfc4514_string(),
        "key": key,
        "cert": cert,
    }


def build_tra_xml(service: str = "wsfe") -> bytes:
    """Build TRA (Ticket de Requerimiento de Acceso) XML."""
    now = datetime.datetime.now(datetime.timezone.utc)
    # AFIP uses Argentina time (UTC-3)
    ar_offset = datetime.timedelta(hours=-3)
    ar_now = now + ar_offset

    generation = ar_now.strftime("%Y-%m-%dT%H:%M:%S-03:00")
    expiration = (ar_now + datetime.timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%S-03:00")
    unique_id = str(int(now.timestamp()))

    tra = f"""<?xml version="1.0" encoding="UTF-8"?>
<loginTicketRequest version="1.0">
    <header>
        <uniqueId>{unique_id}</uniqueId>
        <generationTime>{generation}</generationTime>
        <expirationTime>{expiration}</expirationTime>
    </header>
    <service>{service}</service>
</loginTicketRequest>"""

    return tra.encode("utf-8")


def sign_tra_cms(tra_xml: bytes, key, cert) -> str:
    """Sign TRA XML with CMS/PKCS#7 SignedData.

    Returns base64-encoded CMS signature.
    """
    options = [pkcs7.PKCS7Options.Binary]

    signed_data = (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(tra_xml)
        .add_signer(cert, key, hashes.SHA256())
        .sign(Encoding.DER, options)
    )

    return base64.b64encode(signed_data).decode("ascii")


def call_wsaa(cms_b64: str, ambiente: str) -> dict:
    """Call WSAA LoginCms SOAP endpoint.

    Returns dict with token, sign, and expiration.
    """
    import zeep
    from factura.afip_wsfe import _afip_session
    from zeep.transports import Transport

    wsdl_url = WSAA_URLS[ambiente]
    session = _afip_session()
    transport = Transport(session=session)
    client = zeep.Client(wsdl=wsdl_url, transport=transport)

    response = client.service.loginCms(cms_b64)

    # Parse XML response
    root = ElementTree.fromstring(response)
    token = root.find(".//token").text
    sign = root.find(".//sign").text
    expiration = root.find(".//expirationTime").text

    return {
        "token": token,
        "sign": sign,
        "expiration": expiration,
    }


def get_cached_token(service: str, ambiente: str) -> Optional[dict]:
    """Check if we have a valid cached token."""
    if not TOKEN_CACHE_PATH.exists():
        return None

    try:
        with open(TOKEN_CACHE_PATH, "r") as f:
            cache = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    cache_key = f"{service}_{ambiente}"
    entry = cache.get(cache_key)
    if not entry:
        return None

    # Check expiration with 5-minute margin
    exp_str = entry.get("expiration", "")
    try:
        # Parse AFIP timestamp (format: 2026-03-03T15:30:00.000-03:00)
        exp_dt = datetime.datetime.fromisoformat(exp_str)
        now = datetime.datetime.now(datetime.timezone.utc)
        margin = datetime.timedelta(minutes=5)

        if exp_dt.astimezone(datetime.timezone.utc) - margin > now:
            return entry
    except (ValueError, TypeError):
        pass

    return None


def save_token_cache(service: str, ambiente: str, token_data: dict) -> None:
    """Save token to cache file."""
    TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

    cache = {}
    if TOKEN_CACHE_PATH.exists():
        try:
            with open(TOKEN_CACHE_PATH, "r") as f:
                cache = json.load(f)
        except (json.JSONDecodeError, OSError):
            cache = {}

    cache_key = f"{service}_{ambiente}"
    cache[cache_key] = token_data

    with open(TOKEN_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def authenticate(
    key_path: Path,
    crt_path: Path,
    ambiente: str,
    service: str = "wsfe",
) -> dict:
    """Full WSAA authentication flow.

    1. Check cache
    2. Validate cert
    3. Build TRA
    4. Sign with CMS
    5. Call WSAA
    6. Cache result

    Returns dict with token, sign.
    """
    # Check cache first
    cached = get_cached_token(service, ambiente)
    if cached:
        print(f"  Using cached WSAA token (expires: {cached['expiration']})")
        return cached

    # Validate cert
    print(f"  Validating certificate...")
    cert_info = validate_certificate(key_path, crt_path)
    print(f"  Cert CN: {cert_info['cn']}")
    print(f"  Cert valid until: {cert_info['not_valid_after']}")

    # Build and sign TRA
    print(f"  Building TRA for service '{service}'...")
    tra_xml = build_tra_xml(service)
    cms_b64 = sign_tra_cms(tra_xml, cert_info["key"], cert_info["cert"])
    print(f"  TRA signed ({len(cms_b64)} chars)")

    # Call WSAA
    print(f"  Calling WSAA ({ambiente})...")
    token_data = call_wsaa(cms_b64, ambiente)
    print(f"  Token obtained (expires: {token_data['expiration']})")

    # Cache
    save_token_cache(service, ambiente, token_data)

    return token_data
