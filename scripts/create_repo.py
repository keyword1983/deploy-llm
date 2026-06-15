#!/usr/bin/env python3
"""
create_repo.py — Create a ModelRepository to trigger model download from HuggingFace.

Usage:
  python3 create_repo.py <API_BASE_URL> <ACCESS_TOKEN> <PROJECT_ID> <HF_MODEL_ID>
                         <REPO_NAME> [CREDENTIAL_ID]

  API_BASE_URL:   e.g. http://afsbox.example.com
  ACCESS_TOKEN:   Keycloak JWT token
  PROJECT_ID:     target project ID
  HF_MODEL_ID:    e.g. google/gemma-4-12b-it
  REPO_NAME:      slug name, e.g. google-gemma-4-12b-it
  CREDENTIAL_ID:  optional, for gated/private models

Output (stdout):
  JSON: { created: true, repo_name: str, source_uri: str }
  or:   JSON: { created: false, reason: str }  if already exists (non-Ready)
  or:   ERROR: <message>

Exit codes:
  0 = successfully created (HTTP 201) or already exists
  1 = creation failed
  2 = argument / request error
"""
import sys
import json
import urllib.request
import urllib.error
import ssl
ssl._create_default_https_context = ssl._create_unverified_context


def main():
    if len(sys.argv) < 6:
        print('ERROR: usage: create_repo.py <API_BASE_URL> <ACCESS_TOKEN> <PROJECT_ID> <HF_MODEL_ID> <REPO_NAME> [CREDENTIAL_ID]')
        sys.exit(2)

    api_base      = sys.argv[1].rstrip('/')
    token         = sys.argv[2]
    project_id    = sys.argv[3]
    hf_model_id   = sys.argv[4]
    repo_name     = sys.argv[5]
    credential_id = sys.argv[6] if len(sys.argv) > 6 else None

    url = f'{api_base}/api/v1/models/projects/{project_id}/repositories'

    body = {
        'name':        repo_name,
        'model_name':  hf_model_id,
        'source_uri':  f'hf://{hf_model_id}',
        'revision':    'main',
        'model_type':  'llm',
        'enabled':     True,
    }
    if credential_id:
        body['credential_id'] = credential_id

    payload = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=payload,
        method='POST',
        headers={
            'Authorization':  f'Bearer {token}',
            'Content-Type':   'application/json',
        },
    )

    try:
        with urllib.request.urlopen(req) as resp:
            resp_data = json.loads(resp.read() or b'{}')
            print(json.dumps({
                'created':    True,
                'repo_name':  repo_name,
                'source_uri': f'hf://{hf_model_id}',
                'response':   resp_data,
            }))
            sys.exit(0)

    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            err_body = json.loads(body_bytes)
        except Exception:
            err_body = body_bytes.decode('utf-8', errors='replace')

        # 409 Conflict = already exists, treat as OK
        if e.code == 409:
            print(json.dumps({
                'created':    False,
                'reason':     'already_exists',
                'repo_name':  repo_name,
                'source_uri': f'hf://{hf_model_id}',
            }))
            sys.exit(0)

        print(f'ERROR: HTTP {e.code} — {err_body}')
        sys.exit(1)

    except Exception as e:
        print(f'ERROR: {e}')
        sys.exit(2)


if __name__ == '__main__':
    main()