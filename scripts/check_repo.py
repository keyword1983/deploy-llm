#!/usr/bin/env python3
"""
check_repo.py — Check if a ModelRepository already exists and is Ready.
                Also generates the repo_name slug.

Usage:
  python3 check_repo.py <API_BASE_URL> <ACCESS_TOKEN> <PROJECT_ID> <HF_MODEL_ID>

Output (stdout):
  JSON: {
    exists: bool,
    repo_name: str,       # existing name if found, or generated slug
    phase: str,           # current phase if exists
    slug: str             # always the generated slug
  }

Exit codes:
  0 = success
  2 = API request failed
"""
import sys
import os
import json
import urllib.request
import urllib.error
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from token_utils import refresh_token


def make_slug(hf_model_id: str) -> str:
    """Convert HF model ID to a valid k8s-compatible repo name."""
    return hf_model_id.replace('/', '-').lower()[:40].rstrip('-')


def main():
    if len(sys.argv) < 5:
        print('ERROR: usage: check_repo.py <API_BASE_URL> <ACCESS_TOKEN> <PROJECT_ID> <HF_MODEL_ID>')
        sys.exit(1)

    api_base = sys.argv[1].rstrip('/')
    token = sys.argv[2]
    project_id = sys.argv[3]
    hf_model_id = sys.argv[4]

    slug = make_slug(hf_model_id)
    source_uri = f'hf://{hf_model_id}'

    url = f'{api_base}/api/v1/models/projects/{project_id}/repositories'
    req = urllib.request.Request(url, headers={
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    })

    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            try:
                sys.stderr.write('  ⏳ Token expired (401), refreshing...\n')
                sys.stderr.flush()
                token = refresh_token()
                req = urllib.request.Request(url, headers={
                    'Authorization': f'Bearer {token}',
                    'Content-Type': 'application/json',
                })
                with urllib.request.urlopen(req) as resp:
                    data = json.loads(resp.read())
            except Exception as e2:
                print(f'ERROR: HTTP 401 + refresh failed: {e2}')
                sys.exit(2)
        else:
            print(f'ERROR: HTTP {e.code} calling {url}')
            sys.exit(2)
    except Exception as e:
        print(f'ERROR: {e}')
        sys.exit(2)

    repos = data.get('model_repositories', [])

    # Find any repo with matching source_uri (case-insensitive)
    found = next(
        (r for r in repos if r.get('source_uri', '').lower() == source_uri.lower()),
        None
    )

    if found and found.get('phase') == 'Ready':
        print(json.dumps({
            'exists': True,
            'repo_name': found['name'],
            'phase': found.get('phase', ''),
            'slug': slug,
        }))
    else:
        print(json.dumps({
            'exists': False,
            'repo_name': slug,
            'phase': found.get('phase', '') if found else '',
            'slug': slug,
        }))
    sys.exit(0)


if __name__ == '__main__':
    main()