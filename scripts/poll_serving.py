#!/usr/bin/env python3
"""
poll_serving.py — Poll ModelServing until internalEndpoint is available.

Usage:
  python3 poll_serving.py <API_BASE_URL> <ACCESS_TOKEN> <PROJECT_ID> <SERVING_NAME>
                          [TIMEOUT_SECONDS] [INTERVAL_SECONDS]

  TIMEOUT_SECONDS:  default 600 (10 min)
  INTERVAL_SECONDS: default 15

Output (stdout):
  Progress lines:  waiting... 30s
  Final line:      READY:<json>  or  TIMEOUT

Exit codes:
  0 = Ready (endpoint available)
  2 = Timeout or request error
"""
import sys
import os
import json
import time
import urllib.request
import urllib.error
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

# Allow import from scripts directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from token_utils import is_token_expired, refresh_token


def main():
    if len(sys.argv) < 5:
        print('ERROR: usage: poll_serving.py <API_BASE_URL> <ACCESS_TOKEN> <PROJECT_ID> <SERVING_NAME> [TIMEOUT] [INTERVAL]')
        sys.exit(2)

    api_base     = sys.argv[1].rstrip('/')
    token        = sys.argv[2]
    project_id   = sys.argv[3]
    serving_name = sys.argv[4]
    timeout      = int(sys.argv[5]) if len(sys.argv) > 5 else 600
    interval     = int(sys.argv[6]) if len(sys.argv) > 6 else 15

    url = f'{api_base}/api/v1/models/projects/{project_id}/servings/{serving_name}'

    start = time.time()
    while time.time() - start < timeout:
        # Auto-refresh token if about to expire during this polling cycle
        if is_token_expired(token, buffer_seconds=interval + 30):
            try:
                sys.stderr.write('  ⏳ Token expiring, refreshing...\n')
                sys.stderr.flush()
                token = refresh_token()
            except Exception as e:
                print(f'  Token refresh failed: {e}', file=sys.stderr, flush=True)

        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                try:
                    sys.stderr.write('  ⏳ Token expired (401), refreshing...\n')
                    sys.stderr.flush()
                    token = refresh_token()
                    continue
                except Exception:
                    pass
            print(f'  HTTP {e.code} — retrying...', file=sys.stderr, flush=True)
            time.sleep(interval)
            continue
        except Exception as e:
            print(f'  Request error: {e} — retrying...', file=sys.stderr, flush=True)
            time.sleep(interval)
            continue

        output   = data.get('output') or {}
        endpoint = (output.get('internalEndpoint') or {}).get('value', '')
        elapsed  = int(time.time() - start)
        print(f'  waiting... {elapsed}s', file=sys.stderr, flush=True)

        if endpoint:
            model_name = (output.get('servedModelName') or {}).get('value', '')
            external_endpoint = (output.get('externalEndpoint') or {}).get('value', '')
            
            # Smoke test health check
            sys.stderr.write(f'  ⏳ Endpoint available, running health check on {model_name}...\n')
            sys.stderr.flush()
            
            import socket
            try:
                # Run health check
                url = f"{endpoint.rstrip('/')}/v1/models"
                # Use standard host and model headers required by the gateway
                req_hc = urllib.request.Request(url, headers={
                    'Authorization': f'Bearer {token}',
                    'Host': 'afsbox-aigateway.afsbox-system.svc.cluster.local',
                    'x-model': model_name
                })
                # Check health
                with urllib.request.urlopen(req_hc, timeout=5) as resp_hc:
                    res_data = json.loads(resp_hc.read().decode('utf-8'))
                    models = [m.get('id') for m in res_data.get('data', [])]
                    sys.stderr.write(f'  ✅ Health check passed. Available models: {models}\n')
                    sys.stderr.flush()
            except urllib.error.HTTPError as he:
                if he.code in [502, 503, 504, 404]:
                    sys.stderr.write(f'  ⏳ Model is still loading (HTTP {he.code})...\n')
                    sys.stderr.flush()
                    time.sleep(interval)
                    continue
                else:
                    sys.stderr.write(f'  ⚠️ Health check returned HTTP {he.code}. Assuming ready.\n')
                    sys.stderr.flush()
            except urllib.error.URLError as ue:
                # If network is not reachable (e.g. external environment), skip health check
                if isinstance(ue.reason, socket.gaierror) or 'connection refused' in str(ue.reason).lower():
                    sys.stderr.write('  ⚠️ Endpoint is not network-reachable from this host. Skipping health check.\n')
                    sys.stderr.flush()
                else:
                    sys.stderr.write(f'  ⏳ Health check network issue: {ue.reason}. Retrying...\n')
                    sys.stderr.flush()
                    time.sleep(interval)
                    continue
            except Exception as e_hc:
                sys.stderr.write(f'  ⚠️ Health check failed with unexpected error: {e_hc}. Assuming ready.\n')
                sys.stderr.flush()

            result = {
                'internal':   endpoint,
                'external':   external_endpoint,
                'model_name': model_name,
                'elapsed':    elapsed,
            }
            print(f'READY:{json.dumps(result)}')
            sys.exit(0)

        time.sleep(interval)

    print(f'TIMEOUT: serving did not become ready within {timeout}s')
    sys.exit(2)


if __name__ == '__main__':
    main()