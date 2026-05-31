"""
Cookie manager for KB NetApp MCP Server.
Primary: Windows Credential Manager via keyring.
Fallback: DPAPI-encrypted JSON file in ~/.copilot/ (used when Credential Manager
          rejects large blobs on Azure-AD-joined devices with restrictive group policies).

PORTABLE: Username is configurable via KB_USERNAME environment variable.
"""

import os
import json
import getpass
import ctypes
import ctypes.wintypes
from datetime import datetime, timezone
from typing import Optional

import keyring
import keyring.errors

SERVICE_NAME = "KBNetAppMCP"

# Fallback file path for DPAPI-encrypted cookies
_FALLBACK_DIR = os.path.join(os.path.expanduser("~"), ".copilot")
_FALLBACK_FILE = os.path.join(_FALLBACK_DIR, ".kb_cookies.dpapi")


# ---------------------------------------------------------------------------
# DPAPI helpers (Windows only)
# ---------------------------------------------------------------------------

def _dpapi_encrypt(data: bytes) -> bytes:
    """Encrypt bytes with DPAPI (current user scope)."""
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data)
    blob_in = DATA_BLOB(len(data), buf)
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise RuntimeError("DPAPI encrypt failed")
    enc = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return enc


def _dpapi_decrypt(data: bytes) -> bytes:
    """Decrypt DPAPI-encrypted bytes."""
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data)
    blob_in = DATA_BLOB(len(data), buf)
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise RuntimeError("DPAPI decrypt failed")
    dec = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return dec


def _fallback_save(username: str, payload: str) -> None:
    os.makedirs(_FALLBACK_DIR, exist_ok=True)
    enc = _dpapi_encrypt(payload.encode("utf-8"))
    with open(_FALLBACK_FILE, "wb") as f:
        f.write(enc)


def _fallback_load(username: str) -> Optional[str]:
    if not os.path.exists(_FALLBACK_FILE):
        return None
    try:
        with open(_FALLBACK_FILE, "rb") as f:
            enc = f.read()
        return _dpapi_decrypt(enc).decode("utf-8")
    except Exception:
        return None


def _fallback_delete(username: str) -> bool:
    if os.path.exists(_FALLBACK_FILE):
        os.remove(_FALLBACK_FILE)
        return True
    return False


# Gated article used to probe session liveness (same URL as login_helper)
_GATED_PROBE_URL = (
    "https://kb.netapp.com/on-prem/ontap/Perf/Perf-KBs/"
    "High_CIFS_latency_on_FlexGroup_constituents_from_heavy_RENAME_workload"
)


def probe_session_valid(username: str = None) -> bool:
    """
    Live HTTP probe: fetch a known gated KB article using the stored cookies.
    Returns True if the content is accessible (server-side session is still alive).

    This is used as a secondary check when the 8-hour session-cookie fallback
    in is_cookies_expired() fires — NetApp SSO sessions routinely outlive 8 h,
    so the timestamp-only check is overly conservative.
    """
    try:
        import requests as _req
        data = get_stored_cookies(username)
        cookies_list = data.get("cookies", [])
        if not cookies_list:
            return False
        jar = _req.cookies.RequestsCookieJar()
        for c in cookies_list:
            jar.set(
                c["name"], c["value"],
                domain=c.get("domain", ".kb.netapp.com"),
                path=c.get("path", "/"),
            )
        s = _req.Session()
        s.cookies = jar
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Referer": "https://kb.netapp.com/",
        })
        resp = s.get(_GATED_PROBE_URL, timeout=10, allow_redirects=True)
        return "sign in to view the entire content" not in resp.text.lower()
    except Exception:
        return False


def get_username() -> str:
    """
    Get username from environment variable or system username.

    Priority:
    1. KB_USERNAME environment variable
    2. Current system username
    """
    env_user = os.environ.get("KB_USERNAME")
    if env_user:
        return env_user
    try:
        return os.getlogin()
    except OSError:
        return getpass.getuser()


def get_stored_cookies(username: str = None) -> dict:
    """
    Retrieve stored cookie data.
    Tries Windows Credential Manager first, then DPAPI file fallback.
    """
    if username is None:
        username = get_username()

    raw = keyring.get_password(SERVICE_NAME, username)
    if raw is None:
        raw = _fallback_load(username)
    if raw is None:
        raise RuntimeError(
            f"No cookies found for user '{username}'.\n"
            "Please run Set-KBCookies.ps1 first to log in to kb.netapp.com."
        )
    return json.loads(raw)


