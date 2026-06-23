# Recipe Fallback 流程

`find_recipe.py` 以 4 層遞迴 Fallback 策略為任意 HuggingFace 模型尋找 vLLM 部署參數，即使該模型不在 recipe DB 中也能推斷出可用的啟動參數。

## 流程總覽

```
find_recipe.py <HF_MODEL_ID> [HARDWARE]
│
├── 一次性載入: GET recipes.vllm.ai/models.json → recipe_db
│   └── 失敗 → Ultimate Fallback (vllm serve {model}, tp=1)
│
├── Level 1 ── 精確比對 (Exact Match)
│   └── ❌ 失敗 ↓
├── Level 2 ── 祖先繼承 (Base Model Inheritance)
│   └── ❌ 失敗 ↓
├── Level 3 ── 家族關鍵詞比對 (Family Keyword Matching)
│   └── ❌ 失敗 ↓
├── Level 4 ── 組態推斷 (Config Inference)
│   └── ❌ 失敗 ↓
└── Ultimate Fallback
```

**設計原則**：每一層都使用前層已載入的資料（recipe_db 只載入一次），越靠後的層越不需要外部服務，確保即使 recipe DB 離線也能產出結果。

---

## Level 1 — 精確比對 (Exact Match)

**做法**：在 recipe DB 中找 `hf_id` 完全吻合的記錄。

**匹配規則**：
```python
model.hf_id.lower() == hf_model_id.lower()
```

**資料來源**：`recipes.vllm.ai/models.json`（開頭已載入）

**命中後**：
1. 載入完整 recipe JSON → `recipes.vllm.ai/{recipe_path}`
2. 若有指定硬體參數，嘗試載入硬體專用配置 → `recipes.vllm.ai/{recipe_path}/hw/{hardware}.json`
3. 優先使用硬體配置的 argv，否則使用 recipe 的 `recommended_command`
4. 解析 `variants` 陣列判定 `has_fp8` / `has_nvfp4`

**輸出欄位**：
| 欄位 | 說明 |
|------|------|
| `found` | `true` |
| `recipe_url` | 完整 recipe JSON 網址 |
| `min_vllm_version` | 最低 vLLM 版本要求 |
| `argv` | vLLM 啟動參數（優先 hw 配置） |
| `variants` | 各精度變體的 VRAM 需求 |
| `has_fp8` | 是否有 FP8 變體 |
| `has_nvfp4` | 是否有 NVFP4 變體 |
| `hardware` | 使用的硬體配置名稱 |

**輸出範例**：
```json
{
  "found": true,
  "recipe_url": "https://recipes.vllm.ai/recipes/google/gemma-4-12b-it.json",
  "min_vllm_version": "0.7.0",
  "argv": ["vllm", "serve", "google/gemma-4-12b-it", "--max-model-len", "8192", ...],
  "variants": { "bf16": {"precision": "bf16", "vram_minimum_gb": 24}, "fp8": {"precision": "fp8", "vram_minimum_gb": 12} },
  "has_fp8": true,
  "has_nvfp4": false,
  "hardware": "h100"
}
```

---

## Level 2 — 祖先繼承 (Base Model Inheritance)

**做法**：當模型本身不在 recipe DB 中（例如 fine-tune 或 quantized 版本），透過 HF API 查詢模型的 `base_model` 並**遞迴往上追溯**到根基底模型，逐一在 recipe DB 中比對。

### 追溯流程

```
使用者輸入: org/custom-finetune-7b
    │
    ├── HF API 查詢 tags/cardData
    │   └── 找到 base_model: org/foundation-7b
    │
    ├── recipe DB 查 org/foundation-7b → ❌ 沒找到
    │
    ├── HF API 查詢 org/foundation-7b 的 base_model
    │   └── 找到 base_model: meta-llama/Llama-3.1-7B-Instruct
    │
    └── recipe DB 查 meta-llama/Llama-3.1-7B-Instruct → ✅ 找到！
        └── 用此 recipe，標注 base_model_inherited
```

### base_model 取得方式

兩層優先順序：

| 優先級 | 來源 | 格式 |
|--------|------|------|
| 1 | `model_info.tags[]` | `base_model:owner/name`，需剝離 `quantized:`、`finetuned:` 前綴 |
| 2 | `model_info.cardData.base_model` | 字串或字串陣列 |

### 防護機制

- `visited` set 防止無限迴圈（A → B → A）
- 只追溯第一條分支（不展开多父系）

### 命中後額外欄位

| 欄位 | 說明 |
|------|------|
| `base_model_inherited` | 實際匹配的祖先模型 ID |
| `ancestry_chain` | 完整追溯鏈：`[直接父系, 祖父系, ..., 根基底]` |
| `note` | 說明繼承來源 |

**輸出範例**：
```json
{
  "found": true,
  "base_model_inherited": "meta-llama/Llama-3.1-7B-Instruct",
  "ancestry_chain": ["org/foundation-7b", "meta-llama/Llama-3.1-7B-Instruct"],
  "note": "Exact match not found for org/custom-finetune-7b, inherited from ancestor: meta-llama/Llama-3.1-7B-Instruct (chain: org/foundation-7b -> meta-llama/Llama-3.1-7B-Instruct)",
  "min_vllm_version": "0.5.0",
  "argv": ["vllm", "serve", "meta-llama/Llama-3.1-7B-Instruct", ...]
}
```

