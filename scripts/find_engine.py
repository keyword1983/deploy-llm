#!/usr/bin/env python3
"""
find_engine.py — Query afsbox API and return/bootstrap the compatible vLLM engine.

Usage:
  python3 find_engine.py <API_BASE_URL> <ACCESS_TOKEN> [MIN_VERSION]

Output (stdout):
  JSON: { id, name, version, chartRef, image }
  or:   ERROR: <message>

Exit codes:
  0 = success
  1 = no compatible vllm engine found and bootstrap failed
  2 = API request failed
"""
import sys
import os
import json
import re
import urllib.request
import urllib.error
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from token_utils import refresh_token


def parse_version(engine: dict) -> tuple:
    name = engine.get('name', '')
    engine_id = engine.get('id', '')
    
    # Check if this is an NVIDIA Blackwell optimized engine
    if 'nvidia' in engine_id.lower() or 'nvidia' in name.lower():
        return (99, 0, 0)
        
    # 1. Parse from name (extract version like '0.6.0' or 'v0.7.1')
    m = re.search(r'(\d+\.\d+\.\d+)', name, re.IGNORECASE)
    if m:
        return tuple(int(x) for x in m.group(1).split('.'))
        
    # 2. Parse from id (supports both dot and dash formats, e.g. 'v0-6-0' or 'v0.6.0')
    m = re.search(r'v(\d+)[-.](\d+)[-.](\d+)', engine_id, re.IGNORECASE)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        
    # 3. Fallback: try looking for any three numbers separated by dash/dot in id
    m = re.search(r'(\d+)[-.](\d+)[-.](\d+)', engine_id)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        
    return (0, 0, 0)


def version_str(t: tuple) -> str:
    return '.'.join(str(x) for x in t)


