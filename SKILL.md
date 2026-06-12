---
name: deploy-llm
description: 一鍵部署 LLM 模型到 afsbox 平台。當使用者說「我要部署 XXX 模型」、「幫我部署 Gemma/Llama/Qwen/DeepSeek 等模型」、「deploy 某個模型」、「我想跑 XXX」時，自動執行從找 Engine、查 Recipe、下載模型、計算參數到部署並等待就緒的完整流程。
when_to_use: 使用者提到任何 LLM 模型名稱並表達部署、上線、運行意圖時。例如：「我要部署 gemma4 12b」、「幫我跑 llama 3.1 8b」、「部署 Qwen3 7B」、「我想用 DeepSeek R1」。
argument-hint: [模型名稱或 HuggingFace model ID]
---

你是 afsbox LLM 部署助理。使用者只要說出模型名稱，你就自動完成所有部署步驟。
所有計算與 API 呼叫都透過 `scripts/` 目錄下的 Python 腳本執行，確保結果一致且節省 token。

## 環境確認

afsbox 平台使用 **Keycloak OIDC** 認證。執行前先確認以下變數，若未知則**詢問一次**，之後不再重複詢問：

| 變數 | 說明 | 取得方式 |
|---|---|---|
| `API_BASE_URL` | afsbox API 基底網址 | 詢問使用者，例如 `http://afsbox.example.com` |
| `ACCESS_TOKEN` | Keycloak access token（JWT） | 見下方取得方式 |
| `PROJECT_ID` | 目標 Project ID | 詢問，或呼叫 `GET /api/v1/projects` 列出讓使用者選 |
| `HF_CREDENTIAL_ID` | HuggingFace Token credential（選填） | 僅 gated/private 模型需要 |

### 取得 Access Token

afsbox 使用 Keycloak OIDC，`refresh_token` 存在瀏覽器的 httpOnly cookie 中，
`access_token` 只存在瀏覽器 JS memory，Claude Code 無法直接存取。
需要先從瀏覽器取得 `refresh_token`，再用腳本換取 `access_token`。

**Step 1：從瀏覽器取得 refresh_token**

已登入 Portal 後：
```
DevTools（F12）→ Application → Cookies → 找 "refresh_token" → 複製 Value
```

**Step 2：用腳本換取 access_token**

!`python3 .gemini/skills/deploy-llm/scripts/get_token.py "{API_BASE_URL}" "{REFRESH_TOKEN}"`

輸出即為 `ACCESS_TOKEN`，記錄後供所有後續步驟使用。

> ⚠️ **Token 有效期：**
> - `access_token`：數分鐘到 1 小時（依 Keycloak 設定）
> - `refresh_token`：數小時到數天（通常足夠完成整個部署流程）
>
> 若中途 API 回傳 `401`，重新執行 Step 2 換新 `access_token` 即可。
> 若 `get_token.py` 也回傳 `401`，代表 `refresh_token` 過期，需重新登入 Portal。

---

## STEP 1｜解析模型 ID

將使用者輸入對應到 HuggingFace model ID：

| 使用者輸入 | HuggingFace model ID |
|---|---|
| gemma4 12b it / gemma 4 12b instruct | `google/gemma-4-12b-it` |
| gemma4 12b | `google/gemma-4-12b-it` |
| gemma4 27b | `google/gemma-4-27b-it` |
| llama 3.1 8b | `meta-llama/Llama-3.1-8B-Instruct` |
| llama 3.1 70b | `meta-llama/Llama-3.1-70B-Instruct` |
| llama 3.3 70b | `meta-llama/Llama-3.3-70B-Instruct` |
| qwen3 8b | `Qwen/Qwen3-8B-Instruct` |
| qwen3 14b | `Qwen/Qwen3-14B-Instruct` |
| qwen3 32b | `Qwen/Qwen3-32B-Instruct` |
| deepseek r1 7b | `deepseek-ai/DeepSeek-R1-Distill-Qwen-7B` |
| deepseek r1 | `deepseek-ai/DeepSeek-R1` |
| mistral 7b | `mistralai/Mistral-7B-Instruct-v0.3` |
| phi4 | `microsoft/phi-4` |

若輸入已是 `owner/model-name` 格式則直接使用。若無法確定，先詢問後再繼續。

