#!/usr/bin/env python3
"""
find_recipe.py — Query recipes.vllm.ai for a HuggingFace model recipe.

Usage:
  python3 find_recipe.py <HF_MODEL_ID> [HARDWARE]

  HF_MODEL_ID: e.g. google/gemma-4-12b-it
  HARDWARE:    optional, e.g. h100, h200, mi300x

Output (stdout):
  JSON: {
    found: bool,
    min_vllm_version: str,
    argv: list[str],
    variants: dict,
    has_fp8: bool,
    has_nvfp4: bool,
    recipe_url: str
  }
  or on not found:
  JSON: { found: false, argv: [fallback...] }

Exit codes:
  0 = success (recipe found or fallback)
  1 = argument error
"""
import sys
import json
import urllib.request
import urllib.error
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

RECIPES_BASE = 'https://recipes.vllm.ai'


def fetch_json(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def main():
    if len(sys.argv) < 2:
        print('ERROR: usage: find_recipe.py <HF_MODEL_ID> [HARDWARE]')
        sys.exit(1)

    hf_model_id = sys.argv[1]
    hardware = sys.argv[2].lower() if len(sys.argv) > 2 else None

    # Step 1: Search model list
    models = fetch_json(f'{RECIPES_BASE}/models.json')
    if not models:
        # Fallback on network error
        print(json.dumps({
            'found': False,
            'argv': ['vllm', 'serve', hf_model_id, '--tensor-parallel-size', '1'],
            'note': 'Could not reach recipes.vllm.ai',
        }))
        sys.exit(0)

    match = next((m for m in models if m.get('hf_id', '').lower() == hf_model_id.lower()), None)
    if not match:
        print(json.dumps({
            'found': False,
            'argv': ['vllm', 'serve', hf_model_id, '--tensor-parallel-size', '1'],
            'note': f'No recipe found for {hf_model_id}',
        }))
        sys.exit(0)

    recipe_path = match.get('json', '')
    recipe_url = f'{RECIPES_BASE}{recipe_path}'

    # Step 2: Fetch full recipe
    recipe = fetch_json(recipe_url)
    if not recipe:
        print(json.dumps({
            'found': False,
            'argv': ['vllm', 'serve', hf_model_id, '--tensor-parallel-size', '1'],
            'note': f'Could not fetch recipe from {recipe_url}',
        }))
        sys.exit(0)

    # Step 3: Try hardware-specific config if requested
    hw_config = None
    if hardware:
        hw_url = f'{RECIPES_BASE}{recipe_path.replace(".json", "")}/hw/{hardware}.json'
        hw_config = fetch_json(hw_url)

    recommended = hw_config or recipe.get('recommended_command', {})
    variants = recipe.get('variants', {})

    result = {
        'found': True,
        'recipe_url': recipe_url,
        'min_vllm_version': recipe.get('model', {}).get('min_vllm_version', '0.0.0'),
        'argv': recommended.get('argv', ['vllm', 'serve', hf_model_id]),
        'variants': {
            k: {'precision': v.get('precision', ''), 'vram_minimum_gb': v.get('vram_minimum_gb', 0)}
            for k, v in variants.items()
        },
        'has_fp8': 'fp8' in variants or 'nvidia_fp8' in variants,
        'has_nvfp4': 'nvfp4' in variants,
        'hardware': hardware or 'default',
    }
    print(json.dumps(result))
    sys.exit(0)


if __name__ == '__main__':
    main()