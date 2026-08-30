"""Microsoft Entra ID single sign-on (OIDC authorization-code flow).

Two routers:
  - `router` — the public login surface (/auth/microsoft, /auth/microsoft/
    callback). 404 while SSO is not enabled, so an unconfigured install is
    byte-identical to a pre-SSO build.
  - `admin_router` — the ladmin configuration API (/api/admin/sso*), guarded
    by routes.admin_data._require_admin like every other admin surface.

The whole feature is driven by DATA_ROOT/sso_config.json (sso_store.py):
saving new values / enabling / disabling takes effect on the next request —
no restart. Authlib does the OIDC heavy lifting (discovery, JWKS, ID-token
signature/issuer/audience/nonce validation, state in the cookie session);
this module never parses a JWT by hand and never logs secrets or tokens.

Import direction: this module imports routes.auth (for _landing /
_start_session) — routes.auth must never import this module (it reaches
sso_store function-locally instead).
"""
from __future__ import annotations

import hashlib
import re

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
import httpx

import brain_client
import db_sources
import sso_store
from local_store import AuthStore
from logger_utils import log_with_sid
from routes.admin_data import _json_body, _require_admin
from routes.auth import _landing, _start_session

router = APIRouter(tags=["client-sso"])
admin_router = APIRouter(prefix="/api/admin", tags=["client-admin-sso"])

_SIGNIN_FAILED = ("Microsoft sign-in failed. Please try again or contact "
                  "your administrator.")

_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$")

# Token-endpoint AADSTS codes that mean "credentials are RIGHT but tenant
# policy blocks a client-credentials token" — the browser sign-in flow may
# still work, so this outcome records test-ok instead of blocking Enable.
# Deliberately NOT included: AADSTS7000215 (bad secret), AADSTS700016
# (app not found in tenant — wrong Client/Tenant ID), AADSTS90002
# (tenant not found) — those are real configuration failures.
_POLICY_BLOCK_CODES = ("AADSTS500011", "AADSTS65001")
_POLICY_BLOCKED_MSG = ("Tenant ID, Client ID and secret were accepted, but "
                       "the tenant's policy blocked the test token. Sign-in "
                       "may still work — contact your Entra administrator or "
                       "skip the test.")


# ---------------------------------------------------------------------------
# OIDC client (fresh per request — Article VII rule 8: the client secret
# only ever exists in function-locals / the transient client object, never
# at importable module scope. What IS cached module-wide is exclusively the
# PUBLIC discovery metadata + JWKS, so repeat logins skip the network
# round-trips a fresh client would otherwise re-do.)
# ---------------------------------------------------------------------------

_METADATA_CACHE: dict = {"key": None, "metadata": None}   # public data ONLY


def _oauth_client():
    """A FRESH authlib client for the CURRENTLY saved config, or None when
    no usable config exists (no file, or the secret cannot be decrypted).
    Keyed on (tenant_id, client_id, secret-hash): a config change drops the
    cached metadata; an unchanged config reuses it (verified: metadata
    kwargs land in server_metadata and no discovery fetch happens when
    server_metadata_url is omitted)."""
    cfg = sso_store.load()
    if not cfg or not (cfg.get("tenant_id") and cfg.get("client_id")
                       and cfg.get("client_secret")):
        return None
    key = (cfg["tenant_id"], cfg["client_id"],
           hashlib.sha256(cfg["client_secret"].encode("utf-8")).hexdigest())
    extra = {}
    if _METADATA_CACHE["key"] == key and _METADATA_CACHE["metadata"]:
        extra.update(_METADATA_CACHE["metadata"])
    else:
        extra["server_metadata_url"] = (
            f"https://login.microsoftonline.com/{cfg['tenant_id']}/v2.0/"
            f".well-known/openid-configuration")
    client = OAuth().register(
        name="microsoft",
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        client_kwargs={"scope": "openid profile email"},
        **extra,
    )
    client._pdc_cache_key = key
    return client


