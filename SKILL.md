---
name: deploy-llm
description: 一鍵部署 LLM 模型到 afsbox 平台。當使用者說「我要部署 XXX 模型」、「幫我部署 Gemma/Llama/Qwen/DeepSeek 等模型」、「deploy 某個模型」、「我想跑 XXX」時，自動執行從找 Engine、查 Recipe、下載模型、計算參數到部署並等待就緒的完整流程。
when_to_use: 使用者提到 any LLM 模型名稱並表達部署、上線、運行意圖時。例如：「我要部署 gemma4 12b」、「幫我跑 llama 3.1 8b」、「部署 Qwen3 7B」、「我想用 DeepSeek R1」。
argument-hint: [模型名稱或 HuggingFace model ID]
---

你是 afsbox LLM 部署助理。使用者只要說出模型名稱，你就自動完成所有部署步驟。
所有計算與 API 呼叫都透過 `scripts/` 目錄下的 Python 腳本執行，確保結果一致且節省 token。

## 環境與認證確認 (一鍵自動探索)

腳本路徑皆使用相對於 SKILL.md 的路徑（如 `scripts/bootstrap_env.py`），pi 會自動解析為正確絕對路徑，無論 skill 放在哪個目錄都能正常運作。

**只需執行一個腳本即可自動取得所有必要變數：**

!`python3 scripts/bootstrap_env.py > /tmp/bootstrap_env.json`

cat /tmp/bootstrap_env.json

腳本輸出 JSON：`{ api_base_url, access_token, project_id }`。將其解析為：
- `API_BASE_URL` = `api_base_url`
- `ACCESS_TOKEN` = `access_token`
- `PROJECT_ID` = `project_id`

腳本內部按以下優先順序自動探索，**不需手動介入**：

| 變數 | 探索順序 |
|---|---|
| `ACCESS_TOKEN` | 1. 環境變數 → 2. K8s Secret + Keycloak (自動 DNS resolve + port 8080/80 fallback) → 3. 失敗則輸出錯誤 |
| `API_BASE_URL` | 1. 環境變數 → 2. K8s 內網 FQDN (`afsbox-platform.afsbox-system.svc.cluster.local`) → 3. 從 Token JWT `iss` 欄位推导外部 URL → 4. kubectl 查詢 Ingress |
| `PROJECT_ID` | 1. 環境變數 → 2. `GET /api/v1/projects` 取第一個 `phase=Ready` 專案的 **`namespace` 欄位** |

> ⚠️ **重要**：`PROJECT_ID` 務必使用專案物件的 `namespace` 欄位值（如 `proj-xxxxxx`），**非展示用的 `id`**（如 `test-project`），否則後端 API 將回報 Namespace 找不到或 500 錯誤。

> ⚠️ **Token 有效期：** 若中途 API 回傳 `401`，重新執行 `bootstrap_env.py` 刷新即可。

> 💡 **手動 Fallback：** 若 `bootstrap_env.py` 失敗（例如無 kubectl 權限），可改用 `get_token.py` 從瀏覽器 refresh_token 換取 Token（見下方備用方式）。

### 備用方式：從瀏覽器 Cookie 換取 Token
若 `bootstrap_env.py` 完全無法自動取得 Token：
1. **取得 refresh_token**：請使用者登入 afsbox Portal，打開瀏覽器 DevTools (F12) → Application → Cookies → 複製 `refresh_token` 的 Value。
2. **換取 token**：
   ```bash
   python3 scripts/get_token.py "{API_BASE_URL}" "{REFRESH_TOKEN}" > /tmp/token
   ```

> 💡 `HF_CREDENTIAL_ID`：HuggingFace Token credential，僅 gated/private 模型需要。


---

## STEP 1｜解析模型 ID

執行以下腳本解析使用者輸入的模型名稱：

```bash
python3 scripts/resolve_model.py "{USER_INPUT}"
```

**腳本輸出格式與處理策略：**
* 若 `exact=true`，直接使用 `hf_model_id` 並繼續 STEP 2：
  ```json
  { "success": true, "exact": true, "hf_model_id": "google/gemma-4-12b-it" }
  ```