def detect_image_source(engine_id: str, api_base: str, token: str) -> str:
    # 1. Detect hardware characteristics
    import platform
    import subprocess
    
    is_arm = platform.machine().lower() in ["aarch64", "arm64"]
    is_blackwell = False
    
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=gpu_name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5
        )
        if proc.returncode == 0:
            gpu_name = proc.stdout.strip().lower()
            if any(x in gpu_name for x in ["gb10", "gb200", "b200", "blackwell"]):
                is_blackwell = True
    except Exception:
        pass

    # Determine base image based on hardware configuration
    if is_arm or is_blackwell:
        preferred_image = "nvcr.io/nvidia/vllm:26.02-py3"
    else:
        preferred_image = "vllm/vllm-openai:latest"

    # Try to discover local private registry from existing controllers (offline mode)
    private_registry = ""
    try:
        cmd = ["kubectl", "get", "deployment", "afsbox-controller", "-n", "afsbox-system", "-o", "jsonpath={.spec.template.spec.containers[0].image}"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            image_path = proc.stdout.strip()
            parts = image_path.split('/')
            if len(parts) > 1 and ('.' in parts[0] or ':' in parts[0]):
                private_registry = parts[0]
    except Exception:
        pass

    # Reconstruct preferred image with private registry if available
    if private_registry:
        if "nvcr.io/" in preferred_image:
            preferred_image = f"{private_registry}/nvidia/vllm:26.02-py3"
        else:
            preferred_image = f"{private_registry}/afsbox/vllm-openai:latest"

    # 2. Query Engine details from API to see allowed image options
    engine_url = f"{api_base}/api/v1/models/engines/{engine_id}"
    req = urllib.request.Request(engine_url, headers={
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    })
    
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            engine_data = json.loads(resp.read())
            
            # Find values.image question
            questions = engine_data.get("additionalQuestions", [])
            image_q = next((q for q in questions if q.get("variable") == "values.image"), None)
            if image_q:
                options = image_q.get("options", [])
                default_val = image_q.get("default", "")
                
                if options:
                    if preferred_image in options:
                        return preferred_image
                    
                    vllm_options = [opt for opt in options if "vllm" in opt.lower()]
                    if vllm_options:
                        if is_arm or is_blackwell:
                            nvidia_opts = [opt for opt in vllm_options if "nvidia" in opt.lower()]
                            if nvidia_opts:
                                return nvidia_opts[0]
                        else:
                            openai_opts = [opt for opt in vllm_options if "openai" in opt.lower()]
                            if openai_opts:
                                return openai_opts[0]
                        return vllm_options[0]
                    return options[0]
                
                if default_val:
                    return default_val
    except Exception as e:
        sys.stderr.write(f"  ⚠️ Failed to query engine details from API: {e}\n")
        sys.stderr.flush()

    return preferred_image


def try_bootstrap_engine(version_tuple: tuple, chart_name: str, chart_namespace: str) -> bool:
    import subprocess
    try:
        # Check if kubectl is available
        subprocess.run(["kubectl", "version", "--client"], capture_output=True, check=True, timeout=5)
    except Exception:
        return False  # No kubectl, cannot auto-bootstrap

    version_str_val = ".".join(str(x) for x in version_tuple) if version_tuple != (0, 0, 0) else ""

    # 1. Determine image tag based on hardware and version
    import platform
    is_arm = platform.machine().lower() in ["aarch64", "arm64"]
    is_blackwell = False
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=gpu_name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5
        )
        if proc.returncode == 0:
            gpu_name = proc.stdout.strip().lower()
            if any(x in gpu_name for x in ["gb10", "gb200", "b200", "blackwell"]):
                is_blackwell = True
    except Exception:
        pass

    # Build image tag based on required version
    if version_str_val:
        image = f"vllm/vllm-openai:v{version_str_val}"
    else:
        if is_arm or is_blackwell:
            image = "nvcr.io/nvidia/vllm:26.02-py3"
        else:
            image = "vllm/vllm-openai:latest"

    # Reconstruct with private registry if available
    try:
        cmd = ["kubectl", "get", "deployment", "afsbox-controller", "-n", "afsbox-system", "-o", "jsonpath={.spec.template.spec.containers[0].image}"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            image_path = proc.stdout.strip()
            parts = image_path.split('/')
            if len(parts) > 1 and ('.' in parts[0] or ':' in parts[0]):
                if version_str_val:
                    image = f"{parts[0]}/afsbox/vllm-openai:v{version_str_val}"
                else:
                    if is_arm or is_blackwell:
                        image = f"{parts[0]}/nvidia/vllm:26.02-py3"
                    else:
                        image = f"{parts[0]}/afsbox/vllm-openai:latest"
    except Exception:
        pass

    # Use DNS safe name: e.g. auto-vllm-engine-v0-7-1 or auto-vllm-engine-latest
    suffix = f"v{version_str_val.replace('.', '-')}" if version_str_val else "latest"
    engine_name = f"auto-vllm-engine-{suffix}"
    display_name = f"Auto vLLM Engine {version_str_val}" if version_str_val else "Auto vLLM Engine Latest"

    # Generate ModelEngine YAML
    yaml_content = f"""apiVersion: afsbox.asus.com/v1beta1
kind: ModelEngine
metadata:
  name: {engine_name}
  namespace: afsbox-system
spec:
  displayName: "{display_name}"
  engine:
    type: vllm
    servicePort: 8000
  modelType: llm
  chartRef:
    name: {chart_name}
    namespace: {chart_namespace}
  values:
    image: "{image}"
    shm:
      enabled: true
      size: 12Gi
  additionalQuestions:
    - variable: values.image
      type: reference_image
      label: "Container Image"
      description: "選擇 engine 的容器映像"
      group: "BasicSetting"
      required: true
      editable: true
      options:
        - "{image}"
    - variable: tensorParallelSize
      type: integer
      label: "Tensor Parallel Size"
      required: false
      default: 1
    - variable: maxModelLen
      type: integer
      label: "Max Model Length"
      required: false
      default: 2048
    - variable: gpuMemoryUtilization
      type: float
      label: "GPU Memory Utilization"
      required: false
      default: 0.9
    - variable: maxNumSeqs
      type: integer
      label: "Max Num Seqs"
      required: false
      default: 64
    - variable: dtype
      type: string
      label: "Data Type"
      required: false
      default: "bfloat16"
"""
    try:
        # Apply YAML via kubectl
        subprocess.run(
            ["kubectl", "apply", "-f", "-"],
            input=yaml_content,
            capture_output=True,
            text=True,
            check=True,
            timeout=10
        )
        return True
    except Exception as e:
        sys.stderr.write(f"  ⚠️ Failed to apply ModelEngine via kubectl: {e}\n")
        sys.stderr.flush()
        return False


def main():
    if len(sys.argv) < 3:
        print('ERROR: usage: find_engine.py <API_BASE_URL> <ACCESS_TOKEN> [MIN_VERSION]')
        sys.exit(1)

    api_base = sys.argv[1].rstrip('/')
    token = sys.argv[2]
    min_version = tuple(int(x) for x in sys.argv[3].split('.')) if len(sys.argv) > 3 else (0, 0, 0)

    url = f'{api_base}/api/v1/models/engines'
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
                # Remove old header first by rebuilding
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

    # Filter vllm engines
    engines = [e for e in data.get('engines', []) if e.get('engine', {}).get('type') == 'vllm']

    # Sort by version descending
    engines.sort(key=lambda e: parse_version(e), reverse=True)

    # Check if we have a compatible engine
    compatible_engine = None
    if engines:
        best = engines[0]
        best_ver = parse_version(best)
        if best_ver >= min_version:
            compatible_engine = best

    # If no compatible engine exists, try to bootstrap a new one with the required version
    if not compatible_engine:
        sys.stderr.write(f"  ⏳ No compatible engine version >= {version_str(min_version)} found. Attempting to bootstrap a new ModelEngine...\n")
        sys.stderr.flush()

        # Copy chartRef from existing engines if available
        chart_name = "inference-engine"
        chart_namespace = "afsbox-system"
        for e in engines:
            ref = e.get("chartRef", {})
            if ref.get("name"):
                chart_name = ref.get("name")
                chart_namespace = ref.get("namespace") or chart_namespace
                break

        if try_bootstrap_engine(min_version, chart_name, chart_namespace):
            # Retry API call to fetch the newly created engine (polling up to 5 times, 10s total)
            for attempt in range(5):
                try:
                    import time
                    time.sleep(2)
                    sys.stderr.write(f"  ⏳ Retrying API check (attempt {attempt + 1}/5)...\n")
                    sys.stderr.flush()
                    with urllib.request.urlopen(req) as resp:
                        data = json.loads(resp.read())
                    engines = [e for e in data.get('engines', []) if e.get('engine', {}).get('type') == 'vllm']
                    engines.sort(key=lambda e: parse_version(e), reverse=True)
                    if engines:
                        best = engines[0]
                        best_ver = parse_version(best)
                        if best_ver >= min_version:
                            compatible_engine = best
                            break
                except Exception:
                    pass

    if not compatible_engine:
        print(f'ERROR: latest engine version {version_str(best_ver) if engines else "none"} < required {version_str(min_version)} and auto-bootstrap failed')
        sys.exit(1)

    best = compatible_engine
    best_ver = parse_version(best)

    # Dynamically detect optimal image tag based on allowed options in this specific engine
    image_tag = detect_image_source(best['id'], api_base, token)

    result = {
        'id': best['id'],
        'name': best['name'],
        'version': version_str(best_ver),
        'chartRef': best.get('chartRef', {}),
        'servicePort': best.get('engine', {}).get('servicePort', 8000),
        'image': image_tag,
        'all_versions': [version_str(parse_version(e)) for e in engines],
    }
    print(json.dumps(result))
    sys.exit(0)


if __name__ == '__main__':
    main()