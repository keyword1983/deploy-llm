#!/usr/bin/env python3
"""
find_recipe.py — Query recipes.vllm.ai for a HuggingFace model recipe with Smart Fallback.

Usage:
  python3 find_recipe.py <HF_MODEL_ID> [HARDWARE]

Levels:
  1. Exact Match in recipes.vllm.ai
  2. Base Model Inheritance (via HF API tags/cardData)
  3. Family Keyword Matching (in recipe DB)
  4. Config Inference (via HF config.json)

Output (stdout):
  JSON
"""
import sys
import json
import urllib.request
import urllib.error
import ssl
import re
ssl._create_default_https_context = ssl._create_unverified_context

RECIPES_BASE = 'https://recipes.vllm.ai'
HF_API_BASE = 'https://huggingface.co/api'
HF_RESOLVE_BASE = 'https://huggingface.co'


def fetch_json(url: str) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'afsbox-deploy/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def find_recipe_exact(hf_id: str, recipe_db: list) -> dict | None:
    """Level 1: Exact match in recipe DB"""
    match = next((m for m in recipe_db if m.get('hf_id', '').lower() == hf_id.lower()), None)
    return match


def get_base_models(hf_id: str) -> list:
    """Level 2: Query HF API for immediate base models only"""
    model_info = fetch_json(f'{HF_API_BASE}/models/{hf_id}')
    if not model_info:
        return []
    
    # Check tags first, as HF API often puts 'base_model:owner/name' there
    tags = model_info.get('tags', [])
    bases = []
    for t in tags:
        if t.startswith('base_model:'):
            # Strip 'quantized:' or 'finetuned:' prefixes like 'base_model:quantized:owner/name'
            base = t[len('base_model:'):]
            while ':' in base and base.count(':') > 1:
                # Remove 'quantized:', 'finetuned:' etc.
                parts = base.split(':')
                if len(parts) > 1:
                    base = ':'.join(parts[1:])
                else:
                    break
            if '/' in base:
                bases.append(base)
    if bases:
        return bases

    # Fallback to cardData
    card_data = model_info.get('cardData', {}) or {}
    base = card_data.get('base_model', [])
    
    if isinstance(base, str):
        base = base.strip()
        if base and '/' in base:
            return [base]
    elif isinstance(base, list):
        cleaned = []
        for b in base:
            if isinstance(b, str):
                b = b.strip()
                if '/' in b:
                    cleaned.append(b)
        return cleaned
    return []


def trace_ancestry(hf_id: str, visited: set | None = None) -> list:
    """Recursively trace the full ancestry chain of a model.
    
    Returns ordered list: [immediate_parent, grandparent, ..., root_base]
    """
    if visited is None:
        visited = set()
    
    if hf_id.lower() in visited:
        return []  # Prevent infinite loops
    visited.add(hf_id.lower())
    
    immediate_bases = get_base_models(hf_id)
    if not immediate_bases:
        return []
    
    chain = []
    for base in immediate_bases:
        chain.append(base)
        # Recurse deeper
        deeper = trace_ancestry(base, visited)
        chain.extend(deeper)
        # Only need to trace the first branch deep enough
        break
    
    return chain


def find_recipe_by_keywords(hf_id: str, recipe_db: list) -> dict | None:
    """Level 3: Keyword matching in recipe DB"""
    families = ['qwen', 'gemma', 'llama', 'mistral', 'phi', 'yi', 'mixtral', 'deepseek', 'internlm']
    found_family = None
    for f in families:
        if f in hf_id.lower():
            found_family = f
            break
    
    if not found_family:
        return None
        
    size_match = re.search(r'(\d+)[bB]', hf_id)
    target_size = int(size_match.group(1)) if size_match else None
    
    best_match = None
    min_diff = float('inf')
    
    for m in recipe_db:
        m_name = m.get('hf_id', '').lower()
        m_label = m.get('name', '').lower()
        
        if found_family not in m_name and found_family not in m_label:
            continue
            
        m_size_match = re.search(r'(\d+)[bB]', m_name)
        if m_size_match:
            m_size = int(m_size_match.group(1))
            diff = abs(m_size - target_size) if target_size else 9999
            if diff < min_diff:
                min_diff = diff
                best_match = m
        else:
            if min_diff > 1000: 
                min_diff = 1000
                best_match = m
                
    return best_match