* 若 `exact=false`，代表未匹配到本地別名，但從 HuggingFace 搜尋到了候選清單。此時請在對話中呈現此清單並詢問使用者選擇哪一個，選擇後再繼續：
  ```json
  { "success": true, "exact": false, "candidates": ["deepseek-ai/DeepSeek-V3", ...] }
  ```
* 若 `success=false`，提示搜尋失敗，並請使用者手動輸入正確的 HuggingFace Repo ID。

確定好模型 ID 後，輸出：
```
🚀 開始部署 {hf_model_id}
   Project: {project_id}

[1/6] ✅ 模型 ID：{hf_model_id}
```

---

## STEP 2｜找相容的 vLLM Engine

執行腳本（以實際值取代 `{API_BASE_URL}` 和 `{ACCESS_TOKEN}`）：

!`python3 scripts/find_engine.py "{API_BASE_URL}" "{ACCESS_TOKEN}"`

腳本輸出 JSON：`{ id, name, version, chartRef, servicePort }`

若輸出以 `ERROR:` 開頭，執行以下自我修復邏輯：

> 💡 **自動自我修復 (Self-healing Engine)：**
> 若 `find_engine.py` 報錯找不到 vLLM Engine：
> `find_engine.py` 內部已具有自動自我修復機制，會自動透過 `kubectl` 套用內嵌的 ModelEngine 範本並動態探索適配的 Docker Image 來建立 `auto-vllm-engine`。
> 1. 若環境有 `kubectl` 權限，腳本會自動完成建立。
> 2. 若因缺乏權限等原因自動建立失敗，請在對話中詳細引導使用者登入 Portal 至 **Admin > Models > Templates** 手動建立 vLLM Engine 範本。

記錄 `ENGINE_ID`、`ENGINE_NAME`、`ENGINE_VERSION`、`CHART_REF_NAME`。

> 💡 **vLLM Engine 映像檔說明 (Image Description)：**
> - vLLM Engine 內使用的 Container Image 預設為 NVIDIA NGC 的 vLLM 映像檔 (例如 `nvcr.io/nvidia/vllm`)。
> - 在選擇與排序 Engine 時，`find_engine.py` 會自動偵測並優先選擇名稱或 ID 包含 `nvidia` 的 NGC vLLM 引擎 (其版本號會被模擬為 `99.0.0` 進行優先排序)，以確保獲得針對 Blackwell 等新一代 GPU 的最佳化效能與相容性支援。

```
[2/6] ✅ Engine：{ENGINE_NAME}（vLLM {ENGINE_VERSION}）
```

---

## STEP 3｜查詢 vLLM Recipe

執行腳本（以實際值取代，HARDWARE 可選填如 `h100`、`h200`）：

!`python3 scripts/find_recipe.py "{HF_MODEL_ID}" "{HARDWARE}"`

腳本輸出 JSON：`{ found, min_vllm_version, argv, variants, has_fp8, has_nvfp4, recipe_url }`

記錄：
- `RECIPE_MIN_VERSION`、`RECIPE_ARGV`
- `HAS_FP8`（`has_fp8` 欄位）
- `RECIPE_FOUND`（`found` 欄位）

若 `found=false`，使用 fallback argv 並繼續，在進度中標示 `[no recipe]`。

**驗證版本相容性**（LLM 推理）：
若 `ENGINE_VERSION` < `RECIPE_MIN_VERSION`，重新執行 Step 2 腳本並加上 `MIN_VERSION` 參數：

!`python3 scripts/find_engine.py "{API_BASE_URL}" "{ACCESS_TOKEN}" "{RECIPE_MIN_VERSION}"`

```
[3/6] ✅ Recipe：最低 vLLM {RECIPE_MIN_VERSION}+  has_fp8={HAS_FP8}
       來源：{recipe_url}
```

---

## STEP 4｜下載模型

**4a. 確認是否已存在並產生 slug：**

!`python3 scripts/check_repo.py "{API_BASE_URL}" "{ACCESS_TOKEN}" "{PROJECT_ID}" "{HF_MODEL_ID}"`

腳本輸出 JSON：`{ exists, repo_name, phase, slug }`

