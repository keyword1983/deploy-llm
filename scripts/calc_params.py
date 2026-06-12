#!/usr/bin/env python3
"""
calc_params.py — Calculate optimal vLLM serving parameters based on model info,
                 hardware presets, and SLO priority.

Usage:
  python3 calc_params.py <MODEL_INFO_JSON> <PRESETS_JSON> <CAPABILITY_JSON>
                         [SLO_PRIORITY] [HAS_FP8]

  MODEL_INFO_JSON:   JSON string from poll_download.py READY output
  PRESETS_JSON:      JSON string from GET /api/v1/clusters/resourcepresets
  CAPABILITY_JSON:   JSON string from GET /api/v1/clusters/resourcepresets/capability
  SLO_PRIORITY:      latency | throughput | balanced (default: balanced)
  HAS_FP8:           true | false (default: false)

Output (stdout):
  JSON: {
    preset, gpu_count, product, tp_size,
    max_model_len, dtype, gpu_memory_utilization,
    max_num_seqs, vram_used_gb, vram_total_gb, vram_pct
  }
  or: JSON: { error: str, all: list }

Exit codes:
  0 = success
  1 = no feasible preset found
  2 = argument / parse error
"""
import sys
import json
import math


def parse_vram_bytes(s: str) -> float:
    """Parse memory string like '80 GiB', '40GiB', '80 GB' into bytes."""
    s = (s or '').strip()
    for suffix, mult in [('GiB', 1024**3), ('Gi', 1024**3), ('MiB', 1024**2), ('Mi', 1024**2), ('GB', 1e9), ('MB', 1e6)]:
        if s.endswith(suffix):
            return float(s[:-len(suffix)].strip()) * mult
    return 0.0


def estimate_kv_per_token(parameters: str) -> int:
    """Estimate kv_cache_memory_per_token from model parameter count string."""
    try:
        # Parse strings like "8B", "70B", "1.5B"
        s = parameters.upper().replace('B', '').strip()
        n = float(s)
    except (ValueError, AttributeError):
        n = 0
    if n <= 3:  return 256
    if n <= 8:  return 512
    if n <= 14: return 640
    if n <= 34: return 1024
    if n <= 72: return 2048
    return 4096


def max_num_seqs_for_slo(slo: str) -> int:
    return {'latency': 16, 'throughput': 256}.get(slo, 64)


def main():
    if len(sys.argv) < 4:
        print('ERROR: usage: calc_params.py <MODEL_INFO_JSON> <PRESETS_JSON> <CAPABILITY_JSON> [SLO] [HAS_FP8]')
        sys.exit(2)

    try:
        model_info   = json.loads(sys.argv[1])
        presets_data = json.loads(sys.argv[2])
        cap_data     = json.loads(sys.argv[3])
    except json.JSONDecodeError as e:
        print(f'ERROR: invalid JSON input: {e}')
        sys.exit(2)

    slo      = sys.argv[4].lower() if len(sys.argv) > 4 else 'balanced'
    has_fp8  = sys.argv[5].lower() == 'true' if len(sys.argv) > 5 else False

    # Extract model info
    req_vram      = model_info.get('required_min_gpu_memory') or 0
    kv_per_token  = model_info.get('kv_cache_memory_per_token') or 0
    context_len   = model_info.get('context_length') or 131072
    parameters    = model_info.get('parameters') or '0B'
    torch_dtype   = model_info.get('torch_dtype') or 'bfloat16'

    # Fallback kv estimate if not available
    if not kv_per_token:
        kv_per_token = estimate_kv_per_token(parameters)

    # Build GPU VRAM map from capability
    devices = cap_data.get('gpu', {}).get('devices', [])
    vram_map = {d['product']: parse_vram_bytes(d.get('memory', '0')) for d in devices}
    gpu_families = {d['product']: d.get('family', '').lower() for d in devices}

    max_num_seqs = max_num_seqs_for_slo(slo)

    # Evaluate each enabled preset
    presets = presets_data.get('resourcepresets', [])
    results = []
    for p in presets:
        if not p.get('enabled', True):
            continue
        product   = p.get('gpu_product', '')
        gpu_count = p.get('gpu', 1) or 1
        per_gpu   = vram_map.get(product, 0)
        if per_gpu == 0:
            continue  # Unknown GPU, skip

        total_vram = per_gpu * gpu_count * 0.9
        can_fit    = total_vram >= req_vram
        avail_kv   = max(total_vram - req_vram, 0)

        if kv_per_token > 0:
            eff_ctx = min(int(avail_kv / (kv_per_token * max_num_seqs)), context_len)
        else:
            eff_ctx = context_len

        results.append({
            'name':              p['name'],
            'can_fit':           can_fit,
            'gpu_count':         gpu_count,
            'product':           product,
            'family':            gpu_families.get(product, ''),
            'per_gpu_vram_bytes': per_gpu,
            'effective_ctx':     eff_ctx,
            'total_vram_gb':     round(total_vram / 1e9, 1),
            'used_gb':           round(req_vram / 1e9, 1),
        })

    feasible = [r for r in results if r['can_fit']]

    if slo == 'latency':
        # Prefer fewest GPUs with sufficient context
        min_ctx = min(8192, context_len)
        feasible.sort(key=lambda x: (x['gpu_count'], -(x['effective_ctx'] >= min_ctx), -x['effective_ctx']))
    elif slo == 'throughput':
        # Prefer largest effective context
        feasible.sort(key=lambda x: (-x['effective_ctx'], x['gpu_count']))
    else:  # balanced
        feasible.sort(key=lambda x: (x['gpu_count'], -x['effective_ctx']))

    if not feasible:
        print(json.dumps({'error': 'no feasible preset found', 'all': results}))
        sys.exit(1)

    best = feasible[0]
    per_gpu = best['per_gpu_vram_bytes']

    # Calculate tensor parallel size (minimum GPUs needed for weights)
    tp_size = math.ceil(req_vram / per_gpu) if per_gpu > 0 else 1
    tp_size = max(tp_size, 1)

    # max_model_len cap by SLO
    ctx_cap = {'latency': 8192, 'throughput': 32768}.get(slo, 16384)
    max_model_len = min(best['effective_ctx'], ctx_cap)
    max_model_len = max(max_model_len, 2048)  # minimum reasonable context

    # Determine dtype
    family = best['family']
    use_fp8 = has_fp8 and any(f in family for f in ['hopper', 'blackwell'])
    dtype = 'fp8' if use_fp8 else 'bfloat16'

    # VRAM utilisation %
    pct = round(req_vram / (best['total_vram_gb'] * 1e9) * 100, 1) if best['total_vram_gb'] > 0 else 0

    print(json.dumps({
        'preset':                 best['name'],
        'gpu_count':              best['gpu_count'],
        'product':                best['product'],
        'family':                 family,
        'tp_size':                tp_size,
        'max_model_len':          max_model_len,
        'dtype':                  dtype,
        'gpu_memory_utilization': 0.9,
        'max_num_seqs':           max_num_seqs,
        'vram_used_gb':           best['used_gb'],
        'vram_total_gb':          best['total_vram_gb'],
        'vram_pct':               pct,
    }))
    sys.exit(0)


if __name__ == '__main__':
    main()