def infer_from_config(hf_id: str) -> dict:
    """Level 4: Infer parameters from HF config.json"""
    config = fetch_json(f'{HF_RESOLVE_BASE}/{hf_id}/resolve/main/config.json')
    if not config:
        return None
    
    max_model_len = config.get('max_position_embeddings', 4096)
    text_cfg = config.get('text_config', {})
    if text_cfg and 'max_position_embeddings' in text_cfg:
        max_model_len = text_cfg['max_position_embeddings']
        
    argv = [
        'vllm', 'serve', hf_id,
        '--max-model-len', str(max_model_len),
        '--tensor-parallel-size', '1'
    ]
    
    return {
        'found': False,
        'argv': argv,
        'note': f'Inferred from HF config.json (max_model_len: {max_model_len})',
        'inferred_max_model_len': max_model_len
    }


def load_and_process_recipe(match: dict, hardware: str | None) -> dict:
    """Load the full recipe JSON and process it"""
    recipe_path = match.get('json', '')
    recipe_url = f'{RECIPES_BASE}{recipe_path}'
    recipe = fetch_json(recipe_url)
    
    if not recipe:
        return None
        
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
        'argv': recommended.get('argv', ['vllm', 'serve', match.get('hf_id', '')]),
        'variants': {
            k: {'precision': v.get('precision', ''), 'vram_minimum_gb': v.get('vram_minimum_gb', 0)}
            for k, v in variants.items()
        },
        'has_fp8': 'fp8' in variants or 'nvidia_fp8' in variants,
        'has_nvfp4': 'nvfp4' in variants,
        'hardware': hardware or 'default',
    }
    return result


def main():
    if len(sys.argv) < 2:
        print('ERROR: usage: find_recipe.py <HF_MODEL_ID> [HARDWARE]')
        sys.exit(1)

    hf_model_id = sys.argv[1]
    hardware = sys.argv[2].lower() if len(sys.argv) > 2 else None

    # Load recipe DB once
    recipe_db = fetch_json(f'{RECIPES_BASE}/models.json')
    if not recipe_db:
        # Ultimate Fallback if Recipe DB is unreachable
        print(json.dumps({
            'found': False,
            'argv': ['vllm', 'serve', hf_model_id, '--tensor-parallel-size', '1'],
            'note': 'Could not reach recipes.vllm.ai'
        }))
        sys.exit(0)

    # --- LEVEL 1: Exact Match ---
    match = find_recipe_exact(hf_model_id, recipe_db)
    if match:
        result = load_and_process_recipe(match, hardware)
        if result:
            print(json.dumps(result))
            sys.exit(0)
            
    # --- LEVEL 2: Base Model Inheritance (Recursive) ---
    ancestry_chain = trace_ancestry(hf_model_id)
    for ancestor in ancestry_chain:
        ancestor_match = find_recipe_exact(ancestor, recipe_db)
        if ancestor_match:
            result = load_and_process_recipe(ancestor_match, hardware)
            if result:
                result['base_model_inherited'] = ancestor
                result['ancestry_chain'] = ancestry_chain
                result['note'] = f'Exact match not found for {hf_model_id}, inherited from ancestor: {ancestor} (chain: {" -> ".join(ancestry_chain)})'
                print(json.dumps(result))
                sys.exit(0)
                
    # --- LEVEL 3: Family Keyword Matching ---
    keyword_match = find_recipe_by_keywords(hf_model_id, recipe_db)
    if keyword_match:
        result = load_and_process_recipe(keyword_match, hardware)
        if result:
            result['note'] = f'Exact match not found. Approximate match found in recipe DB: {keyword_match.get("hf_id")}'
            print(json.dumps(result))
            sys.exit(0)
            
    # --- LEVEL 4: Config Inference ---
    inferred = infer_from_config(hf_model_id)
    if inferred:
        print(json.dumps(inferred))
        sys.exit(0)
        
    # Ultimate Fallback
    print(json.dumps({
        'found': False,
        'argv': ['vllm', 'serve', hf_model_id, '--tensor-parallel-size', '1'],
        'note': 'All fallback levels failed. Using ultimate fallback.'
    }))
    sys.exit(0)


if __name__ == '__main__':
    main()
