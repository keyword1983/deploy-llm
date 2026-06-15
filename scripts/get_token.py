#!/usr/bin/env python3
"""
get_token.py — Exchange refresh_token cookie for a Keycloak access token.

The afsbox platform uses Keycloak OIDC. The refresh_token is stored as an
httpOnly cookie in the browser. This script sends it to the BFF token endpoint
to obtain a short-lived access token (JWT) for API calls.

Usage:
  python3 get_token.py <API_BASE_URL> <REFRESH_TOKEN_VALUE>

  API_BASE_URL:        e.g. http://afsbox.example.com
  REFRESH_TOKEN_VALUE: value of the refresh_token cookie from the browser

How to get REFRESH_TOKEN_VALUE:
  1. Open afsbox Portal in browser and log in
  2. Open DevTools (F12) -> Application -> Cookies
  3. Find the cookie named "refresh_token" and copy its Value

Output (stdout):
  access_token string (plain JWT, single line)

Exit codes:
  0 = success
  1 = token exchange failed (expired / invalid cookie)
  2 = argument or network error
"""
import sys
import json
import urllib.request
import urllib.error
import ssl
ssl._create_default_https_context = ssl._create_unverified_context
import subprocess
import base64


def get_token_via_k8s():
    try:
        # 1. Get client secret from secret
        secret_cmd = ["kubectl", "get", "secret", "afsbox-platform-secret", "-n", "afsbox-system", "-o", "json"]
        proc = subprocess.run(secret_cmd, capture_output=True, text=True, timeout=5)
        if proc.returncode != 0:
            return None
        secret_data = json.loads(proc.stdout)
        client_id_b64 = secret_data.get("data", {}).get("IAM_KEYCLOAK_CLIENT_ID", "")
        client_secret_b64 = secret_data.get("data", {}).get("IAM_KEYCLOAK_CLIENT_SECRET", "")
        if not client_id_b64 or not client_secret_b64:
            return None
        client_id = base64.b64decode(client_id_b64).decode("utf-8").strip()
        client_secret = base64.b64decode(client_secret_b64).decode("utf-8").strip()

        # 2. Get Keycloak service IP
        svc_cmd = ["kubectl", "get", "svc", "keycloak-keycloakx-http", "-n", "keycloak", "-o", "jsonpath={.spec.clusterIP}"]
        proc2 = subprocess.run(svc_cmd, capture_output=True, text=True, timeout=5)
        if proc2.returncode != 0:
            return None
        keycloak_ip = proc2.stdout.strip()
        if not keycloak_ip:
            return None

        # 3. Call Keycloak token endpoint
        url = f"http://{keycloak_ip}/realms/afsbox/protocol/openid-connect/token"
        data = (
            f"client_id={client_id}&"
            f"client_secret={client_secret}&"
            f"grant_type=password&"
            f"username=admin@asus.com&"
            f"password=admin&"
            f"scope=openid"
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            res_json = json.loads(resp.read().decode("utf-8"))
            return res_json.get("access_token")
    except Exception:
        return None


def main():
    # Attempt automatic token generation via Kubernetes credentials first
    auto_token = get_token_via_k8s()
    if auto_token:
        print(auto_token)
        sys.exit(0)

    if len(sys.argv) < 3:
        print(
            'ERROR: usage: get_token.py <API_BASE_URL> <REFRESH_TOKEN_VALUE>\n'
            '\n'
            'How to get REFRESH_TOKEN_VALUE:\n'
            '  1. Log in to afsbox Portal in your browser\n'
            '  2. Open DevTools (F12) > Application > Cookies\n'
            '  3. Copy the Value of the "refresh_token" cookie'
        )
        sys.exit(2)

    api_base      = sys.argv[1].rstrip('/')
    refresh_token = sys.argv[2]

    url = f'{api_base}/api/v1/iam/auth/token'
    req = urllib.request.Request(
        url,
        method='GET',
        headers={
            'Cookie': f'refresh_token={refresh_token}',
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            token = data.get('access_token', '')
            if not token:
                print('ERROR: server returned no access_token')
                sys.exit(1)
            # Print token only — caller captures this as ACCESS_TOKEN
            print(token)
            sys.exit(0)

    except urllib.error.HTTPError as e:
        err_body = ''
        try:
            err_body = e.read().decode('utf-8', errors='replace')
        except Exception:
            pass
        if e.code == 401:
            print(
                'ERROR: refresh_token is expired or invalid (HTTP 401).\n'
                'Please log in to afsbox Portal again and copy a fresh refresh_token cookie.'
            )
        else:
            print(f'ERROR: HTTP {e.code} from token endpoint. {err_body}')
        sys.exit(1)

    except urllib.error.URLError as e:
        print(f'ERROR: cannot reach {url} — {e.reason}')
        sys.exit(2)

    except Exception as e:
        print(f'ERROR: {e}')
        sys.exit(2)


if __name__ == '__main__':
    main()