def _remember_metadata(client) -> None:
    """Stash the client's PUBLIC server metadata (incl. the JWKS authlib
    fetched during ID-token validation) for the next login. Never the
    client object, never the secret. Best-effort (Article IV)."""
    try:
        md = dict(client.server_metadata or {})
        if md.get("token_endpoint"):
            _METADATA_CACHE["key"] = client._pdc_cache_key
            _METADATA_CACHE["metadata"] = md
    except Exception as e:
        log_with_sid("sso", "warning", f"SSO_METADATA_CACHE_FAILED: {e}")


# ---------------------------------------------------------------------------
# Public login routes
# ---------------------------------------------------------------------------

@router.get("/auth/microsoft")
async def microsoft_login(request: Request):
    """Start the authorization-code flow. 404 while SSO is disabled."""
    if not sso_store.is_enabled():
        raise HTTPException(status_code=404)
    client = _oauth_client()
    if client is None:
        # Enabled but the secret is unreadable (encryption key rotated away)
        log_with_sid("sso", "warning", "SSO_SECRET_UNREADABLE")
        return _landing(request, error=_SIGNIN_FAILED, status_code=503)
    try:
        # First call per config also fetches the discovery document — a bad
        # tenant / unreachable Microsoft must degrade, not 500 (Article IV).
        resp = await client.authorize_redirect(request, sso_store.redirect_uri(request))
    except Exception as e:
        log_with_sid("sso", "warning", f"SSO_START_FAILED: {type(e).__name__}: {e}")
        return _landing(request, error=_SIGNIN_FAILED, status_code=503)
    _remember_metadata(client)
    return resp


@router.get("/auth/microsoft/callback")
async def microsoft_callback(request: Request):
    """Exchange the code, validate the ID token (authlib: signature, issuer,
    audience, nonce), start the local session, land on /lab."""
    if not sso_store.is_enabled():
        raise HTTPException(status_code=404)
    client = _oauth_client()
    if client is None:
        log_with_sid("sso", "warning", "SSO_SECRET_UNREADABLE")
        return _landing(request, error=_SIGNIN_FAILED, status_code=503)
    try:
        token = await client.authorize_access_token(request)
    except OAuthError as e:
        # str(e) carries only Microsoft's error/error_description — never
        # token material. The token dict itself is NEVER logged.
        log_with_sid("sso", "warning", f"SSO_CALLBACK_FAILED: OAuthError: {e}")
        return _landing(request, error=_SIGNIN_FAILED, status_code=401)
    except Exception as e:
        log_with_sid("sso", "warning",
                     f"SSO_CALLBACK_FAILED: {type(e).__name__}: {e}")
        return _landing(request, error=_SIGNIN_FAILED, status_code=401)

    _remember_metadata(client)
    claims = token.get("userinfo") or {}
    email = str(claims.get("preferred_username") or claims.get("email") or "")
    email = email.strip().lower()
    if not email or "@" not in email:
        log_with_sid("sso", "warning", "SSO_CALLBACK_NO_EMAIL")
        return _landing(request, error=_SIGNIN_FAILED, status_code=400)
    if AuthStore().is_bootstrap_admin(email):
        # The appliance account signs in with its password only — even on an
        # install whose LOCAL_ADMIN_USERNAME was set to a real Entra email,
        # SSO must never open the bootstrap-admin session.
        log_with_sid(email, "warning", "SSO_BOOTSTRAP_ADMIN_REFUSED")
        return _landing(request, error=_SIGNIN_FAILED, status_code=403)

    store = AuthStore()
    store.ensure_user(email)
    store.mark_sso_login(email, "microsoft")
    # Browser-session cookie on purpose (remember=False): Entra re-auth is
    # silent on joined devices, so a 30-day persistent cookie would add risk
    # with no UX benefit. must_change is never set for an SSO login.
    _start_session(request, email, remember=False)
    log_with_sid(email, "info", "USER_LOGIN_SSO", sid=request.session.get("sid"))
    try:
        brain_client.post_activity("login", email)
    except Exception:
        pass
    return RedirectResponse(url="/lab", status_code=302)


# ---------------------------------------------------------------------------
# Admin configuration API
# ---------------------------------------------------------------------------

