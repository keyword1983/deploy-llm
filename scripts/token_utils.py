#!/usr/bin/env python3
"""
token_utils.py — Shared token refresh + auto-retry module.

Import in any script that makes afsbox API calls:
    from token_utils import auto_request, api_request

Usage (one-liner, auto-retries on 401):
    data = api_request("GET", url, token)

Usage (low-level, for scripts that build their own Request):
    data = auto_request(req, token)

Usage (standalone, to get a fresh token):
    python3 token_utils.py
    → prints new access_token to stdout

Usage (check if token is still valid):
    python3 token_utils.py --check <token> <api_base_url>
    → prints "EXPIRED" or "VALID: remaining_seconds"
"""
import sys
import os
import json
import time
import base64
import urllib.request
import urllib.error
import ssl
import subprocess
import socket
import re

ssl._create_default_https_context = ssl._create_unverified_context

# ── Token refresh via K8s secrets + Keycloak ──────────────────────────

def _get_keycloak_ip():
    """Resolve Keycloak service IP via kubectl or in-cluster DNS."""
    # Try kubectl
    try:
        proc = subprocess.run(
            ["kubectl", "get", "svc", "keycloak-keycloakx-http",
             "-n", "keycloak", "-o", "jsonpath={.spec.clusterIP}"],
            capture_output=True, text=True, timeout=5
        )
        if proc.returncode == 0:
            ip = proc.stdout.strip()
            if ip:
                return ip
    except Exception:
        pass
    # Fallback: DNS
    try:
        return socket.gethostbyname(
            "keycloak-keycloakx-http.keycloak.svc.cluster.local"
        )
    except socket.gaierror:
        pass
    return None


def _get_k8s_secret_data():
    """Read the K8s afsbox-platform-secret."""
    try:
        proc = subprocess.run(
            ["kubectl", "get", "secret", "afsbox-platform-secret",
             "-n", "afsbox-system", "-o", "json"],
            capture_output=True, text=True, timeout=5
        )
        if proc.returncode == 0:
            return json.loads(proc.stdout)
    except Exception:
        pass
    return None


def _get_client_creds(secret_data: dict) -> tuple:
    """Extract Keycloak client_id and client_secret from K8s secret data."""
    if not secret_data:
        return None, None
    try:
        client_id = base64.b64decode(
            secret_data.get("data", {}).get("IAM_KEYCLOAK_CLIENT_ID", "")
        ).decode("utf-8").strip()
        client_secret = base64.b64decode(
            secret_data.get("data", {}).get("IAM_KEYCLOAK_CLIENT_SECRET", "")
        ).decode("utf-8").strip()
        return client_id, client_secret
    except Exception:
        return None, None


def _get_admin_creds(secret_data: dict = None) -> tuple:
    """Get Keycloak admin username and password from env vars, K8s secret, or default fallback."""
    # 1. Try env vars
    admin_user = os.environ.get("KEYCLOAK_ADMIN_USER", "")
    admin_pass = os.environ.get("KEYCLOAK_ADMIN_PASS", "")
    if admin_user and admin_pass:
        return admin_user, admin_pass

    # 2. Try K8s secret keys
    if secret_data:
        try:
            b64_user = secret_data.get("data", {}).get("ADMIN_USERNAME", "")
            b64_pass = secret_data.get("data", {}).get("ADMIN_PASSWORD", "")
            if b64_user:
                admin_user = base64.b64decode(b64_user).decode("utf-8").strip()
            if b64_pass:
                admin_pass = base64.b64decode(b64_pass).decode("utf-8").strip()
            if admin_user and admin_pass:
                return admin_user, admin_pass
        except Exception:
            pass

    # 3. Default fallback
    return "admin@asus.com", "admin"


def refresh_token() -> str:
    """Get a fresh access token from Keycloak via K8s credentials.

    Returns: new access_token string
    Raises: RuntimeError if refresh fails
    """
    secret_data = _get_k8s_secret_data()
    client_id, client_secret = _get_client_creds(secret_data)
    if not client_id or not client_secret:
        raise RuntimeError("Cannot read Keycloak credentials from K8s secret")

    admin_user, admin_pass = _get_admin_creds(secret_data)

    keycloak_ip = _get_keycloak_ip()
    if not keycloak_ip:
        raise RuntimeError("Cannot resolve Keycloak service IP")

    data = (
        f"client_id={client_id}&"
        f"client_secret={client_secret}&"
        f"grant_type=password&"
        f"username={admin_user}&"
        f"password={admin_pass}&"
        f"scope=openid"
    ).encode("utf-8")

    for port in [8080, 80]:
        url = (
            f"http://{keycloak_ip}:{port}/realms/afsbox/protocol/openid-connect/token"
            if port != 80
            else f"http://{keycloak_ip}/realms/afsbox/protocol/openid-connect/token"
        )
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                return res.get("access_token", "")
        except (urllib.error.HTTPError, urllib.error.URLError):
            continue

    raise RuntimeError("Failed to refresh token from Keycloak on all ports")


