"""
Spike: Test CMS/PKCS#7 signing with cryptography library.
This validates that we can sign the TRA XML for WSAA authentication.
"""
import datetime
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7, Encoding
import base64


def generate_test_cert_and_key():
    """Generate a self-signed cert+key pair for testing CMS signing."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Test AFIP Signing"),
        x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )

    return key, cert


def sign_tra_cms(tra_xml: bytes, key, cert) -> str:
    """
    Sign a TRA XML using CMS/PKCS#7 SignedData.
    This is the exact process needed for WSAA LoginCms.

    Returns base64-encoded CMS signature.
    """
    # Build PKCS7 signed data (what AFIP calls "CMS")
    options = [pkcs7.PKCS7Options.Binary]

    signed_data = (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(tra_xml)
        .add_signer(cert, key, hashes.SHA256())
        .sign(Encoding.DER, options)
    )

    return base64.b64encode(signed_data).decode("ascii")


def build_tra_xml(service: str = "wsfe") -> bytes:
    """Build a sample TRA (Ticket de Requerimiento de Acceso) XML."""
    now = datetime.datetime.now(datetime.timezone.utc)
    generation = now.strftime("%Y-%m-%dT%H:%M:%S-03:00")
    expiration = (now + datetime.timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%S-03:00")

    tra = f"""<?xml version="1.0" encoding="UTF-8"?>
<loginTicketRequest version="1.0">
    <header>
        <uniqueId>{int(now.timestamp())}</uniqueId>
        <generationTime>{generation}</generationTime>
        <expirationTime>{expiration}</expirationTime>
    </header>
    <service>{service}</service>
</loginTicketRequest>"""

    return tra.encode("utf-8")


def main():
    print("=== CMS Signing Spike ===\n")

    # Step 1: Generate test cert/key
    print("1. Generating test RSA key + self-signed cert...")
    key, cert = generate_test_cert_and_key()
    print(f"   Key: RSA {key.key_size}-bit")
    print(f"   Cert CN: {cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value}")
    print(f"   Cert valid until: {cert.not_valid_after_utc}")

    # Step 2: Build TRA XML
    print("\n2. Building TRA XML...")
    tra_xml = build_tra_xml("wsfe")
    print(f"   TRA size: {len(tra_xml)} bytes")
    print(f"   TRA preview: {tra_xml[:100].decode()}...")

    # Step 3: Sign with CMS/PKCS7
    print("\n3. Signing TRA with PKCS7SignatureBuilder...")
    cms_b64 = sign_tra_cms(tra_xml, key, cert)
    print(f"   CMS signature length: {len(cms_b64)} chars (base64)")
    print(f"   CMS preview: {cms_b64[:80]}...")

    # Step 4: Verify the signature is valid DER
    print("\n4. Verifying DER structure...")
    cms_der = base64.b64decode(cms_b64)
    # Check ASN.1 SEQUENCE tag (0x30) at start
    assert cms_der[0] == 0x30, f"Expected ASN.1 SEQUENCE, got {hex(cms_der[0])}"
    print(f"   DER size: {len(cms_der)} bytes")
    print(f"   ASN.1 tag: SEQUENCE (0x30) - correct")

    # Step 5: Test with real cert file loading (simulate)
    print("\n5. Testing cert/key PEM serialization round-trip...")
    key_pem = key.private_bytes(
        Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(Encoding.PEM)

    # Reload from PEM (as we would from .key/.crt files)
    key_reloaded = serialization.load_pem_private_key(key_pem, password=None)
    cert_reloaded = x509.load_pem_x509_certificate(cert_pem)

    # Re-sign with reloaded cert/key
    cms_b64_2 = sign_tra_cms(tra_xml, key_reloaded, cert_reloaded)
    print(f"   Re-signed CMS length: {len(cms_b64_2)} chars")
    print(f"   Round-trip: OK")

    print("\n=== SPIKE PASSED === CMS signing works on Python 3.13 + Windows")
    print("Ready to proceed with Fase 1.")


if __name__ == "__main__":
    main()