def set_stored_cookies(cookies: list, username: str = None) -> None:
    """
    Store session cookies. Tries Windows Credential Manager first;
    falls back to DPAPI-encrypted file if the blob is rejected (e.g. policy limits).
    """
    if username is None:
        username = get_username()

    cookie_data = {
        "cookies": cookies,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "username": username,
    }
    payload = json.dumps(cookie_data)

    try:
        keyring.set_password(SERVICE_NAME, username, payload)
    except Exception:
        # Credential Manager rejected the write (common on AAD-joined devices
        # with group policies that limit blob size). Use DPAPI file fallback.
        _fallback_save(username, payload)


def has_stored_cookies(username: str = None) -> bool:
    """Check if cookies are stored for this user."""
    if username is None:
        username = get_username()
    return (
        keyring.get_password(SERVICE_NAME, username) is not None
        or _fallback_load(username) is not None
    )


def is_cookies_expired(username: str = None) -> bool:
    """
    Check whether the stored cookies have expired.

    Uses the earliest 'expires' field from the actual cookies if available,
    otherwise falls back to checking if the save timestamp is older than 8 hours.
    """
    if username is None:
        username = get_username()

    try:
        data = get_stored_cookies(username)
        cookies = data.get("cookies", [])
        now_ts = datetime.now(timezone.utc).timestamp()

        # Collect expiry timestamps from cookies that have them
        persistent_exps = [
            c["expires"] for c in cookies
            if c.get("expires", -1) and c.get("expires", -1) > 0
        ]

        if persistent_exps:
            # Session is alive as long as the LATEST persistent cookie hasn't expired.
            # Using max() avoids false positives from short-lived analytics/tracking cookies
            # expiring before the actual auth session cookie.
            return now_ts > max(persistent_exps)

        # All cookies are session-only (no expires field) — fall back to 8h from save time
        saved_at = datetime.fromisoformat(data.get("saved_at", "2000-01-01T00:00:00+00:00"))
        age_hours = (datetime.now(timezone.utc) - saved_at).total_seconds() / 3600
        if age_hours <= 8:
            return False

        # Beyond the 8 h window — do a live HTTP probe before declaring expired.
        # NetApp SSO sessions commonly survive well beyond 8 h; the timestamp
        # fallback is intentionally conservative so we verify before forcing
        # the user through a browser login.
        if probe_session_valid(username):
            # Session is still alive: refresh saved_at so the next call within
            # ~8 h won't probe the network unnecessarily.
            try:
                set_stored_cookies(data.get("cookies", []), username)
            except Exception:
                pass
            return False

        return True

    except Exception:
        return True


def delete_stored_cookies(username: str = None) -> bool:
    """
    Delete stored cookies (both Credential Manager and file fallback).
    """
    if username is None:
        username = get_username()
    deleted = False
    try:
        keyring.delete_password(SERVICE_NAME, username)
        deleted = True
    except keyring.errors.PasswordDeleteError:
        pass
    if _fallback_delete(username):
        deleted = True
    return deleted


def get_cookie_status(username: str = None) -> dict:
    """
    Return a human-readable summary of the current cookie status.
    """
    if username is None:
        username = get_username()

    if not has_stored_cookies(username):
        return {
            "authenticated": False,
            "username": username,
            "message": "No cookies stored. Run Set-KBCookies.ps1 to log in.",
        }

    try:
        data = get_stored_cookies(username)
        saved_at = datetime.fromisoformat(data.get("saved_at", "2000-01-01T00:00:00+00:00"))
        expired = is_cookies_expired(username)
        cookies = data.get("cookies", [])

        # Find earliest actual expiry
        min_exp: Optional[float] = None
        for c in cookies:
            exp = c.get("expires", -1)
            if exp and exp > 0:
                if min_exp is None or exp < min_exp:
                    min_exp = exp

        expires_str = (
            datetime.fromtimestamp(min_exp).strftime("%Y-%m-%d %H:%M:%S UTC")
            if min_exp
            else "Session cookie (8h from login)"
        )

        return {
            "authenticated": not expired,
            "username": data.get("username", username),
            "saved_at": saved_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "expires": expires_str,
            "cookie_count": len(cookies),
            "expired": expired,
            "message": "Cookies expired. Run Set-KBCookies.ps1 to re-login." if expired else "Cookies valid.",
        }
    except Exception as e:
        return {"authenticated": False, "username": username, "message": str(e)}