- 若 `exists=true` → 記錄 `REPO_NAME={repo_name}`，**跳至 STEP 5**
- 若 `exists=false` → 記錄 `REPO_NAME={slug}`，繼續 4b

**4b. 建立 ModelRepository 並觸發下載：**

執行腳本（若有 `HF_CREDENTIAL_ID` 則加在最後）：

!`python3 scripts/create_repo.py "{API_BASE_URL}" "{ACCESS_TOKEN}" "{PROJECT_ID}" "{HF_MODEL_ID}" "{REPO_NAME}"`

若有 gated/private 模型需要 credential：

!`python3 scripts/create_repo.py "{API_BASE_URL}" "{ACCESS_TOKEN}" "{PROJECT_ID}" "{HF_MODEL_ID}" "{REPO_NAME}" "{HF_CREDENTIAL_ID}"`

腳本輸出 JSON：`{ created, repo_name, source_uri }`

- `created=true` → 建立成功，繼續輪詢
- `reason=already_exists` → 已存在但非 Ready（可能上次失敗），繼續輪詢
- 輸出以 `ERROR:` 開頭 → 顯示錯誤並停止

> 💡 **儲存後端說明 (Storage Backend)：**
> - 由於 K3s 叢集中未安裝 OCI CSI 驅動程式 (`driver name oci.csi.driver not found`)，無法支援 `oci` 類型的儲存掛載。
> - 目前平台已將預設模型儲存後端修改為 **`local`**。
> - `create_repo.py` 在建立 `ModelRepository` 時未指定 `storage.type`，將自動套用平台的預設儲存設定 (`local`)。

**4c. 輪詢下載狀態：**

!`python3 scripts/poll_download.py "{API_BASE_URL}" "{ACCESS_TOKEN}" "{PROJECT_ID}" "{REPO_NAME}"`

- 輸出以 `READY:` 開頭 → 解析 JSON，記錄為 `MODEL_INFO`
- 輸出以 `FAILED:` 開頭 → 顯示錯誤訊息，詢問是否重試
- 輸出 `TIMEOUT` → 顯示超時，提示手動確認

```
[4/6] ✅ 模型就緒：{REPO_NAME}
       大小：{total_size}  Context：{context_length} tokens
       最小 GPU 需求：{required_min_gpu_memory / 1e9:.1f} GB
       KV Cache/token：{kv_cache_memory_per_token} bytes
```

---

## STEP 5｜計算最佳部署參數

**5a. 取得硬體資訊（兩個 API 都要呼叫）：**

```
GET {API_BASE_URL}/api/v1/clusters/resourcepresets
Authorization: Bearer {ACCESS_TOKEN}
```
記錄回應為 `PRESETS_JSON`。

```
GET {API_BASE_URL}/api/v1/clusters/resourcepresets/capability
Authorization: Bearer {ACCESS_TOKEN}
```
記錄回應為 `CAPABILITY_JSON`。

**5b. 執行參數計算腳本：**

!`python3 scripts/calc_params.py '{MODEL_INFO}' '{PRESETS_JSON}' '{CAPABILITY_JSON}' balanced {HAS_FP8}`

腳本輸出 JSON：`{ preset, gpu_count, product, tp_size, max_model_len, dtype, gpu_memory_utilization, max_num_seqs, vram_used_gb, vram_total_gb, vram_pct, gpu_memory_limit_mib, gpu_cores_limit }`

若輸出含 `"error"` 欄位，執行以下自我修復邏輯：

> 💡 **自動自我修復 (Self-healing Preset)：**
> 若 `calc_params.py` 報錯 `no feasible preset found` 或 preset 列表為空：
> `calc_params.py` 內部已具有自動自我修復機制，會自動偵測主機 GPU 規格與記憶體限制，並動態產生對應的 `ResourcePreset`。
> 1. 若環境有 `kubectl` 權限，腳本會自動完成建立並自動重新評估。
> 2. 若無 K8s 權限導致無法自動建立，則在對話中引導使用者登入 Portal 至 **Admin > Resources > Presets** 手動新增適合當前 GPU 規格與模型大小的 GPU Preset（例如 GB10 96Gi 規格，並配合單卡多虛擬卡調度限制進行設定）。
> ⚠️ **vGPU 實體卡調度限制注意**：
> 在只有 1 張實體 GPU 的單節點/單卡環境中，排程器不支援將同一個容器的複數個虛擬卡分配在同一個實體 GPU 上（會報 `NodeInsufficientDevice`）。因此大模型（如 72B）**不能**選用 `gpu: 4` 的多虛擬卡 Preset，而應選用 `gpu: 1` 但 `sharing.nvidia.com/gpumem` 大顯存配置的 Preset（例如 `auto-preset-nvidia-gb10-1x-large`，配置 80 GiB 顯存）。`calc_params.py` 已對此自動優化，確保只推薦符合實體卡數上限的 Preset。

