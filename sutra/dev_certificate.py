"""Create a short-lived self-signed certificate for localhost development."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path


def create_certificate(output_dir: str | Path) -> tuple[Path, Path]:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:
        raise RuntimeError("Install development dependencies: python -m pip install -e '.[dev]'") from exc
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    key_path, cert_path = destination / "localhost-key.pem", destination / "localhost-cert.pem"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, hashes.SHA256()))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def main() -> None:
    parser = argparse.ArgumentParser(prog="sutra dev-certificate")
    parser.add_argument("--output-dir", default=".dev-certs")
    args = parser.parse_args()
    cert, key = create_certificate(args.output_dir)
    print(f"Created localhost development certificate: {cert}")
    print(f"Created private key: {key}")
    print("Keep the key local and uncommitted; the certificate is only for localhost development.")


if __name__ == "__main__":
    main()
