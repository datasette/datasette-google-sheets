"""RSA keys for the mock, generated once per test process.

No private keys are committed: service-account key files are built in memory
the first time they are asked for (the ``service_account_keys`` session
fixture does that at session start).

Private key material is kept out of ``repr()`` so a failing assertion never
prints it.
"""

from __future__ import annotations

import functools
import secrets
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

PROJECT_ID = "mock-project"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Short name -> client_email local part. Every account except `unregistered`
# is known to the mock's token endpoint.
SA_TEST = f"sa-test@{PROJECT_ID}.iam.gserviceaccount.com"
SA_OTHER = f"sa-other@{PROJECT_ID}.iam.gserviceaccount.com"
SA_UNREGISTERED = f"sa-unregistered@{PROJECT_ID}.iam.gserviceaccount.com"
SA_TOKEN_500 = f"sa-token-500@{PROJECT_ID}.iam.gserviceaccount.com"

SERVICE_ACCOUNTS = {
    # Editor on the fixture spreadsheets.
    "test": SA_TEST,
    # Valid key, no access to any fixture spreadsheet ("share it with me").
    "other": SA_OTHER,
    # Valid-looking key the token endpoint has never heard of (deleted key).
    "unregistered": SA_UNREGISTERED,
    # The token endpoint answers its first exchange with a 500.
    "token_500": SA_TOKEN_500,
}

ID_TOKEN_KID = "mock-id-token-key"


def _generate() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@dataclass(frozen=True)
class ServiceAccountKey:
    client_email: str
    client_id: str
    private_key_id: str
    private_key: rsa.RSAPrivateKey = field(repr=False)
    project_id: str = PROJECT_ID

    @property
    def public_key(self) -> rsa.RSAPublicKey:
        return self.private_key.public_key()

    @property
    def private_key_pem(self) -> str:
        return self.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

    def key_json(self, **overrides: object) -> dict[str, object]:
        """The key file as the Cloud console downloads it; ``overrides``
        replace or add fields (e.g. ``token_uri="https://evil.example/"``)."""
        data: dict[str, object] = {
            "type": "service_account",
            "project_id": self.project_id,
            "private_key_id": self.private_key_id,
            "private_key": self.private_key_pem,
            "client_email": self.client_email,
            "client_id": self.client_id,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": GOOGLE_TOKEN_URI,
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": (
                "https://www.googleapis.com/robot/v1/metadata/x509/"
                + self.client_email.replace("@", "%40")
            ),
            "universe_domain": "googleapis.com",
        }
        data.update(overrides)
        return data


def make_service_account(client_email: str) -> ServiceAccountKey:
    return ServiceAccountKey(
        client_email=client_email,
        client_id=str(100000000000000000000 + secrets.randbelow(10**18)),
        private_key_id=secrets.token_hex(20),
        private_key=_generate(),
    )


@functools.cache
def fixture_service_accounts() -> dict[str, ServiceAccountKey]:
    """The session's service accounts, keyed by short name (``SERVICE_ACCOUNTS``)."""
    return {
        name: make_service_account(email) for name, email in SERVICE_ACCOUNTS.items()
    }


@functools.cache
def id_token_key() -> rsa.RSAPrivateKey:
    """Signs the mock's OpenID ``id_token``s (RS256, kid ``ID_TOKEN_KID``)."""
    return _generate()