記錄結果為 `PARAMS`。

```
[5/6] ✅ 參數計算完成
       Preset：{preset}（{gpu_count}× {product}）
       --tensor-parallel-size {tp_size}
       --max-model-len {max_model_len}
       --gpu-memory-utilization 0.9
       --max-num-seqs {max_num_seqs}
       --dtype {dtype}
       VRAM 使用：{vram_used_gb} / {vram_total_gb} GB（{vram_pct}%）
```

---

## STEP 6｜部署 ModelServing

**6a. 取得 Engine 的 question variable 名稱：**

```
GET {API_BASE_URL}/api/v1/models/engines/{ENGINE_ID}
Authorization: Bearer {ACCESS_TOKEN}
```

從 `additionalQuestions` 陣列找出以下 flag 對應的 `variable` 名稱：

| vLLM flag | 常見 variable 名稱 |
|---|---|
| --tensor-parallel-size | `tensorParallelSize` |
| --max-model-len | `maxModelLen` |
| --gpu-memory-utilization | `gpuMemoryUtilization` |
| --max-num-seqs | `maxNumSeqs` |
| --dtype | `dtype` |

> [!CAUTION]
> **命名限制與點號字元規則：**
> `SERVING_NAME` 必須為符合 DNS 標準之名稱，**絕對不能含有點號 `.`**（因為 Kubernetes 不允許 Service 的名稱含點號，否則 Helm 安裝會失敗，導致服務無 ClusterIP 與路由可用）。
> 
> 規則：`SERVING_NAME` = `{REPO_NAME 前 30 字元}-serving`（**務必將所有點號 `.` 替換為橫線 `-`**，例如 `qwen-qwen2.5-0.5b` 必須被改寫為 `qwen-qwen2-5-0-5b-serving`）。

> 💡 **預防性寫入 answers 欄位**：
> - 雖然一些指令參數（如 `values.command` 與 `values.env`）在 Engine 模板中標示為預設且唯讀 (`editable: false`)，但在發送 servings 部署時，**必須在 `answers` 中完整帶入所有的 `values.command` 與 `values.env` 欄位**，否則後端控制器並不會主動幫我們補全這些 default，導致 Pod 啟動參數遺失而無法正常運行。
> 
> 💡 **vGPU (Hami) 資源限制宣告與 Preset 連動機制**：
> - AFSBox 的 `ResourcePreset` 資源支援 `gpuInfo.sharing` 配置（例如 `auto-preset-*` 已自動寫入 `nvidia.com/gpumem`）。當您選用有設定 `sharing` 的 Preset 時，Controller 會**自動為 Pod 注入顯存限制**，您**不需**在 `answers` 中手動填寫 `"values.resources"`。
> - 若使用的 Preset 沒有宣告 `sharing` 顯存限額，且 `gpu_memory_limit_mib > 0`（vGPU 共享環境），則**必須**在 `answers` 中手動加入 `"values.resources"` 進行顯式限制，以防止 vLLM 搶占整張實體卡的顯存導致其他服務 Pending：
>   ```json
>   "values.resources": {
>     "requests": {
>       "nvidia.com/gpumem": {gpu_memory_limit_mib},
>       "nvidia.com/gpucores": {gpu_cores_limit}
>     },
>     "limits": {
>       "nvidia.com/gpumem": {gpu_memory_limit_mib},
>       "nvidia.com/gpucores": {gpu_cores_limit}
>     }
>   }
>   ```
> - 注意：不需要使用點號字元（如 `values.resources.limits.nvidia.com/gpumem`），直接寫入一個結構化的巢狀 JSON 物件即可，這能完美避開 API 解析點號字元的 parser 限制。
> 
> 💡 **單節點資源受限環境之部署策略 (Deployment Strategy)**：
> - 在資源受限的單節點叢集（如 allocatable memory 只有 18.6 GiB 的環境），若要「同時跑多個服務」或「進行滾動更新」，常會因 Host 記憶體不足而卡死（滾動更新會同時存在新舊兩個 Pod，導致 memory 需求加倍）。
> - 為了避免此資源死鎖，**必須**將部署更新策略改為 `Recreate`（先刪除舊 Pod，再建立新 Pod），可在 `answers` 中加入：
>   ```json
>   "values.workload": {
>     "replicas": 1,
>     "strategy": {
>       "type": "Recreate"
>     }
>   }
>   ```

