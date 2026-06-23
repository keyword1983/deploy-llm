#!/usr/bin/env python3
"""
find_engine.py — Query afsbox API and return the latest compatible vLLM engine.

Usage:
  python3 find_engine.py <API_BASE_URL> <ACCESS_TOKEN> [MIN_VERSION]

Output (stdout):
  JSON: { id, name, version, chartRef, image }
  or:   ERROR: <message>

Exit codes:
  0 = success
  1 = no vllm engine found or incompatible version
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
    # Parse from name
    m = re.search(r'v(\d+\.\d+\.\d+)', name, re.IGNORECASE)
    if m:
        return tuple(int(x) for x in m.group(1).split('.'))
    # Parse from id
    m = re.search(r'v(\d+\.\d+\.\d+)', engine_id, re.IGNORECASE)
    if m:
        return tuple(int(x) for x in m.group(1).split('.'))
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
                    # If preferred_image is in options, use it
                    if preferred_image in options:
                        return preferred_image
                    
                    # Try to find a matching option containing "vllm"
                    vllm_options = [opt for opt in options if "vllm" in opt.lower()]
                    if vllm_options:
                        # Prefer the one matching hardware (nvidia/vllm vs vllm-openai)
                        if is_arm or is_blackwell:
                            nvidia_opts = [opt for opt in vllm_options if "nvidia" in opt.lower()]
                            if nvidia_opts:
                                return nvidia_opts[0]
                        else:
                            openai_opts = [opt for opt in vllm_options if "openai" in opt.lower()]
                            if openai_opts:
                                return openai_opts[0]
                        return vllm_options[0]
                    
                    # Return the first option as fallback
                    return options[0]
                
                if default_val:
                    return default_val
    except Exception as e:
        sys.stderr.write(f"  ⚠️ Failed to query engine details from API: {e}\n")
        sys.stderr.flush()

    return preferred_image


def try_bootstrap_engine() -> bool:
    import subprocess
    try:
        # Check if kubectl is available
        subprocess.run(["kubectl", "version", "--client"], capture_output=True, check=True, timeout=5)
    except Exception:
        return False  # No kubectl, cannot auto-bootstrap

    # Auto detect image tag using default fallback since we don't have token during boot
    # but we can detect based on hardware
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
                if is_arm or is_blackwell:
                    image = f"{parts[0]}/nvidia/vllm:26.02-py3"
                else:
                    image = f"{parts[0]}/afsbox/vllm-openai:latest"
    except Exception:
        pass

    # Generate ModelEngine YAML
    yaml_content = f"""apiVersion: afsbox.asus.com/v1beta1
kind: ModelEngine
metadata:
  name: auto-vllm-engine
  namespace: afsbox-system
spec:
  displayName: "Auto vLLM Engine"
  engine:
    type: vllm
    servicePort: 8000
  modelType: llm
  chartRef:
    name: inference-engine
    namespace: afsbox-system
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
    except Exception:
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

    if not engines:
        # Attempt to auto bootstrap ModelEngine
        if try_bootstrap_engine():
            # Retry API call once to fetch the newly created engine
            try:
                import time
                time.sleep(2)
                with urllib.request.urlopen(req) as resp:
                    data = json.loads(resp.read())
                engines = [e for e in data.get('engines', []) if e.get('engine', {}).get('type') == 'vllm']
            except Exception:
                pass

    if not engines:
        print(
            'ERROR: no vllm engine template found.\n'
            'Possible causes:\n'
            '  1. Admin has not created a vLLM Engine Template yet.\n'
            '     Go to: Admin > Models > Templates > + New Template\n'
            '     Set engine.type = "vllm" and select the installed vLLM Helm chart.\n'
            '  2. The vLLM Helm chart has not been installed to the cluster yet.\n'
            '     Install the chart first, then create the Engine Template.'
        )
        sys.exit(1)

    # Sort by version descending, pick latest
    engines.sort(key=lambda e: parse_version(e), reverse=True)
    best = engines[0]
    best_ver = parse_version(best)

    # Check min version compatibility
    if best_ver < min_version:
        print(f'ERROR: latest engine version {version_str(best_ver)} < required {version_str(min_version)}')
        sys.exit(1)

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