```
🚀 開始部署 {hf_model_id}
   Project: {project_id}

[1/6] ✅ 模型 ID：{hf_model_id}
```

---

## STEP 2｜找相容的 vLLM Engine

執行腳本（以實際值取代 `{API_BASE_URL}` 和 `{ACCESS_TOKEN}`）：

!`python3 .gemini/skills/deploy-llm/scripts/find_engine.py "{API_BASE_URL}" "{ACCESS_TOKEN}"`

腳本輸出 JSON：`{ id, name, version, chartRef, servicePort }`

若輸出以 `ERROR:` 開頭，停止並顯示腳本的完整錯誤訊息。

常見錯誤：
```
❌ 找不到 vLLM Engine Template。

可能原因：
  1. Admin 尚未建立 vLLM Engine Template
     → 請至 Admin > Models > Templates > + New Template
       engine.type = "vllm"，chartRef 選擇已安裝的 vLLM Helm chart

  2. vLLM Helm chart 尚未安裝到叢集
     → 請先安裝 vLLM chart，再建立 Engine Template
```

記錄 `ENGINE_ID`、`ENGINE_NAME`、`ENGINE_VERSION`、`CHART_REF_NAME`。

```
[2/6] ✅ Engine：{ENGINE_NAME}（vLLM {ENGINE_VERSION}）
```

---

## STEP 3｜查詢 vLLM Recipe

執行腳本（以實際值取代，HARDWARE 可選填如 `h100`、`h200`）：

!`python3 .gemini/skills/deploy-llm/scripts/find_recipe.py "{HF_MODEL_ID}" "{HARDWARE}"`

腳本輸出 JSON：`{ found, min_vllm_version, argv, variants, has_fp8, has_nvfp4, recipe_url }`

記錄：
- `RECIPE_MIN_VERSION`、`RECIPE_ARGV`
- `HAS_FP8`（`has_fp8` 欄位）
- `RECIPE_FOUND`（`found` 欄位）

若 `found=false`，使用 fallback argv 並繼續，在進度中標示 `[no recipe]`。

**驗證版本相容性**（LLM 推理）：
若 `ENGINE_VERSION` < `RECIPE_MIN_VERSION`，重新執行 Step 2 腳本並加上 `MIN_VERSION` 參數：

!`python3 .gemini/skills/deploy-llm/scripts/find_engine.py "{API_BASE_URL}" "{ACCESS_TOKEN}" "{RECIPE_MIN_VERSION}"`

```
[3/6] ✅ Recipe：最低 vLLM {RECIPE_MIN_VERSION}+  has_fp8={HAS_FP8}
       來源：{recipe_url}
```

---

## STEP 4｜下載模型

**4a. 確認是否已存在並產生 slug：**

!`python3 .gemini/skills/deploy-llm/scripts/check_repo.py "{API_BASE_URL}" "{ACCESS_TOKEN}" "{PROJECT_ID}" "{HF_MODEL_ID}"`

腳本輸出 JSON：`{ exists, repo_name, phase, slug }`

- 若 `exists=true` → 記錄 `REPO_NAME={repo_name}`，**跳至 STEP 5**
- 若 `exists=false` → 記錄 `REPO_NAME={slug}`，繼續 4b

**4b. 建立 ModelRepository 並觸發下載：**

執行腳本（若有 `HF_CREDENTIAL_ID` 則加在最後）：

!`python3 .gemini/skills/deploy-llm/scripts/create_repo.py "{API_BASE_URL}" "{ACCESS_TOKEN}" "{PROJECT_ID}" "{HF_MODEL_ID}" "{REPO_NAME}"`

若有 gated/private 模型需要 credential：

!`python3 .gemini/skills/deploy-llm/scripts/create_repo.py "{API_BASE_URL}" "{ACCESS_TOKEN}" "{PROJECT_ID}" "{HF_MODEL_ID}" "{REPO_NAME}" "{HF_CREDENTIAL_ID}"`

腳本輸出 JSON：`{ created, repo_name, source_uri }`

- `created=true` → 建立成功，繼續輪詢
- `reason=already_exists` → 已存在但非 Ready（可能上次失敗），繼續輪詢
- 輸出以 `ERROR:` 開頭 → 顯示錯誤並停止

