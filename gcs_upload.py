"""Optional direct-to-GCS upload transport (Cloud Run demo only).

Inert unless ``settings.GCS_UPLOAD_BUCKET`` is set: every customer install
leaves it empty and never imports a Google library — the imports below are
function-local on purpose so the image works with no GCS environment.

Port of the B2C flow: the browser asks ``/upload/init`` for a V4 signed PUT URL
(signed with the Cloud Run runtime service account through IAM ``signBlob`` —
no key file anywhere, Article VII), PUTs the bytes straight to the bucket, and
``/upload/finalize`` pulls the object into the per-session store and deletes
it. The bucket is a transient transport, never storage: raw data still lives
under DATA_ROOT only (Article V).

Every function here is BLOCKING; the routes run them on the shared executor.
Denied inside the code-exec sandbox (sandbox_guard) like every other
credential-adjacent module.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Optional

from settings import settings

_client = None


def enabled() -> bool:
    """True when a bucket is configured — read at call time (tests patch settings)."""
    return bool((settings.GCS_UPLOAD_BUCKET or "").strip())


def bucket_name() -> str:
    return (settings.GCS_UPLOAD_BUCKET or "").strip()


def _get_client():
    """Memoized storage client (ADC). Lazy import: never at module level."""
    global _client
    if _client is None:
        from google.cloud import storage  # optional dependency, imported lazily
        _client = storage.Client()
    return _client


def _bucket():
    return _get_client().bucket(bucket_name())


def sign_put_url(object_path: str, content_type: str, expiration_minutes: int = 15) -> str:
    """V4 signed PUT URL for ``object_path``, bound to ``content_type``.

    Mirrors the B2C signer: application-default credentials refreshed for an
    access token, then the library's IAM-``signBlob`` path via
    ``service_account_email`` + ``access_token`` — the runtime service account
    needs ``roles/iam.serviceAccountTokenCreator`` on itself. No private key
    is ever read. Raises on any failure; the route turns that into a fixed
    500 (google exceptions embed request URLs — never echoed to the client).
    """
    import google.auth
    from google.auth.transport.requests import Request as AuthRequest

    credentials, _project = google.auth.default()
    credentials.refresh(AuthRequest())
    sa_email = getattr(credentials, "service_account_email", None)
    if not sa_email or sa_email == "default":
        raise RuntimeError("Cannot determine the service account email for signing")
    blob = _bucket().blob(object_path)
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=expiration_minutes),
        method="PUT",
        content_type=content_type,
        service_account_email=sa_email,
        access_token=credentials.token,
    )


def blob_size(object_path: str) -> Optional[int]:
    """Size of the object in bytes, or None when it does not exist.

    ONE metadata round-trip that doubles as the existence check and the
    size guard: a signed PUT does not bind Content-Length, so finalize must
    verify the real size before downloading.
    """
    blob = _bucket().get_blob(object_path)
    if blob is None:
        return None
    return int(blob.size or 0)


def download_to(object_path: str, dest: Path) -> int:
    """Stream the object to ``dest`` (never buffered in RAM); returns its size."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _bucket().blob(object_path).download_to_filename(str(dest))
    return dest.stat().st_size


def delete_blob(object_path: str) -> None:
    _bucket().blob(object_path).delete()