def _admin_view(request: Request) -> dict:
    """The masked config enriched with everything the panel needs."""
    out = sso_store.load_masked()
    out["redirect_uri"] = sso_store.redirect_uri(request)
    out["test_current"] = sso_store.test_is_current()
    out["encryption_ready"] = db_sources.encryption_ready()
    return out


@admin_router.get("/sso")
async def get_sso(request: Request):
    email, err = _require_admin(request)
    if err:
        return err
    return _admin_view(request)


@admin_router.post("/sso/save")
async def save_sso(request: Request):
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
    tenant_id = str(body.get("tenant_id") or "").strip()
    client_id = str(body.get("client_id") or "").strip()
    client_secret = str(body.get("client_secret") or "")
    public_base_url = str(body.get("public_base_url") or "").strip()

    if not (_GUID_RE.match(tenant_id) or _DOMAIN_RE.match(tenant_id)):
        return JSONResponse({"error": "Tenant ID must be a GUID or a domain "
                                      "like contoso.onmicrosoft.com."},
                            status_code=400)
    if not client_id:
        return JSONResponse({"error": "Client ID is required."}, status_code=400)
    if not client_secret and not sso_store.load_masked().get("client_secret_set"):
        return JSONResponse({"error": "Client secret is required."}, status_code=400)
    if public_base_url and not (public_base_url.startswith("http://")
                                or public_base_url.startswith("https://")):
        return JSONResponse({"error": "Public base URL must start with "
                                      "http:// or https://."}, status_code=400)
    try:
        sso_store.save({
            "tenant_id": tenant_id,
            "client_id": client_id,
            "client_secret": client_secret,      # empty ⇒ keep stored
            "public_base_url": public_base_url,
            "auto_redirect": bool(body.get("auto_redirect")),
        }, updated_by=email)
    except db_sources.EncryptionUnavailable as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    db_sources.audit(email, "sso.save", target="microsoft",
                     detail={"tenant_id": tenant_id, "client_id": client_id,
                             "auto_redirect": bool(body.get("auto_redirect")),
                             "public_base_url": public_base_url,
                             "secret_changed": bool(client_secret)},
                     ip=(request.client.host if request.client else None))
    log_with_sid(email, "info", "SSO_CONFIG_SAVED")
    return _admin_view(request)


def _http_client() -> httpx.AsyncClient:
    """Seam for tests (MockTransport). 10s cap per the spec."""
    return httpx.AsyncClient(timeout=10.0)


def _ms_error(resp: httpx.Response) -> str:
    """Microsoft's error text from a failed discovery/token response —
    error_description or error, truncated; never token material."""
    try:
        data = resp.json()
        msg = str(data.get("error_description") or data.get("error") or "")
        if msg:
            return msg[:300]
    except Exception:
        pass
    return f"Microsoft returned HTTP {resp.status_code}."


