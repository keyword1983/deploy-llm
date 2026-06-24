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


def fetch_json(url: str):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'afsbox-deploy/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def check_features_from_hf(hf_id: str) -> tuple:
    """Check if model supports reasoning and tool calling by analyzing HF configs.
    
    Returns (has_reasoning, has_tools)
    """
    has_reasoning = False
    has_tools = False

    hf_id_lower = hf_id.lower()
    is_instruct = any(x in hf_id_lower for x in ['instruct', 'chat', '-it', 'distill', 'coder', 'qwq', 'r1', 'thinking'])
    if not is_instruct:
        return False, False

    # 1. Fetch tokenizer_config.json
    tok_url = f'{HF_RESOLVE_BASE}/{hf_id}/resolve/main/tokenizer_config.json'
    tok_cfg = fetch_json(tok_url)
    if tok_cfg:
        chat_temp = tok_cfg.get('chat_template', '')
        if isinstance(chat_temp, str):
            chat_temp_lower = chat_temp.lower()
            # Check for tools in chat_template
            if 'tools' in chat_temp_lower or 'tool_use' in chat_temp_lower or 'tool_call' in chat_temp_lower:
                has_tools = True
            # Check for thinking/reasoning tags in chat_template
            if 'think' in chat_temp_lower or 'reasoning' in chat_temp_lower:
                has_reasoning = True
        
        # Check added_tokens
        added_tokens = tok_cfg.get('added_tokens_decoder', {}) or {}
        for token_val in added_tokens.values():
            content = token_val.get('content', '') if isinstance(token_val, dict) else str(token_val)
            if '<think>' in content or '</think>' in content:
                has_reasoning = True

    # 2. Fetch generation_config.json
    gen_url = f'{HF_RESOLVE_BASE}/{hf_id}/resolve/main/generation_config.json'
    gen_cfg = fetch_json(gen_url)
    if gen_cfg:
        if 'thinking' in gen_cfg or 'reasoning' in gen_cfg:
            has_reasoning = True
            
    # 3. Model ID heuristics as fallback (e.g. if offline, rate-limited, or gated model)
    hf_id_lower = hf_id.lower()
    if 'r1' in hf_id_lower or 'qwq' in hf_id_lower or 'thinking' in hf_id_lower or 'qwen3' in hf_id_lower:
        has_reasoning = True

    known_tool_families = ['llama3', 'llama-3', 'qwen', 'hermes', 'mistral', 'mixtral', 'internlm', 'gemma']
    if not has_tools and any(f in hf_id_lower for f in known_tool_families):
        if any(x in hf_id_lower for x in ['instruct', 'chat', '-it', 'coder']):
            has_tools = True

    return has_reasoning, has_tools


def detect_reasoning_parser(hf_id: str):
    hf_id_lower = hf_id.lower()
    if 'deepseek' in hf_id_lower and 'r1' in hf_id_lower:
        return 'deepseek_r1'
    if 'qwq' in hf_id_lower:
        return 'deepseek_r1'
    if 'qwen3' in hf_id_lower:
        return 'qwen3'
    if 'gemma-4' in hf_id_lower or 'gemma4' in hf_id_lower:
        return 'gemma4'
    if 'granite' in hf_id_lower:
        return 'granite'
    if 'hunyuan' in hf_id_lower:
        return 'hunyuan_a13b'
    if 'minimax' in hf_id_lower:
        return 'minimax_m3'
    return 'deepseek_r1'


def detect_tool_parser(hf_id: str):
    hf_id_lower = hf_id.lower()
    if 'llama3' in hf_id_lower or 'llama-3' in hf_id_lower:
        return 'llama3_json'
    if 'hermes' in hf_id_lower:
        return 'hermes'
    if 'mistral' in hf_id_lower or 'mixtral' in hf_id_lower:
        return 'mistral'
    if 'internlm' in hf_id_lower:
        return 'internlm'
    if 'functiongemma' in hf_id_lower:
        return 'functiongemma'
    if 'gemma-4' in hf_id_lower or 'gemma4' in hf_id_lower:
        return 'gemma4'
    if 'qwen' in hf_id_lower:
        if 'qwen3' in hf_id_lower:
            if 'coder' in hf_id_lower:
                return 'qwen3_coder'
            return 'qwen3_xml'
        return 'hermes'
    return 'hermes'


def build_extra_args(hf_id: str, existing_argv: list) -> list:
    extra = []
    argv_lower = [x.lower() for x in existing_argv]

    has_reasoning, has_tools = check_features_from_hf(hf_id)

    # Check reasoning parser
    if '--reasoning-parser' not in argv_lower and has_reasoning:
        parser = detect_reasoning_parser(hf_id)
        if parser:
            extra.extend(['--reasoning-parser', parser])

    # Check tool calling
    if '--enable-auto-tool-choice' not in argv_lower and has_tools:
        tool_parser = detect_tool_parser(hf_id)
        if tool_parser:
            extra.extend(['--enable-auto-tool-choice', '--tool-call-parser', tool_parser])
                
    return extra


def find_recipe_exact(hf_id: str, recipe_db: list):
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
            base = t[len('base_model:'):]
            while ':' in base and base.count(':') > 1:
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


