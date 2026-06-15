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


def calculate_precise_kv_per_token(model_info: dict) -> int:
    """
    1. Try to compute KV cache per token using metadata fields already present in model_info.
    2. Try to use transformers.AutoConfig if available.
    3. Fallback to 0.
    """
    layers = model_info.get('num_layers')
    hidden_size = model_info.get('hidden_size')
    num_heads = model_info.get('num_heads')
    kv_heads = model_info.get('num_key_value_heads')

    # Method 1: Use metadata fields parsed by BFF
    if layers and hidden_size and num_heads:
        if kv_heads is None:
            kv_heads = num_heads  # Fallback to MHA
        head_dim = hidden_size // num_heads
        # 2 (Key + Value) * 2 (Bytes for BF16) * layers * kv_heads * head_dim
        return 2 * 2 * layers * kv_heads * head_dim

    # Method 2: Use transformers AutoConfig
    source_uri = model_info.get('source_uri') or model_info.get('repo_name')
    if source_uri:
        try:
            from transformers import AutoConfig
            # Load config (local or remote metadata)
            config = AutoConfig.from_pretrained(source_uri, trust_remote_code=True)
            cfg_layers = getattr(config, "num_hidden_layers", 0)
            cfg_hidden_size = getattr(config, "hidden_size", 0)
            cfg_num_heads = getattr(config, "num_attention_heads", 0)
            cfg_kv_heads = getattr(config, "num_key_value_heads", cfg_num_heads)
            cfg_head_dim = getattr(config, "head_dim", 0) or (cfg_hidden_size // cfg_num_heads if cfg_num_heads else 0)
            
            if cfg_layers and cfg_kv_heads and cfg_head_dim:
                return 2 * 2 * cfg_layers * cfg_kv_heads * cfg_head_dim
        except Exception:
            pass

    return 0


def discover_local_hardware(vram_map: dict) -> dict:
    """
    Query local GPU and CPU/RAM using nvidia-smi and kubectl.
    """
    import subprocess
    result = {"gpu_product": "", "gpu_count": 0, "gpu_memory": "0Gi", "cpu_limit": "8", "mem_limit": "16Gi"}
    
    # 1. Get GPU Name
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=gpu_name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5, check=True
        )
        gpu_name = proc.stdout.strip().split('\n')[0]
        # Normalize (replace spaces with dashes, e.g. "NVIDIA GB10" -> "NVIDIA-GB10")
        result["gpu_product"] = "-".join(gpu_name.split())
    except Exception:
        pass

    # 2. Get GPU VRAM (prefer from vram_map using detected product name)
    vram_bytes = 0.0
    if result["gpu_product"] in vram_map:
        vram_bytes = vram_map[result["gpu_product"]]
        result["gpu_memory"] = f"{round(vram_bytes / 1024**3, 1)}Gi"
    
    # If not in vram_map, try nvidia-smi
    if vram_bytes == 0.0:
        try:
            proc = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5, check=True
            )
            total_vram_mib = int(proc.stdout.strip().split('\n')[0])
            result["gpu_memory"] = f"{round(total_vram_mib / 1024, 1)}Gi"
        except Exception:
            # Final fallback if both failed
            result["gpu_memory"] = "24.0Gi"

    # 3. Get allocatable resources from K8s node
    try:
        # Get first node name
        cmd_node = ["kubectl", "get", "nodes", "-o", "jsonpath={.items[0].metadata.name}"]
        node_name = subprocess.run(cmd_node, capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        
        # Get allocatable resources
        cmd_alloc = ["kubectl", "get", "node", node_name, "-o", "json"]
        alloc_data = json.loads(subprocess.run(cmd_alloc, capture_output=True, text=True, timeout=5, check=True).stdout)
        alloc = alloc_data.get("status", {}).get("allocatable", {})
        
        # GPU Count
        gpu_str = alloc.get("nvidia.com/gpu", "0")
        result["gpu_count"] = int(gpu_str)
        
        # CPU Limit (take half of allocatable)
        cpu_alloc = int(alloc.get("cpu", "8").replace('m', ''))
        # If it has millicores, handle it
        if "m" in alloc.get("cpu", ""):
            cpu_alloc = cpu_alloc // 1000
        result["cpu_limit"] = str(max(8, cpu_alloc))
        
        # Memory Limit (allocatable minus 4Gi, in Gi)
        mem_str = alloc.get("memory", "16384000Ki")
        if mem_str.endswith("Ki"):
            mem_bytes = int(mem_str[:-2]) * 1024
        elif mem_str.endswith("Mi"):
            mem_bytes = int(mem_str[:-2]) * 1024**2
        elif mem_str.endswith("Gi"):
            mem_bytes = int(mem_str[:-2]) * 1024**3
        else:
            mem_bytes = int(mem_str)
        mem_gb = mem_bytes / 1024**3
        result["mem_limit"] = f"{max(16, int(mem_gb - 4))}Gi"
    except Exception:
        # Fallbacks
        if not result["gpu_count"]:
            result["gpu_count"] = 1
            
    return result


def try_bootstrap_preset(req_vram: float, vram_map: dict) -> bool:
    import subprocess
    try:
        # Check kubectl
        subprocess.run(["kubectl", "version", "--client"], capture_output=True, check=True, timeout=5)
    except Exception:
        return False

    hw = discover_local_hardware(vram_map)
    if not hw["gpu_product"] or hw["gpu_count"] == 0:
        return False

    # Standard GPU memory parsed into bytes to check if it's feasible
    per_gpu_bytes = parse_vram_bytes(hw["gpu_memory"])
    
    # We dynamically create presets for different GPU counts (1x, 2x, 4x, 8x) up to allocatable gpu_count
    created_any = False
    for count in [1, 2, 4, 8]:
        if count > hw["gpu_count"]:
            break
        # Only create if the total VRAM is enough
        if per_gpu_bytes * count * 0.9 < req_vram:
            continue
            
        # Scale host CPU and memory requests/limits dynamically based on the ratio of requested GPUs to the node's total GPUs.
        # This keeps allocations proportional to the node's physical capabilities (e.g. modest on 18.6 GiB RAM nodes, but higher on 512 GiB RAM nodes).
        gpu_ratio = count / hw["gpu_count"]
        
        cpu_alloc = int(hw.get("cpu_limit", "8"))
        mem_alloc_gb = parse_vram_bytes(hw.get("mem_limit", "16Gi")) / (1024**3)
        
        preset_cpu_req = max(2, int(cpu_alloc * gpu_ratio * 0.5))
        preset_cpu_lim = max(4, int(cpu_alloc * gpu_ratio))
        preset_mem_req = f"{max(8, int(mem_alloc_gb * gpu_ratio * 0.5))}Gi"
        preset_mem_lim = f"{max(16, int(mem_alloc_gb * gpu_ratio))}Gi"

        preset_name = f"auto-preset-{hw['gpu_product'].lower()}-{count}x"
        
        yaml_content = f"""apiVersion: afsbox.asus.com/v1beta1
kind: ResourcePreset
metadata:
  name: {preset_name}
  namespace: afsbox-system
spec:
  enabled: true
  cpu:
    requests: "{preset_cpu_req}"
    limits: "{preset_cpu_lim}"
  memory:
    requests: "{preset_mem_req}"
    limits: "{preset_mem_lim}"
  gpuInfo:
    gpu: {count}
    product: "{hw['gpu_product']}"
    memory: "{hw['gpu_memory']}"
    resourceName: "nvidia.com/gpu"
"""
        try:
            subprocess.run(
                ["kubectl", "apply", "-f", "-"],
                input=yaml_content,
                capture_output=True,
                text=True,
                check=True,
                timeout=10
            )
            created_any = True
        except Exception:
            pass
            
    return created_any


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
        kv_per_token = calculate_precise_kv_per_token(model_info)
        if not kv_per_token:
            kv_per_token = estimate_kv_per_token(parameters)

    # Build GPU VRAM map from capability
    devices = cap_data.get('gpu', {}).get('devices', [])
    vram_map = {d['product']: parse_vram_bytes(d.get('memory', '0')) for d in devices}
    gpu_families = {d['product']: d.get('family', '').lower() for d in devices}

    max_num_seqs = max_num_seqs_for_slo(slo)

    # Calculate vGPU scale factor if Hami is enabled
    vgpu_scale = 1.0
    try:
        import subprocess
        # Detect physical GPUs
        proc = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True, text=True, timeout=5, check=True
        )
        physical_gpus = max(1, len(proc.stdout.strip().split('\n')))
        
        # Detect allocatable GPUs in K8s
        cmd_node = ["kubectl", "get", "nodes", "-o", "jsonpath={.items[0].metadata.name}"]
        node_name = subprocess.run(cmd_node, capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        cmd_alloc = ["kubectl", "get", "node", node_name, "-o", "jsonpath={.status.allocatable.nvidia\\.com/gpu}"]
        k8s_gpus = int(subprocess.run(cmd_alloc, capture_output=True, text=True, timeout=5, check=True).stdout.strip())
        
        if k8s_gpus > physical_gpus:
            vgpu_scale = physical_gpus / k8s_gpus
    except Exception:
        pass

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

        # Apply vGPU scale factor to get true VRAM allocatable per vGPU unit
        actual_per_gpu = per_gpu * vgpu_scale
        total_vram = actual_per_gpu * gpu_count * 0.9
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

    if not feasible:
        # Try to dynamically bootstrap compatible ResourcePreset
        if try_bootstrap_preset(req_vram, vram_map):
            import subprocess
            try:
                # Query fresh presets
                proc = subprocess.run(
                    ["kubectl", "get", "resourcepresets.afsbox.asus.com", "-n", "afsbox-system", "-o", "json"],
                    capture_output=True, text=True, timeout=5, check=True
                )
                fresh_presets_data = json.loads(proc.stdout)
                
                # Re-evaluate
                presets = fresh_presets_data.get('items', [])
                results = []
                for p in presets:
                    spec = p.get('spec', {})
                    if not spec.get('enabled', True):
                        continue
                    gpu_info  = spec.get('gpuInfo', {})
                    product   = gpu_info.get('product', '')
                    gpu_count = gpu_info.get('gpu', 1) or 1
                    per_gpu   = parse_vram_bytes(gpu_info.get('memory', '0'))
                    if per_gpu == 0:
                        continue
                        
                    # Apply vGPU scale factor to get true VRAM allocatable per vGPU unit
                    actual_per_gpu = per_gpu * vgpu_scale
                    total_vram = actual_per_gpu * gpu_count * 0.9
                    can_fit    = total_vram >= req_vram
                    avail_kv   = max(total_vram - req_vram, 0)

                    if kv_per_token > 0:
                        eff_ctx = min(int(avail_kv / (kv_per_token * max_num_seqs)), context_len)
                    else:
                        eff_ctx = context_len

                    results.append({
                        'name':              p.get('metadata', {}).get('name', ''),
                        'can_fit':           can_fit,
                        'gpu_count':         gpu_count,
                        'product':           product,
                        'family':            gpu_families.get(product, 'blackwell'),
                        'per_gpu_vram_bytes': per_gpu,
                        'effective_ctx':     eff_ctx,
                        'total_vram_gb':     round(total_vram / 1e9, 1),
                        'used_gb':           round(req_vram / 1e9, 1),
                    })
                
                feasible = [r for r in results if r['can_fit']]
            except Exception:
                pass

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

    # Get physical GPU count to handle vGPU/Hami sharing environments
    import subprocess
    physical_gpus = 1
    try:
        proc = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True, text=True, timeout=5, check=True
        )
        physical_gpus = len(proc.stdout.strip().split('\n'))
    except Exception:
        pass

    # Calculate tensor parallel size.
    # 1. We match tp_size to the preset's gpu_count to maximize resource utilization.
    # 2. We align to the nearest lower power of 2 for vLLM compatibility.
    # 3. TP size CANNOT exceed physical GPUs (vGPU slices cannot do TP within a single physical card).
    tp_size = 1
    while tp_size * 2 <= best['gpu_count']:
        tp_size *= 2
    tp_size = min(tp_size, physical_gpus)
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