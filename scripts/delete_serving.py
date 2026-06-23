#!/usr/bin/env python3
"""
delete_serving.py — Delete a ModelServing via afsbox API.

Usage:
  python3 delete_serving.py <API_BASE_URL> <ACCESS_TOKEN> <PROJECT_ID> <SERVING_NAME>

  API_BASE_URL:   e.g. http://afsbox.example.com
  ACCESS_TOKEN:   Keycloak JWT token
  PROJECT_ID:     target project ID
  SERVING_NAME:   name of the serving to delete

Output (stdout):
  JSON: { "deleted": true, "serving_name": str }
  or:   JSON: { "deleted": true, "reason": "not_found", "serving_name": str }
  or:   ERROR: <message>

Exit codes:
  0 = successfully deleted or already deleted (404)
  1 = deletion failed
  2 = argument / request error
"""
import sys
import os
import json
import urllib.error

# Allow import from scripts directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from token_utils import api_request


def main():
    if len(sys.argv) < 5:
        print('ERROR: usage: delete_serving.py <API_BASE_URL> <ACCESS_TOKEN> <PROJECT_ID> <SERVING_NAME>')
        sys.exit(2)

    api_base     = sys.argv[1].rstrip('/')
    token        = sys.argv[2]
    project_id   = sys.argv[3]
    serving_name = sys.argv[4]

    url = f'{api_base}/api/v1/models/projects/{project_id}/servings/{serving_name}'

    try:
        resp_data = api_request("DELETE", url, token)
        print(json.dumps({
            'deleted':      True,
            'serving_name': serving_name,
            'response':     resp_data,
        }))
        sys.exit(0)

    except urllib.error.HTTPError as e:
        # 404 = not found, already deleted
        if e.code == 404:
            print(json.dumps({
                'deleted':      True,
                'reason':       'not_found',
                'serving_name': serving_name,
            }))
            sys.exit(0)

        body_bytes = e.read()
        try:
            err_body = json.loads(body_bytes)
        except Exception:
            err_body = body_bytes.decode('utf-8', errors='replace')

        print(f'ERROR: HTTP {e.code} — {err_body}')
        sys.exit(1)

    except Exception as e:
        print(f'ERROR: {e}')
        sys.exit(2)


if __name__ == '__main__':
    main()