@admin_router.post("/sso/test")
async def test_sso(request: Request):
    """Prove tenant_id + client_id + secret against Microsoft without a
    browser round-trip: fetch the OpenID discovery document, then request a
    client-credentials token (scope graph/.default). Outcomes are 200
    {ok, message} — the admin_data connectivity-failure convention."""
    email, err = _require_admin(request)
    if err:
        return err
    if not db_sources.encryption_ready():
        return JSONResponse({"error": "CLIENT_ENCRYPTION_KEY is not configured. "
                                      "Set it in the environment and restart "
                                      "the container."}, status_code=503)
    cfg = sso_store.load()
    if not cfg or not (cfg.get("tenant_id") and cfg.get("client_id")):
        return JSONResponse({"error": "Save the configuration first."},
                            status_code=400)

    def _outcome(ok: bool, message: str, code: str = None,
                 audit_ok: bool = None) -> JSONResponse:
        """`ok` is the API outcome; `audit_ok` overrides the audit row's ok
        flag (POLICY_BLOCKED: response ok=false, audit ok=true)."""
        detail = {"tenant_id": cfg["tenant_id"], "client_id": cfg["client_id"]}
        if not ok or code:
            detail["message"] = message
        if code:
            detail["code"] = code
        db_sources.audit(email, "sso.test", target="microsoft",
                         ok=(ok if audit_ok is None else audit_ok),
                         detail=detail,
                         ip=(request.client.host if request.client else None))
        body = {"ok": ok, "message": message}
        if code:
            body["code"] = code
        return JSONResponse(body)

    if not cfg.get("client_secret"):
        return _outcome(False, "Stored client secret cannot be read — "
                               "re-enter it.")
    # Hash of the values ACTUALLY tested — a save racing the network window
    # below can never get its untested values stamped as tested.
    tested_hash = sso_store.hash_for(cfg["tenant_id"], cfg["client_id"],
                                     cfg["client_secret"])
    discovery_url = (f"https://login.microsoftonline.com/{cfg['tenant_id']}"
                     f"/v2.0/.well-known/openid-configuration")
    try:
        async with _http_client() as hc:
            disco = await hc.get(discovery_url)
            if disco.status_code != 200:
                return _outcome(False, _ms_error(disco))
            token_endpoint = (disco.json() or {}).get("token_endpoint")
            if not token_endpoint:
                return _outcome(False, "Discovery document has no "
                                       "token_endpoint — check the Tenant ID.")
            resp = await hc.post(token_endpoint, data={
                "grant_type": "client_credentials",
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "scope": "https://graph.microsoft.com/.default",
            })
    except Exception as e:
        log_with_sid(email, "warning", f"SSO_TEST_UNREACHABLE: {type(e).__name__}")
        return _outcome(False, "Could not reach login.microsoftonline.com: "
                               f"{type(e).__name__}")

    if resp.status_code == 200 and (resp.json() or {}).get("access_token"):
        sso_store.record_test_ok(email, tested_hash=tested_hash)
        log_with_sid(email, "info", "SSO_TEST_OK")
        return _outcome(True, "Connection test passed — Tenant ID, Client ID "
                              "and secret are valid.")
    message = _ms_error(resp)
    if any(c in message for c in _POLICY_BLOCK_CODES):
        # Credentials accepted; only the client-credentials grant is blocked
        # by tenant policy — do not hold Enable hostage to it.
        sso_store.record_test_ok(email, tested_hash=tested_hash)
        log_with_sid(email, "info", "SSO_TEST_POLICY_BLOCKED")
        return _outcome(False, _POLICY_BLOCKED_MSG, code="POLICY_BLOCKED",
                        audit_ok=True)
    log_with_sid(email, "warning", "SSO_TEST_FAILED")
    return _outcome(False, message)


@admin_router.post("/sso/enable")
async def enable_sso(request: Request):
    email, err = _require_admin(request)
    if err:
        return err
    masked = sso_store.load_masked()
    if not (masked.get("tenant_id") and masked.get("client_id")
            and masked.get("client_secret_set")):
        return JSONResponse({"error": "Save the configuration first."},
                            status_code=400)
    if not sso_store.test_is_current():
        db_sources.audit(email, "sso.enable", target="microsoft", ok=False,
                         detail={"tenant_id": masked.get("tenant_id"),
                                 "client_id": masked.get("client_id"),
                                 "code": "TEST_REQUIRED"},
                         ip=(request.client.host if request.client else None))
        return JSONResponse({"error": "Run a successful connection test for "
                                      "the saved values first.",
                             "code": "TEST_REQUIRED"}, status_code=409)
    sso_store.set_enabled(True, email)
    db_sources.audit(email, "sso.enable", target="microsoft",
                     detail={"tenant_id": masked.get("tenant_id"),
                             "client_id": masked.get("client_id")},
                     ip=(request.client.host if request.client else None))
    log_with_sid(email, "info", "SSO_ENABLED")
    return _admin_view(request)


@admin_router.post("/sso/disable")
async def disable_sso(request: Request):
    email, err = _require_admin(request)
    if err:
        return err
    masked = sso_store.load_masked()
    sso_store.set_enabled(False, email)
    db_sources.audit(email, "sso.disable", target="microsoft",
                     detail={"tenant_id": masked.get("tenant_id"),
                             "client_id": masked.get("client_id")},
                     ip=(request.client.host if request.client else None))
    log_with_sid(email, "info", "SSO_DISABLED")
    return _admin_view(request)