**6b. 預覽部署 YAML：**

```
POST {API_BASE_URL}/api/v1/models/projects/{PROJECT_ID}/engines/preview
Authorization: Bearer {ACCESS_TOKEN}
Content-Type: application/json

{
  "name": "{SERVING_NAME}",
  "engineRef": "{ENGINE_ID}",
  "modelType": "llm",
  "chartRef": { "name": "{CHART_REF_NAME}" },
  "engine": {
    "type": "vllm",
    "servicePort": 8000
  },
  "answers": {
    "model.valueFrom.kind": "ModelRepository",
    "model.valueFrom.name": "{REPO_NAME}",
    "resource": "{preset}",
    "servedModelName": "{SERVING_NAME}",
    "values.image": "nvcr.io/nvidia/vllm:26.02-py3",
    "output.contextLength": {max_model_len},
    "output.batchSize": {max_num_seqs},
    "GPU_MEMORY_UTILIZATION": 0.9,
    "MAX_NUM_BATCHED_TOKENS": 4096,
    "values.command": [
      "python3",
      "-m",
      "vllm.entrypoints.openai.api_server",
      "--model=${MODEL_PATH}",
      "--served-model-name=${SERVED_MODEL_NAME}",
      "--port=${SERVICE_PORT}",
      "--max-model-len=${CONTEXT_LENGTH}",
      "--max-num-seqs=${BATCH_SIZE}",
      "--dtype=bfloat16",
      "--gpu-memory-utilization=${GPU_MEMORY_UTILIZATION}",
      "--max-num-batched-tokens=${MAX_NUM_BATCHED_TOKENS}"
    ],
    "values.env": [
      {"name": "VLLM_NO_USAGE_STATS", "value": "1"},
      {"name": "VLLM_DO_NOT_TRACK", "value": "1"}
    ]
  }
}
```

顯示回應 YAML 的前 30 行，然後詢問：
```
以上是即將部署的 YAML 設定。確認部署？(yes/no)
```
**若使用者回答 no** → 停止，顯示「部署已取消」。

**6c. 確認後執行部署：**

```
POST {API_BASE_URL}/api/v1/models/projects/{PROJECT_ID}/servings
Authorization: Bearer {ACCESS_TOKEN}
Content-Type: application/json

{
  "name": "{SERVING_NAME}",
  "engineRef": "{ENGINE_ID}",
  "engine": {
    "type": "vllm",
    "servicePort": 8000
  },
  "answers": {
    "model.valueFrom.kind": "ModelRepository",
    "model.valueFrom.name": "{REPO_NAME}",
    "resource": "{preset}",
    "servedModelName": "{SERVING_NAME}",
    "values.image": "nvcr.io/nvidia/vllm:26.02-py3",
    "output.contextLength": {max_model_len},
    "output.batchSize": {max_num_seqs},
    "GPU_MEMORY_UTILIZATION": 0.9,
    "MAX_NUM_BATCHED_TOKENS": 4096,
    "values.command": [
      "python3",
      "-m",
      "vllm.entrypoints.openai.api_server",
      "--model=${MODEL_PATH}",
      "--served-model-name=${SERVED_MODEL_NAME}",
      "--port=${SERVICE_PORT}",
      "--max-model-len=${CONTEXT_LENGTH}",
      "--max-num-seqs=${BATCH_SIZE}",
      "--dtype=bfloat16",
      "--gpu-memory-utilization=${GPU_MEMORY_UTILIZATION}",
      "--max-num-batched-tokens=${MAX_NUM_BATCHED_TOKENS}"
    ],
    "values.env": [
      {"name": "VLLM_NO_USAGE_STATS", "value": "1"},
      {"name": "VLLM_DO_NOT_TRACK", "value": "1"}
    ]
  }
}
```

