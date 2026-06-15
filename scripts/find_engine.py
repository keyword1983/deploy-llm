#!/usr/bin/env python3
"""
find_engine.py — Query afsbox API and return the latest compatible vLLM engine.

Usage:
  python3 find_engine.py <API_BASE_URL> <ACCESS_TOKEN> [MIN_VERSION]

Output (stdout):
  JSON: { id, name, version, chartRef }
  or:   ERROR: <message>

Exit codes:
  0 = success
  1 = no vllm engine found or incompatible version
  2 = API request failed
"""
import sys
import json
import re
import urllib.request
import urllib.error


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


def detect_image_source() -> str:
    # Track 1: Outbound TCP socket test to Nvidia NGC
    import socket
    try:
        socket.setdefaulttimeout(3)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("nvcr.io", 443))
        s.close()
        return "nvcr.io/nvidia/vllm:26.02-py3"
    except Exception:
        pass

    # Track 2: Try to discover local private registry from existing controllers
    import subprocess
    try:
        cmd = ["kubectl", "get", "deployment", "afsbox-controller", "-n", "afsbox-system", "-o", "jsonpath={.spec.template.spec.containers[0].image}"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            image_path = proc.stdout.strip()
            parts = image_path.split('/')
            if len(parts) > 1 and ('.' in parts[0] or ':' in parts[0]):
                # Reconstruct image path with local private registry prefix
                return f"{parts[0]}/nvidia/vllm:26.02-py3"
    except Exception:
        pass

    # Fallback to Nvidia NGC
    return "nvcr.io/nvidia/vllm:26.02-py3"


def try_bootstrap_engine() -> bool:
    import subprocess
    try:
        # Check if kubectl is available
        subprocess.run(["kubectl", "version", "--client"], capture_output=True, check=True, timeout=5)
    except Exception:
        return False  # No kubectl, cannot auto-bootstrap

    # Auto detect image
    image = detect_image_source()

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

    result = {
        'id': best['id'],
        'name': best['name'],
        'version': version_str(best_ver),
        'chartRef': best.get('chartRef', {}),
        'servicePort': best.get('engine', {}).get('servicePort', 8000),
        'all_versions': [version_str(parse_version(e)) for e in engines],
    }
    print(json.dumps(result))
    sys.exit(0)


if __name__ == '__main__':
    main()