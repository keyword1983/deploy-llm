# deploy-llm Skill

> 一鍵將任意 HuggingFace LLM 模型部署到 **afsbox** 平台的 AI Agent Skill。

## 概覽

`deploy-llm` 是一個為 ASUS OCIS AFSBOX 平台 設計的 Skill，只需使用者說出模型名稱（如「幫我部署 Qwen3 7B」、「我想跑 DeepSeek R1」），Agent 即可全自動完成從環境探索、模型下載到服務上線的完整流程。

---

## 技術特點

### 🤖 智慧模型解析
- **別名對照 + HuggingFace 語意搜尋**：支援模糊輸入（如 `gemma 2 9b` → `google/gemma-2-9b-it`），自動語意篩選官方 Instruct 版本
- **模糊推薦引擎**：結合 LMSYS Arena Elo 排行榜與動態版本代數規則，依 task（coding / reasoning / general）與可用 VRAM 推薦最優開源模型

### ⚙️ 全自動環境探索
- **`bootstrap_env.py`** 一鍵取得所有環境變數：
  - `ACCESS_TOKEN`：環境變數 → K8s Secret + Keycloak（自動 DNS + port fallback）
  - `API_BASE_URL`：環境變數 → K8s 內網 FQDN → JWT `iss` 推導 → kubectl Ingress
  - `PROJECT_ID`：環境變數 → API 查詢第一個 Ready 專案的 `namespace` 欄位
- **Token 自動刷新**：輪詢期間偵測 HTTP 401 自動換取新 Token（`token_utils.py`）

### 🔧 自我修復機制（Self-healing）
- **Engine 自動建立**：找不到 vLLM Engine 時，自動透過 kubectl 建立 `auto-vllm-engine`，優先選取 NVIDIA NGC 映像（適配 Blackwell GPU）
- **Preset 自動建立**：找不到可用 ResourcePreset 時，自動偵測主機 GPU 規格並動態建立，並處理單節點 vGPU 排程限制（避免 `NodeInsufficientDevice`）

### 📐 動態 LLM 部署參數計算（`calc_params.py`）

這是 Skill 最核心的推理模組，輸入三份資料後自動算出最佳部署參數：

| 輸入 | 來源 |
|------|------|
| `model_info` | `poll_download.py` 回傳（含 `parameters`、`context_length`、`required_min_gpu_memory`、`kv_cache_memory_per_token`） |
| `presets` | `GET /api/v1/clusters/resourcepresets`（叢集已定義的 GPU 規格） |
| `capability` | `GET /api/v1/clusters/resourcepresets/capability`（各 GPU 型號的實際 VRAM） |

#### KV Cache 記憶體精確計算（三層 Fallback）

```
1. 優先從 model_info 中的 metadata 直接計算：
   kv_per_token = 2 × 2 bytes × num_layers × num_kv_heads × head_dim
   （2 代表 K+V，2 bytes 代表 BF16）

2. 若 metadata 缺失，使用 transformers.AutoConfig 從 HF 拉取 config：
   AutoConfig.from_pretrained(repo_id) → 同公式計算

3. 最終 Fallback：依參數量估算經驗值
   ≤3B  → 256 bytes/token
   ≤8B  → 512 bytes/token
   ≤14B → 640 bytes/token
   ≤34B → 1024 bytes/token
   ≤72B → 2048 bytes/token
   >72B → 4096 bytes/token
```

#### VRAM 需求計算與 Preset 篩選

```
模型權重 VRAM = required_min_gpu_memory（由 afsbox API 提供）

對每個 ResourcePreset 計算：
  total_vram = per_gpu_vram × gpu_count × 0.9   （保留 10% 系統開銷）
  can_fit    = total_vram ≥ req_vram

剩餘可用 KV Cache：
  avail_kv = total_vram − req_vram
  eff_ctx  = min( avail_kv / (kv_per_token × 2), model_context_len )
```

> `× 2` 是保守估算：假設同時有 2 個最大長度序列佔用 KV Cache，確保不 OOM。

#### Tensor Parallel Size（`tp_size`）決策

```
tp_size 規則（按順序套用）：
  1. 初始 = GPU 數向下取最大 2 的冪次（如 gpu_count=3 → tp_size=2）
  2. 不可超過實體 GPU 數（nvidia-smi -L 偵測），避免 vGPU 跨卡 TP 失敗
  3. 最小值 = 1
```

#### dtype / 量化格式決策

```
優先順序：NvFP4 > FP8 > bfloat16

- has_nvfp4=true + GPU family = blackwell → dtype = nvfp4
- has_fp8=true  + GPU family in [hopper, blackwell] → dtype = fp8  （~1.8x 速度）
- 其他 → dtype = bfloat16（最高精度基準）
```

#### `max_num_seqs` 與 SLO 模式