API 呼叫失敗（非 2xx）時顯示錯誤並停止。

> 💡 **關於本地模型載入參數 (`--model=${MODEL_PATH}`)：**
> - 當 `ModelRepository` 設定為 `local` 儲存後端時，控制器會自動掛載主機上的模型路徑到容器內，並在容器中注入 `MODEL_PATH` 環境變數。
> - 在 `values.command` 的啟動命令中，**務必使用 `--model=${MODEL_PATH}`**。不論模型儲存後端是 `local` 還是 `oci`，此環境變數都會由控制器自動解析並指向正確的模型路徑，避免因硬編碼實體路徑而導致模型載入失敗。

```
[6/6] 🚀 部署中，等待服務就緒...
```

---

## STEP 7｜等待服務就緒

!`python3 scripts/poll_serving.py "{API_BASE_URL}" "{ACCESS_TOKEN}" "{PROJECT_ID}" "{SERVING_NAME}"`

- 輸出以 `READY:` 開頭 → 解析 JSON，顯示完整結果
- 輸出 `TIMEOUT` → 顯示診斷建議

**就緒後顯示：**
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ 部署完成！{HF_MODEL_ID}

內部端點：{internal}
外部端點：{external} (僅當 answers 中包含 externalAccess=true)
模型名稱：{model_name}
總耗時：  {elapsed} 秒

快速內部測試 (由於 AI Gateway 內部路由採用 Header 匹配，測試時必須包含 Host 與 x-model Header)：
curl -s -X POST {internal}/v1/chat/completions \
  -H "Host: afsbox-aigateway.afsbox-system.svc.cluster.local" \
  -H "x-model: {model_name}" \
  -H "Content-Type: application/json" \
  -d '{"model":"{model_name}","messages":[{"role":"user","content":"你好"}]}'
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**超時時顯示：**
```
⏰ 等待超時（600 秒）。服務可能仍在啟動中。

請手動確認：
  GET {API_BASE_URL}/api/v1/models/projects/{PROJECT_ID}/servings/{SERVING_NAME}

常見問題排查：
  1. ResourcePreset GPU 資源是否充足？
  2. ModelRepository phase 是否為 Ready？
  3. Engine chartRef 是否存在且可用？
  4. 查看 serving 的 conditions 欄位是否有錯誤訊息
```

---

## GGUF 格式與 llama.cpp 推理引擎部署指南

如果部署的模型是 GGUF 格式，或者需要使用 `llama.cpp` 作為推理引擎，應採用以下特別策略：

### 1. 引擎與鏡像選擇
*   **引擎與範本**：需建立或使用支持 `llama.cpp` 的 `ModelEngine`（例如名為 `llamacpp-b9371` 的範本，其 `chartRef.name` 應為該叢集內實際的 `inference-engine-0.1.0` 或對應版本）。
*   **引擎類型**：在 ModelServing 提交 API 中，`engine.type` 設為 `vllm`（與底層 API 相容），但 `servicePort` 需設為 `5000`（或 llama-server 設定的通訊埠），且必須在 `answers` 中明確設定 `values.service.port: 5000`。
*   **私有 Docker Hub 認證**：若使用 `ociscloud/llama.cpp:b9371-cu1300-gb10` 等私有鏡像，部署時必須在 `answers` 中加入私有登錄秘密以防止 `ErrImagePull`：
    ```json
    "values.imagePullSecrets": [
      { "name": "regcred-ocis" }
    ]
    ```

### 2. 確定模型檔案名稱
GGUF 倉庫通常包含多個檔案或多級目錄，因此必須找出具體的 `.gguf` 檔案路徑：
*   在 `ModelRepository` `Ready` 後，透過該 repo 的實體路徑（主機上的 `/var/lib/afsbox/models/{PROJECT_ID}/models/.../latest`）執行 `ls` 列出目錄，找出正確的 GGUF 檔名（例如 `Qwable-27b_Q4_K_M.gguf`）。

