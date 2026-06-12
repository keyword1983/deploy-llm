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
import json
import time
import urllib.request
import urllib.error


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
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }

    start = time.time()
    while time.time() - start < timeout:
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            print(f'  HTTP {e.code} — retrying...', flush=True)
            time.sleep(interval)
            continue
        except Exception as e:
            print(f'  Request error: {e} — retrying...', flush=True)
            time.sleep(interval)
            continue

        output   = data.get('output') or {}
        endpoint = (output.get('internalEndpoint') or {}).get('value', '')
        elapsed  = int(time.time() - start)
        print(f'  waiting... {elapsed}s', flush=True)

        if endpoint:
            result = {
                'internal':   endpoint,
                'external':   (output.get('externalEndpoint') or {}).get('value', ''),
                'model_name': (output.get('servedModelName') or {}).get('value', ''),
                'elapsed':    elapsed,
            }
            print(f'READY:{json.dumps(result)}')
            sys.exit(0)

        time.sleep(interval)

    print(f'TIMEOUT: serving did not become ready within {timeout}s')
    sys.exit(2)


if __name__ == '__main__':
    main()