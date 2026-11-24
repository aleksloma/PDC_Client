"""Throw-away certificates for the built-in HTTPS tests.

Everything is generated at run time under the test's tmp_path; no certificate
or key is ever committed.
"""
import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

PASSPHRASE = b"correct-horse-battery"


def new_key(kind: str = "ec"):
    if kind == "rsa":
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return ec.generate_private_key(ec.SECP256R1())


def cert_pem(key) -> bytes:
    now = datetime.datetime.now(datetime.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM)


def key_pem(key, *, passphrase: bytes | None = None, traditional: bool = False) -> bytes:
    fmt = (serialization.PrivateFormat.TraditionalOpenSSL if traditional
           else serialization.PrivateFormat.PKCS8)
    enc = (serialization.BestAvailableEncryption(passphrase) if passphrase
           else serialization.NoEncryption())
    return key.private_bytes(serialization.Encoding.PEM, fmt, enc)


def write_pair(folder, *, kind: str = "ec", name: str = "server",
               passphrase: bytes | None = None, traditional: bool = False):
    """(certificate path, key path) of a matching pair written into `folder`."""
    key = new_key(kind)
    cert_path = folder / f"{name}.crt"
    key_path = folder / f"{name}.key"
    cert_path.write_bytes(cert_pem(key))
    key_path.write_bytes(key_pem(key, passphrase=passphrase, traditional=traditional))
    return cert_path, key_path


def assert_no_key_material(text: str, *pems: bytes) -> None:
    """No piece (16+ characters) of any PEM body is in `text`. Fails without
    printing the material itself."""
    if "PRIVATE KEY" in text:
        raise AssertionError("a PEM private-key marker was logged")
    if PASSPHRASE.decode() in text:
        raise AssertionError("the passphrase was logged")
    for pem in pems:
        for line in pem.decode("ascii").splitlines():
            if line.startswith("-----") or ":" in line:
                continue
            for i in range(0, max(1, len(line) - 15), 4):
                piece = line[i:i + 16]
                if len(piece) == 16 and piece in text:
                    raise AssertionError("key material was logged")