def trace_ancestry(hf_id: str, visited=None) -> list:
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
        deeper = trace_ancestry(base, visited)
        chain.extend(deeper)
        break
    
    return chain


def extract_version_token(name: str, family: str) -> str:
    """Extract major/minor version suffix following the last occurrence of the family name."""
    name = name.lower()
    idx = name.rfind(family.lower())
    if idx == -1:
        return ""
    sub = name[idx + len(family):]
    m = re.match(r'^[-_]?(\d+(?:\.\d+)?)', sub)
    if m:
        return m.group(1)
    m = re.match(r'^[-_]?(r\d+|v\d+)', sub)
    if m:
        return m.group(1)
    return "" 


def find_recipe_by_keywords(hf_id: str, recipe_db: list):
    """Level 3: Keyword matching in recipe DB with float size parsing and generation mismatch protection"""
    families = ['qwen', 'gemma', 'llama', 'mistral', 'phi', 'yi', 'mixtral', 'deepseek', 'internlm']
    found_family = None
    for f in families:
        if f in hf_id.lower():
            found_family = f
            break
    
    if not found_family:
        return None

    target_version = extract_version_token(hf_id, found_family)
    size_match = re.search(r'(\d+(?:\.\d+)?)[bB]', hf_id)
    target_size = float(size_match.group(1)) if size_match else None
    
    best_match = None
    min_diff = float('inf')
    
    for m in recipe_db:
        m_name = m.get('hf_id', '').lower()
        m_label = m.get('name', '').lower()
        
        if found_family not in m_name and found_family not in m_label:
            continue

        m_version = extract_version_token(m_name, found_family) or extract_version_token(m_label, found_family)
        if target_version != m_version:
            continue

        target_is_vl = 'vl' in hf_id.lower() or 'vision' in hf_id.lower()
        m_is_vl = 'vl' in m_name or 'vl' in m_label or 'vision' in m_name or 'vision' in m_label
        if target_is_vl != m_is_vl:
            continue
            
        m_size_match = re.search(r'(\d+(?:\.\d+)?)[bB]', m_name)
        if m_size_match:
            m_size = float(m_size_match.group(1))
            diff = abs(m_size - target_size) if target_size is not None else 9999
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
        'extra_args': build_extra_args(hf_id, argv),
        'note': f'Inferred from HF config.json (max_model_len: {max_model_len})',
        'inferred_max_model_len': max_model_len,
        'min_vllm_version': '0.0.0',
        'docker_image': 'vllm/vllm-openai:latest'
    }


def load_and_process_recipe(match: dict, hardware, target_hf_id: str) -> dict:
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
    
    rec_argv = recommended.get('argv', ['vllm', 'serve', match.get('hf_id', '')])
    result = {
        'found': True,
        'recipe_url': recipe_url,
        'min_vllm_version': recipe.get('model', {}).get('min_vllm_version', '0.0.0'),
        'docker_image': recipe.get('model', {}).get('docker_image', ''),
        'argv': rec_argv,
        'extra_args': build_extra_args(target_hf_id, rec_argv),
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
        fb_argv = ['vllm', 'serve', hf_model_id, '--tensor-parallel-size', '1']
        print(json.dumps({
            'found': False,
            'argv': fb_argv,
            'extra_args': build_extra_args(hf_model_id, fb_argv),
            'note': 'Could not reach recipes.vllm.ai',
            'min_vllm_version': '0.0.0',
            'docker_image': 'vllm/vllm-openai:latest'
        }))
        sys.exit(0)

    # --- LEVEL 1: Exact Match ---
    match = find_recipe_exact(hf_model_id, recipe_db)
    if match:
        result = load_and_process_recipe(match, hardware, hf_model_id)
        if result:
            print(json.dumps(result))
            sys.exit(0)
            
    # --- LEVEL 2: Base Model Inheritance (Recursive) ---
    ancestry_chain = trace_ancestry(hf_model_id)
    for ancestor in ancestry_chain:
        ancestor_match = find_recipe_exact(ancestor, recipe_db)
        if ancestor_match:
            result = load_and_process_recipe(ancestor_match, hardware, hf_model_id)
            if result:
                result['base_model_inherited'] = ancestor
                result['ancestry_chain'] = ancestry_chain
                result['note'] = f'Exact match not found for {hf_model_id}, inherited from ancestor: {ancestor} (chain: {" -> ".join(ancestry_chain)})'
                print(json.dumps(result))
                sys.exit(0)
                
    # --- LEVEL 3: Family Keyword Matching ---
    keyword_match = find_recipe_by_keywords(hf_model_id, recipe_db)
    if keyword_match:
        result = load_and_process_recipe(keyword_match, hardware, hf_model_id)
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
    fb_argv = ['vllm', 'serve', hf_model_id, '--tensor-parallel-size', '1']
    print(json.dumps({
        'found': False,
        'argv': fb_argv,
        'extra_args': build_extra_args(hf_model_id, fb_argv),
        'note': 'All fallback levels failed. Using ultimate fallback.',
        'min_vllm_version': '0.0.0',
        'docker_image': 'vllm/vllm-openai:latest'
    }))
    sys.exit(0)


if __name__ == '__main__':
    main()
