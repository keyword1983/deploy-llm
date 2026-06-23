#!/usr/bin/env python3
"""
bootstrap_env.py — Auto-discover API_BASE_URL, ACCESS_TOKEN, PROJECT_ID.

Output (JSON to stdout):
{
    "api_base_url": "...",
    "access_token": "...",
    "project_id": "...",
    "project_namespace": "..."
}

Exit codes:
    0 = success
    1 = discovery failed
"""
import sys
import json
import os
import urllib.request
import urllib.error
import ssl
import subprocess
import base64
import socket
import re
import tempfile
import shutil

ssl._create_default_https_context = ssl._create_unverified_context


def get_token():
    """Get access token via K8s secrets + Keycloak, fallback to env var."""
    # Priority 1: environment variable
    env_token = os.environ.get("ACCESS_TOKEN", "")
    if env_token and len(env_token) > 50:
        return env_token

    # Priority 2: auto via K8s/Keycloak using shared token_utils
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from token_utils import refresh_token
        return refresh_token()
    except Exception:
        pass

    return None


def resolve_url(url, token, timeout=5):
    """Try to reach a URL, return (ok, parsed_iss)."""
    test_url = f"{url}/api/v1/projects"
    req = urllib.request.Request(test_url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, None
    except urllib.error.HTTPError as e:
        # Even 401 means URL is reachable — extract iss from error if possible
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        if e.code == 401:
            # Token may be for a different realm, but URL works
            return True, None
        return False, body
    except Exception as e:
        return False, str(e)


def get_api_base_url(token):
    """Auto-discover API_BASE_URL via multiple strategies."""
    # Priority 1: environment variable
    env_url = os.environ.get("API_BASE_URL", "").rstrip("/")
    if env_url:
        ok, _ = resolve_url(env_url, token)
        if ok:
            return env_url

    # Priority 2: internal K8s service FQDN
    internal_url = "http://afsbox-platform.afsbox-system.svc.cluster.local"
    ok, _ = resolve_url(internal_url, token)
    if ok:
        return internal_url

    # Priority 3: try to extract external URL from token's iss field
    try:
        payload = token.split(".")[1]
        # Add padding if needed
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = base64.urlsafe_b64decode(payload).decode("utf-8")
        iss_data = json.loads(decoded)
        iss = iss_data.get("iss", "")
        if iss:
            # iss is like https://host:port/realms/afsbox
            external_base = re.sub(r"/realms/.*$", "", iss)
            ok, _ = resolve_url(external_base, token)
            if ok:
                return external_base
    except Exception:
        pass

    # Priority 4: try common sslip.io patterns from K8s ingress (with Traefik NodePort support)
    http_port = ""
    https_port = ""
    try:
        proc = subprocess.run(
            ["kubectl", "get", "svc", "-A", "-o", "json"],
            capture_output=True, text=True, timeout=5
        )
        if proc.returncode == 0:
            svc_data = json.loads(proc.stdout)
            for item in svc_data.get("items", []):
                if "traefik" in item.get("metadata", {}).get("name", "").lower():
                    for p in item.get("spec", {}).get("ports", []):
                        if p.get("port") == 80:
                            node_port = p.get("nodePort")
                            if node_port:
                                http_port = f":{node_port}"
                        elif p.get("port") == 443:
                            node_port = p.get("nodePort")
                            if node_port:
                                https_port = f":{node_port}"
    except Exception:
        pass

    try:
        proc = subprocess.run(
            ["kubectl", "get", "ingress", "-n", "afsbox-system", "-o", "json"],
            capture_output=True, text=True, timeout=5
        )
        if proc.returncode == 0:
            ingress_data = json.loads(proc.stdout)
            for item in ingress_data.get("items", []):
                for rule in item.get("spec", {}).get("rules", []):
                    host = rule.get("host", "")
                    if host:
                        # Try https with nodeport first, then http with nodeport, then default
                        url_https = f"https://{host}{https_port}"
                        ok, _ = resolve_url(url_https, token)
                        if ok:
                            return url_https
                            
                        url_http = f"http://{host}{http_port}"
                        ok, _ = resolve_url(url_http, token)
                        if ok:
                            return url_http
    except Exception:
        pass

    return None


def get_project(token, api_base):
    """Get the first ready project's namespace."""
    # Priority 1: env var
    env_pid = os.environ.get("PROJECT_ID", "")
    if env_pid:
        return env_pid

    url = f"{api_base}/api/v1/projects"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for proj in data.get("projects", []):
                if proj.get("phase") == "Ready":
                    return proj.get("namespace", "")
    except Exception:
        pass

    return ""


def main():
    token = get_token()
    if not token:
        print(json.dumps({"error": "Cannot obtain access token"}), file=sys.stderr)
        sys.exit(1)

    api_base = get_api_base_url(token)
    if not api_base:
        print(json.dumps({"error": "Cannot discover API_BASE_URL"}), file=sys.stderr)
        sys.exit(1)

    project_id = get_project(token, api_base)
    if not project_id:
        print(json.dumps({"error": "No ready project found"}), file=sys.stderr)
        sys.exit(1)

    result = {
        "api_base_url": api_base,
        "access_token": token,
        "project_id": project_id,
    }
    print(json.dumps(result))
    sys.exit(0)


if __name__ == "__main__":
    main()