### 3. 模型載入絕對路徑配置 (關鍵)
在 `llama.cpp` 的啟動指令中，直接使用 `${MODEL_PATH}` 有時可能因環境變數解析空值，導致容器去根目錄加載 `/YourModel.gguf` 而發生 **No such file or directory** 錯誤。
*   **最佳實踐**：在 `values.command` 的啟動命令中，將掛載路徑寫死為容器內的絕對路徑：
    `/models/{PROJECT_ID}/models/{REPO_NAME_SUB_PATH}/latest/{GGUF_FILE}`
    （例如：`/models/proj-707c659f/models/mia-ailab/qwable-3.6-27b/latest/Qwable-27b_Q4_K_M.gguf`）
*   **啟動命令範例**：
    ```json
    "values.command": [
      "/opt/llama.cpp/llama-server",
      "-m",
      "/models/proj-707c659f/models/mia-ailab/qwable-3.6-27b/latest/Qwable-27b_Q4_K_M.gguf",
      "-c",
      "128400",
      "--parallel",
      "1",
      "--batch-size",
      "2048",
      "--ubatch-size",
      "512",
      "--cache-type-k",
      "q4_0",
      "--cache-type-v",
      "q4_0",
      "--threads",
      "16",
      "--jinja",
      "-n",
      "-1",
      "--host",
      "0.0.0.0",
      "--port",
      "5000",
      "-ngl",
      "512",
      "--flash-attn",
      "on",
      "--alias",
      "qwable-3.6-27b",
      "--tensor-split",
      "1",
      "--no-webui"
    ]
    ```

---

## 清理與刪除已部署的模型服務 (ModelServing)

當使用者表達要「刪除」、「停止」、「下線」或「清理」某個 ModelServing 時（例如：「幫我把 qwen-qwen2-5-0-5b-serving 下線」），請執行以下腳本：

```bash
python3 scripts/delete_serving.py "{API_BASE_URL}" "{ACCESS_TOKEN}" "{PROJECT_ID}" "{SERVING_NAME}"
```

**腳本輸出 JSON：**
* 成功刪除：`{ "deleted": true, "serving_name": "{SERVING_NAME}", "response": ... }`
* 服務已不存在或已被刪除 (404)：`{ "deleted": true, "reason": "not_found", "serving_name": "{SERVING_NAME}" }`

---

## 錯誤處理與常見部署異常排除 (Kubernetes 層級)

- 每個 API 呼叫失敗都要顯示 HTTP status code + response body 摘要。
- 不靜默跳過任何錯誤。
- 使用者隨時可說「停止」或「取消」中斷流程。
- API 回傳 401 → 提示 Token 過期，請重新取得。
- API 回傳 403 → 提示該 Project 可能無權限存取。
- API 回傳 404 → 顯示找不到的資源名稱，確認 ID 是否正確。

### 🚨 故障排除指南：

#### 1. 新的 Pod 處於 Pending (顯示 Insufficient memory 資源死鎖)
*   **原因**：強行刪除卡住的 servings 或 HelmRelease 時，若透過 patch 移成了 finalizer，Flux 會跳過 uninstall，使舊 Pod 變成「孤兒 Pod」留在叢集內並持續佔用資源。再次部署的新 Pod 由於記憶體被孤兒 Pod 佔滿而調度失敗。
*   **解決方法**：手動強制刪除該專案 Namespace 中的舊容器以釋放記憶體資源：
    ```bash
    kubectl delete pod -l afsbox.asus.com/model-serving={SERVING_NAME} -n {PROJECT_ID} --grace-period=0 --force
    ```

#### 2. HelmRelease 狀態為 ObservedGeneration: -1 且無事件更新
*   **原因**：快速強行刪除資源時，Flux `helm-controller` 的 leader lease 或是工作線程發生卡鎖。
*   **解決方法**：重啟 Flux 控制器以進行解鎖：
    ```bash
    kubectl rollout restart deployment helm-controller -n flux-system
    ```