# ── JWT expiry helpers ────────────────────────────────────────────────

def _get_token_expiry(token: str) -> float:
    """Return the 'exp' timestamp from a JWT token."""
    try:
        payload = token.split(".")[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = base64.urlsafe_b64decode(payload).decode("utf-8")
        return json.loads(decoded).get("exp", 0)
    except Exception:
        return 0


def is_token_expired(token: str, buffer_seconds: int = 30) -> bool:
    """Check if a JWT token is expired or about to expire.

    Args:
        token: JWT access token
        buffer_seconds: consider token expired if it will expire within this many seconds
    """
    exp = _get_token_expiry(token)
    return (time.time() + buffer_seconds) >= exp


def token_remaining_seconds(token: str) -> int:
    """Return how many seconds are left before the token expires."""
    exp = _get_token_expiry(token)
    return max(0, int(exp - time.time()))


# ── Auto-refresh request wrapper ──────────────────────────────────────

def _make_headers(token: str, extra_headers: dict = None) -> dict:
    """Build request headers with Bearer token."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    return headers


def auto_request(req: urllib.request.Request, token: str,
                 max_retries: int = 1, refresh_fn: callable = None) -> bytes:
    """Execute a urllib Request with automatic token refresh on 401.

    Args:
        req: urllib.request.Request object
        token: Current access token
        max_retries: How many times to retry with a fresh token (default 1)
        refresh_fn: Custom token refresh function (default: uses refresh_token())

    Returns:
        Response body as bytes

    Raises:
        urllib.error.HTTPError: If request fails after retries
    """
    if refresh_fn is None:
        refresh_fn = refresh_token

    current_token = token
    for attempt in range(max_retries + 1):
        # Build request with current token
        if isinstance(req, str):
            # If req is a URL string, create a GET request
            req_obj = urllib.request.Request(req, headers=_make_headers(current_token))
        else:
            req_obj = urllib.request.Request(
                req.full_url,
                data=req.data,
                headers=_make_headers(current_token, dict(req.headers)),
                method=req.method,
            )

        try:
            with urllib.request.urlopen(req_obj, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt < max_retries:
                # Token expired, refresh and retry
                sys.stderr.write("  ⏳ Token expired, refreshing...\n")
                sys.stderr.flush()
                current_token = refresh_fn()
                continue
            raise


def api_request(method: str, url: str, token: str,
                data: dict = None, max_retries: int = 1,
                refresh_fn: callable = None) -> dict:
    """Convenience wrapper: make an API request with auto token refresh.

    Args:
        method: HTTP method (GET, POST, PUT, DELETE)
        url: Full URL
        token: Current access token
        data: Optional JSON body (for POST/PUT)
        max_retries: How many times to retry on 401
        refresh_fn: Custom token refresh function

    Returns:
        Parsed JSON response as dict

    Raises:
        urllib.error.HTTPError: If request fails after retries
    """
    headers = {"Content-Type": "application/json"}
    body = json.dumps(data).encode("utf-8") if data else None

    req = urllib.request.Request(
        url, data=body, headers=headers, method=method
    )
    response_bytes = auto_request(req, token, max_retries, refresh_fn)
    return json.loads(response_bytes.decode("utf-8"))


def polling_request(url: str, token: str,
                    check_interval: int = 30, refresh_fn: callable = None) -> dict:
    """Make a request, auto-refreshing token if needed.

    Unlike auto_request which retries once, this checks expiry before each call,
    making it suitable for long-running polling loops.

    Args:
        url: Full URL
        token: Current access token (will be auto-refreshed if expired)
        check_interval: How often to check token expiry (seconds)
        refresh_fn: Custom token refresh function

    Returns:
        Parsed JSON response as dict
    """
    if refresh_fn is None:
        refresh_fn = refresh_token

    # Pre-check: refresh if about to expire
    if is_token_expired(token, buffer_seconds=check_interval + 30):
        sys.stderr.write("  ⏳ Token expiring soon, refreshing before request...\n")
        sys.stderr.flush()
        token = refresh_fn()

    req = urllib.request.Request(url, headers=_make_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            sys.stderr.write("  ⏳ Token expired during request, refreshing...\n")
            sys.stderr.flush()
            token = refresh_fn()
            req = urllib.request.Request(url, headers=_make_headers(token))
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        raise


# ── Standalone usage ──────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) == 1:
        # Get a fresh token
        token = refresh_token()
        print(token)
    elif len(sys.argv) == 3 and sys.argv[1] == "--check":
        # Check if token is expired
        token = sys.argv[2]
        if is_token_expired(token):
            print("EXPIRED")
        else:
            remaining = token_remaining_seconds(token)
            print(f"VALID: {remaining}s remaining")
    else:
        print("Usage:")
        print("  python3 token_utils.py                 → print fresh token")
        print("  python3 token_utils.py --check TOKEN   → check expiry status")
