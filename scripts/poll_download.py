#!/usr/bin/env python3
"""
poll_download.py — Poll ModelRepository until phase is Ready or Failed.

Usage:
  python3 poll_download.py <API_BASE_URL> <ACCESS_TOKEN> <PROJECT_ID> <REPO_NAME>
                           [TIMEOUT_SECONDS] [INTERVAL_SECONDS]

  TIMEOUT_SECONDS:  default 1800 (30 min)
  INTERVAL_SECONDS: default 30

Output (stdout):
  Progress lines:  phase=Running elapsed=60s
  Final line:      READY:<json>  or  FAILED:<message>  or  TIMEOUT

Exit codes:
  0 = Ready
  1 = Failed
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
        print('ERROR: usage: poll_download.py <API_BASE_URL> <ACCESS_TOKEN> <PROJECT_ID> <REPO_NAME> [TIMEOUT] [INTERVAL]')
        sys.exit(2)

    api_base = sys.argv[1].rstrip('/')
    token = sys.argv[2]
    project_id = sys.argv[3]
    repo_name = sys.argv[4]
    timeout = int(sys.argv[5]) if len(sys.argv) > 5 else 1800
    interval = int(sys.argv[6]) if len(sys.argv) > 6 else 30

    url = f'{api_base}/api/v1/models/projects/{project_id}/repositories/{repo_name}'

    start = time.time()
    while time.time() - start < timeout:
        # Auto-refresh token if about to expire during this polling cycle
        if is_token_expired(token, buffer_seconds=interval + 30):
            try:
                sys.stderr.write('  ⏳ Token expiring, refreshing...\n')
                sys.stderr.flush()
                token = refresh_token()
            except Exception as e:
                print(f'  Token refresh failed: {e}', flush=True)

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
            print(f'  HTTP {e.code} — retrying...', flush=True)
            time.sleep(interval)
            continue
        except Exception as e:
            print(f'  Request error: {e} — retrying...', flush=True)
            time.sleep(interval)
            continue

        phase = data.get('phase', '')
        elapsed = int(time.time() - start)
        print(f'  phase={phase} elapsed={elapsed}s', flush=True)

        if phase == 'Ready':
            mi = data.get('model_info') or {}
            result = {
                'repo_name': repo_name,
                'required_min_gpu_memory': mi.get('required_min_gpu_memory'),
                'kv_cache_memory_per_token': mi.get('kv_cache_memory_per_token'),
                'context_length': mi.get('context_length'),
                'torch_dtype': mi.get('torch_dtype'),
                'parameters': mi.get('parameters'),
                'total_size': mi.get('total_size'),
                'architecture': mi.get('architecture'),
                'hidden_size': mi.get('hidden_size'),
                'num_layers': mi.get('num_layers'),
                'num_heads': mi.get('num_heads'),
                'num_key_value_heads': mi.get('num_key_value_heads'),
            }
            print(f'READY:{json.dumps(result)}')
            sys.exit(0)

        elif phase == 'Failed':
            msg = data.get('message', 'unknown error')
            print(f'FAILED:{msg}')
            sys.exit(1)

        time.sleep(interval)

    print(f'TIMEOUT: model download exceeded {timeout}s')
    sys.exit(2)


if __name__ == '__main__':
    main()