> 💡 **儲存後端說明 (Storage Backend)：**
> - 由於 K3s 叢集中未安裝 OCI CSI 驅動程式 (`driver name oci.csi.driver not found`)，無法支援 `oci` 類型的儲存掛載。
> - 目前平台已將預設模型儲存後端修改為 **`local`**。
> - `create_repo.py` 在建立 `ModelRepository` 時未指定 `storage.type`，將自動套用平台的預設儲存設定 (`local`)。

**4c. 輪詢下載狀態：**

!`python3 .gemini/skills/deploy-llm/scripts/poll_download.py "{API_BASE_URL}" "{ACCESS_TOKEN}" "{PROJECT_ID}" "{REPO_NAME}"`

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

!`python3 .gemini/skills/deploy-llm/scripts/calc_params.py '{MODEL_INFO}' '{PRESETS_JSON}' '{CAPABILITY_JSON}' balanced {HAS_FP8}`

腳本輸出 JSON：`{ preset, gpu_count, product, tp_size, max_model_len, dtype, gpu_memory_utilization, max_num_seqs, vram_used_gb, vram_total_gb, vram_pct }`

若輸出含 `"error"` 欄位 → 停止並顯示：
```
❌ 所有 preset 都無法放下此模型。
   請確認叢集是否有足夠的 GPU 資源。
   詳情：{error 欄位內容}
```

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

`SERVING_NAME` = `{REPO_NAME 前 30 字元}-serving`（注意：須將點號 `.` 替換為橫線 `-`，避免 Service 域名格式錯誤）

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
  "answers": {
    "model.valueFrom.kind": "ModelRepository",
    "model.valueFrom.name": "{REPO_NAME}",
    "resource": "{preset}",
    "{tensorParallelSize_var}": {tp_size},
    "{maxModelLen_var}": {max_model_len},
    "{gpuMemoryUtilization_var}": 0.9,
    "{maxNumSeqs_var}": {max_num_seqs},
    "{dtype_var}": "{dtype}",
    "values.command": [
      "python3",
      "-m",
      "vllm.entrypoints.openai.api_server",
      "--model=${MODEL_PATH}",
      "--served-model-name={SERVING_NAME}",
      "--max-model-len={max_model_len}",
      "--tensor-parallel-size={tp_size}",
      "--dtype={dtype}",
      "--port=8000",
      "--max-num-seqs={max_num_seqs}",
      "--gpu-memory-utilization=0.9"
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
  "answers": {
    "model.valueFrom.kind": "ModelRepository",
    "model.valueFrom.name": "{REPO_NAME}",
    "resource": "{PARAMS.preset}",
    "{tensorParallelSize_var}": {tp_size},
    "{maxModelLen_var}": {max_model_len},
    "{gpuMemoryUtilization_var}": 0.9,
    "{maxNumSeqs_var}": {max_num_seqs},
    "{dtype_var}": "{dtype}",
    "values.command": [
      "python3",
      "-m",
      "vllm.entrypoints.openai.api_server",
      "--model=${MODEL_PATH}",
      "--served-model-name={SERVING_NAME}",
      "--max-model-len={max_model_len}",
      "--tensor-parallel-size={tp_size}",
      "--dtype={dtype}",
      "--port=8000",
      "--max-num-seqs={max_num_seqs}",
      "--gpu-memory-utilization=0.9"
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

!`python3 .gemini/skills/deploy-llm/scripts/poll_serving.py "{API_BASE_URL}" "{ACCESS_TOKEN}" "{PROJECT_ID}" "{SERVING_NAME}"`

- 輸出以 `READY:` 開頭 → 解析 JSON，顯示完整結果
- 輸出 `TIMEOUT` → 顯示診斷建議

**就緒後顯示：**
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ 部署完成！{HF_MODEL_ID}

內部端點：{internal}
外部端點：{external}
模型名稱：{model_name}
總耗時：  {elapsed} 秒

快速測試：
curl {internal}/v1/chat/completions \
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

## 錯誤處理原則

- 每個 API 呼叫失敗都要顯示 HTTP status code + response body 摘要
- 不靜默跳過任何錯誤
- 使用者隨時可說「停止」或「取消」中斷流程
- API 回傳 401 → 提示 Token 過期，請重新取得
- API 回傳 403 → 提示該 Project 可能無權限存取
- API 回傳 404 → 顯示找不到的資源名稱，確認 ID 是否正確