---

## Level 3 — 家族關鍵詞比對 (Family Keyword Matching)

**做法**：從 HF model ID 中提取模型家族名稱和參數量，在 recipe DB 中找**同家族、參數量最接近**的 recipe。

### 比對邏輯

```
1. 提取家族 → 從 HF ID 比對已知家族列表
2. 提取參數量 → 正規表達式 (\d+)[bB]
3. 篩出同家族 recipe
4. 計算 |recipe_size - target_size|，取差距最小者
```

### 支援的模型家族

`qwen`、`gemma`、`llama`、`mistral`、`phi`、`yi`、`mixtral`、`deepseek`、`internlm`

### 匹配範例

| 輸入 | 家族 | 參數 | 最佳匹配 | 差距 |
|------|------|------|----------|------|
| `my-org/qwen2.5-custom-14b` | qwen | 14 | `Qwen/Qwen2.5-14B-Instruct` | 0 |
| `org/gemma-finetune-18b` | gemma | 18 | `google/gemma-4-27b-it` (而非 12b) | 9 vs 6 → 選 12b |
| `org/mystery-model` | — | — | 無家族 → 進入 Level 4 | — |

### 未找到家族

若 model ID 不包含任何已知家族關鍵詞，直接回傳 `None`，進入 Level 4。

### 命中後額外欄位

| 欄位 | 說明 |
|------|------|
| `note` | 說明為近似匹配，列出實際比對到的 HF ID |

---

## Level 4 — 組態推斷 (Config Inference)

**做法**：直接從 HuggingFace 下載模型的 `config.json`，提取架構參數，組建最小可用的 argv。

### 資料來源

```
https://huggingface.co/{hf_id}/resolve/main/config.json
```

### 提取項目

| 項目 | 路徑 | 預設值 |
|------|------|--------|
| `max_model_len` | `max_position_embeddings` 或 `text_config.max_position_embeddings` | 4096 |

> `text_config` 用於 Mamba 等嵌套組態結構。

### 輸出

```json
{
  "found": false,
  "argv": ["vllm", "serve", "org/model", "--max-model-len", "32768", "--tensor-parallel-size", "1"],
  "note": "Inferred from HF config.json (max_model_len: 32768)",
  "inferred_max_model_len": 32768
}
```

**注意**：Level 4 回傳 `found: false`，表示參數是從組態推斷而來，非來自 recipe DB。缺少 `min_vllm_version`、`variants`、`has_fp8` 等欄位。

---

## Ultimate Fallback

當 **Level 1-4 全部失敗**（含 HF config.json 也無法下載），或 **recipe DB 本身無法連線** 時的最後防線：

```json
{
  "found": false,
  "argv": ["vllm", "serve", "org/model", "--tensor-parallel-size", "1"],
  "note": "All fallback levels failed. Using ultimate fallback."
}
```

此 argv 只帶模型路徑和 tp=1，依賴 vLLM 自身的預設參數載入模型。

---

## 硬體參數處理

當呼叫 `find_recipe.py` 時指定第二個參數 `[HARDWARE]`（如 `h100`、`h200`、`gb10`）：

**適用層**：Level 1、Level 2、Level 3（這三層會進入 `load_and_process_recipe()`）

**處理流程**：
```
1. 載入標準 recipe: recipes.vllm.ai/{recipe_path}
2. 嘗試載入硬體配置: recipes.vllm.ai/{recipe_path}/hw/{hardware}.json
3. 若硬體配置存在 → 用它取代 recommended_command
4. 若不存在 → 使用 recipe 的 recommended_command
```

**不適用**：Level 4 和 Ultimate Fallback（這兩層不經過 `load_and_process_recipe()`）

---

## 各 Level 與 SKILL.md STEP 3 的關聯

SKILL.md STEP 3 解析 `find_recipe.py` 輸出後，記錄以下變數供後續步驟使用：

| 變數 | Level 1 | Level 2 | Level 3 | Level 4 | Ultimate |
|------|---------|---------|---------|---------|----------|
| `RECIPE_FOUND` | `true` | `true` | `true` | `false` | `false` |
| `RECIPE_MIN_VERSION` | ✅ | ✅ | ✅ | — | — |
| `RECIPE_ARGV` | ✅ | ✅ | ✅ | ✅ (推斷) | ✅ (最小) |
| `HAS_FP8` | ✅ | ✅ | ✅ | — | — |

**STEP 3 的行為差異**：

- `found=true`：正常流程，驗證 `ENGINE_VERSION >= RECIPE_MIN_VERSION`
- `found=false`：在進度中標示 `[no recipe]`，跳過版本驗證，使用 fallback argv

---

## 失敗模式與對策

| 失敗場景 | 發生的層 | 對策 |
|----------|---------|------|
| `recipes.vllm.ai` 離線 | 開頭載入 DB | Ultimate Fallback |
| 模型不在 DB 且無 base_model tag | Level 1 + 2 | 進入 Level 3 或 4 |
| 非已知家族（自研模型） | Level 1-3 | 進入 Level 4 |
| HF 模型頁面尚未建立 config.json | Level 4 | Ultimate Fallback |
| 多父系模型（僅追溯第一條） | Level 2 | 第一條祖先命中即可；若第一條也失敗，不追第二條 |
| HF API rate limit / 離線 | Level 2、4 | 跳過該層，進入下一層 |
