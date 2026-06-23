#!/usr/bin/env python3
"""
resolve_model.py — Resolve user input model name to HuggingFace Model ID.

Usage:
  python3 resolve_model.py "<USER_INPUT>"

Output (stdout):
  JSON:
    { "success": true, "exact": true, "hf_model_id": "owner/name" }
    or:
    { "success": true, "exact": false, "candidates": ["owner/name1", "owner/name2", ...] }
    or:
    { "success": false, "error": "error message" }
"""
import sys
import json
import urllib.request
import urllib.parse
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

# Static aliases for instant offline mapping
ALIASES = {
    "gemma4 12b it": "google/gemma-4-12b-it",
    "gemma4 12b instruct": "google/gemma-4-12b-it",
    "gemma4 12b": "google/gemma-4-12b-it",
    "gemma4 27b": "google/gemma-4-27b-it",
    "gemma 4 12b": "google/gemma-4-12b-it",
    "gemma 4 27b": "google/gemma-4-27b-it",
    "gemma 2 9b": "google/gemma-2-9b-it",
    "gemma 2 27b": "google/gemma-2-27b-it",
    "llama 3.1 8b": "meta-llama/Llama-3.1-8B-Instruct",
    "llama 3.1 70b": "meta-llama/Llama-3.1-70B-Instruct",
    "llama 3.3 70b": "meta-llama/Llama-3.3-70B-Instruct",
    "llama 3 8b": "meta-llama/Meta-Llama-3-8B-Instruct",
    "llama3 8b": "meta-llama/Meta-Llama-3-8B-Instruct",
    "qwen3 8b": "Qwen/Qwen3-8B-Instruct",
    "qwen3 14b": "Qwen/Qwen3-14B-Instruct",
    "qwen3 32b": "Qwen/Qwen3-32B-Instruct",
    "qwen2.5 7b": "Qwen/Qwen2.5-7B-Instruct",
    "qwen2.5 14b": "Qwen/Qwen2.5-14B-Instruct",
    "qwen2.5 32b": "Qwen/Qwen2.5-32B-Instruct",
    "qwen2.5 72b": "Qwen/Qwen2.5-72B-Instruct",
    "qwen2.5 0.5b": "Qwen/Qwen2.5-0.5B-Instruct",
    "qwen2.5 1.5b": "Qwen/Qwen2.5-1.5B-Instruct",
    "deepseek r1 7b": "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
    "deepseek r1 8b": "deepseek-ai/DeepSeek-R1-Distill-Llama-8B",
    "deepseek r1 14b": "deepseek-ai/DeepSeek-R1-Distill-Qwen-14B",
    "deepseek r1 32b": "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B",
    "deepseek r1 70b": "deepseek-ai/DeepSeek-R1-Distill-Llama-70B",
    "deepseek r1": "deepseek-ai/DeepSeek-R1",
    "mistral 7b": "mistralai/Mistral-7B-Instruct-v0.3",
    "phi4": "microsoft/phi-4",
    "phi-4": "microsoft/phi-4",
}


def search_hf_hub(query: str, limit: int = 5) -> list:
    """Query Hugging Face API to find models matching the query string."""
    encoded_query = urllib.parse.quote(query)
    # Filter for text-generation/conversational models to reduce noise
    url = f"https://huggingface.co/api/models?search={encoded_query}&limit={limit}&sort=downloads"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "afsbox-deploy/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return [m.get("id") for m in data if m.get("id")]
    except Exception as e:
        sys.stderr.write(f"  ⚠️ HuggingFace search failed: {e}\n")
        sys.stderr.flush()
        return []


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"success": False, "error": "Usage: resolve_model.py <USER_INPUT>"}))
        sys.exit(2)

    user_input = sys.argv[1].strip()
    
    # 1. If it looks like a Hugging Face Repo ID already (contains slash)
    if "/" in user_input:
        print(json.dumps({"success": True, "exact": True, "hf_model_id": user_input}))
        sys.exit(0)

    # 2. Check local aliases (case insensitive, whitespace normalized)
    normalized = " ".join(user_input.lower().split())
    if normalized in ALIASES:
        print(json.dumps({"success": True, "exact": True, "hf_model_id": ALIASES[normalized]}))
        sys.exit(0)

    # 3. Fallback: Search on HuggingFace Hub
    sys.stderr.write(f"  🔍 '{user_input}' not found in local aliases. Searching on HuggingFace Hub...\n")
    sys.stderr.flush()
    candidates = search_hf_hub(user_input)
    
    if candidates:
        # Check if the query matches the first candidate exactly (case insensitive)
        first_candidate = candidates[0]
        if user_input.lower() == first_candidate.lower() or user_input.lower() == first_candidate.split("/")[-1].lower():
            print(json.dumps({"success": True, "exact": True, "hf_model_id": first_candidate}))
            sys.exit(0)
            
        print(json.dumps({"success": True, "exact": False, "candidates": candidates}))
        sys.exit(0)

    print(json.dumps({"success": False, "error": f"Could not resolve model name '{user_input}'"}))
    sys.exit(1)


if __name__ == "__main__":
    main()