```
latency    模式 → max_num_seqs = 16   （低延遲優先，少量並發）
throughput 模式 → max_num_seqs = 256  （吞吐優先，大量並發）
balanced   模式 → max_num_seqs = 64   （預設，折衷）
```

Preset 排序策略亦依 SLO 調整：
- `latency`：優先選滿足 min_ctx=8192 的 Preset，再選最少 GPU slice
- `throughput`：優先最大 effective_ctx，次要最少 slice
- `balanced`：優先最少 slice（節省資源），次要最大 context

#### vGPU（Hami）環境特殊處理

```
偵測 vGPU：
  vgpu_scale = physical_gpus / k8s_allocatable_gpus

若 vgpu_scale < 1.0（即共享模式）：
  actual_per_gpu_vram = per_gpu_vram × vgpu_scale
  從 K8s Preset 的 nvidia.com/gpumem 取得精確配額（優先）
  gpu_memory_limit_mib 與 gpu_cores_limit 一併帶入 answers
```

### 🗂️ 四層 Recipe Fallback
1. **精確比對**：模型 ID 完全匹配
2. **祖先繼承**：同家族但不同大小
3. **關鍵詞比對**：架構關鍵詞模糊匹配（含浮點數版本號修正，`0.5B` ≠ `5B`）
4. **Config 推斷**：從模型 `config.json` 直接推算 dtype 與 context

### 🐳 多引擎支援

| 推理引擎 | 模型格式 | 適用場景 |
|----------|----------|----------|
| **vLLM** | HuggingFace（BF16/FP8/AWQ/GPTQ）| 生產 API、高併發、OpenAI 相容 |
| **llama.cpp** | GGUF（Q4/Q5/Q6/Q8）| 量化部署、資源受限節點 |

---

## Skill 架構

```
deploy-llm/
├── SKILL.md              # Agent 執行指令（含完整 7 步驟流程）
├── README.md             # 本文件
├── TODO.md               # 改進追蹤清單
├── references/           # 補充參考文件
└── scripts/
    ├── bootstrap_env.py  # 環境自動探索
    ├── resolve_model.py  # 模型 ID 解析（別名 + HF 搜尋）
    ├── recommend_model.py # 智慧模型推薦
    ├── find_engine.py    # vLLM Engine 查詢 + 自動建立
    ├── find_recipe.py    # Recipe 查詢（四層 Fallback）
    ├── check_repo.py     # ModelRepository 存在性檢查
    ├── create_repo.py    # 建立 ModelRepository 並觸發下載
    ├── poll_download.py  # 輪詢模型下載狀態
    ├── calc_params.py    # 最佳部署參數計算（核心模組）
    ├── poll_serving.py   # 輪詢服務就緒狀態（含健康檢查）
    ├── delete_serving.py # 刪除 ModelServing
    ├── get_token.py      # 備用 Token 換取工具
    └── token_utils.py    # Token 共用工具（自動刷新 / 401 重試）
```

---

## 完整部署流程

```
使用者輸入 "部署 Qwen3 7B"
        │
[ENV]   bootstrap_env.py ──── 自動取得 API_BASE_URL / TOKEN / PROJECT_ID
        │
[1/6]   resolve_model.py ──── "Qwen3 7B" → "Qwen/Qwen3-7B-Instruct"
        │
[2/6]   find_engine.py ─────  選擇相容 vLLM Engine（優先 NGC 映像）
        │
[3/6]   find_recipe.py ─────  查詢 Recipe（argv / dtype / min_vllm_ver）
        │
[4/6]   check_repo → create_repo → poll_download ── 下載並等待 Ready
        │
[5/6]   calc_params.py ─────  KV Cache 計算 → Preset 篩選 → tp_size / dtype / max_num_seqs
        │
[6/6]   Preview YAML → 使用者確認 → POST /servings
        │
[7/7]   poll_serving.py ────  等待就緒 + /v1/models 健康檢查
        │
        ✅ 輸出 curl 測試指令（含 AI Gateway Host header）
```

---

## 平台相依性

| 元件 | 說明 |
|------|------|
| **afsbox Platform** | ASUS AI 平台，提供 ModelEngine / ModelRepository / ModelServing CRD |
| **Kubernetes / K3s** | 叢集排程，支援 NVIDIA vGPU（Hami）共享 |
| **Flux CD** | Helm Release 管理 |
| **Keycloak** | 身份認證與 Token 發放 |
| **HuggingFace Hub** | 模型下載來源（支援 gated / private 模型） |

---

## 常見用法

```
# 部署指定模型
"幫我部署 Llama 3.1 8B"
"deploy gemma4 12b"
"我想跑 DeepSeek R1"

# 模糊需求（自動推薦）
"推薦一個適合目前資源、寫 code 最好的模型"
"幫我裝一個最近很紅的推理模型，40GB 以內"

# 刪除服務
"幫我把 qwen-qwen3-7b-serving 下線"
```

---

## 授權

Internal use — ASUS OCIS
