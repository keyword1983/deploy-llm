# deploy-llm Skill 改進清單

> 建立日期：2026-06-22

---

## ✅ 已確認設計良好的部分

- [x] 完整的自我修復 (Self-healing) 機制 — Engine / Preset 自動建立
- [x] 4 層 Recipe Fallback 策略 — 精確比對 → 祖先繼承 → 關鍵詞 → config 推斷
- [x] 精確的 KV Cache 計算 — `num_layers × kv_heads × head_dim`
- [x] vGPU / Hami 完整支援 — `vgpu_scale` 偵測與 `tp_size` 限制
- [x] 環境自動探索 — `bootstrap_env.py` 四層 URL + Token 探索
- [x] GGUF / llama.cpp 專用部署指南
- [x] 大量實戰經驗硬編碼（DNS 命名、Recreate 策略、answers 完整帶入、PROJECT_ID 用 namespace）

---

## 🔧 待改進項目

### P0 — 安全與相容性（必須修）

- [x] **B. 憑證硬編碼**
  - 影響：`bootstrap_env.py` 和 `get_token.py` 硬編碼 `admin@asus.com` / `admin`
  - 方案：從 K8s Secret 讀取（新增 `ADMIN_USERNAME` / `ADMIN_PASSWORD` key），或改用環境變數 `KEYCLOAK_ADMIN_USER` / `KEYCLOAK_ADMIN_PASS`
  - 狀態：已改為優先讀取 `KEYCLOAK_ADMIN_USER` / `KEYCLOAK_ADMIN_PASS` 環境變數與 K8s Secret `ADMIN_USERNAME` / `ADMIN_PASSWORD` 欄位，最後才 fallback 到默認值。

- [x] **A. 路徑硬編碼**
  - 影響：SKILL.md 與腳本中 `/home/asus/.gemini/skills/deploy-llm/scripts/`、`/home/asus/frank/afsbox/` 等絕對路徑，換環境失效
  - 方案：改用環境變數 `SKILL_DIR` 或 `${BASH_SOURCE}` 動態解析；SKILL.md 中用 `{SKILL_DIR}/scripts/` 替代
  - 狀態：已全面改為相對路徑（包括 scripts/xxx.py 呼叫，並消除了對本地實體 yaml 的路徑依賴）。

### P1 — 穩定性（強烈建議修）

- [x] **D. 輪詢期間 Token 過期自動刷新**
  - 影響：`poll_download.py` 預設 30 分鐘，Keycloak token 常在中途過期導致 401 無限重試
  - 方案：偵測 HTTP 401 時重新呼叫 `bootstrap_env.py` 取得新 token
  - 狀態：已建立 `token_utils.py` 作為共用模組，並在 `poll_download.py`、`poll_serving.py` 等所有連線腳本中引入進行 401 自動刷新與重試。

- [x] **F. 部署後健康檢查 (Smoke Test)**
  - 影響：`poll_serving.py` 只檢查 `internalEndpoint` 有值就標記 READY，但模型可能載入失敗
  - 方案：找到 endpoint 後呼叫 `/v1/models` 確認模型名稱出現，或發一筆 `chat/completions` 測試請求
  - 狀態：已在 `poll_serving.py` 中整合 `/v1/models` 健康檢查，能智慧處理未就緒時的載入等待，並具備外部網絡無法連接時的自動 fallback 降級機制。

- [x] **M. Model Info 解析失敗**
  - 影響：當 `poll_download.py` 輸出 JSON 解析異常，或 AI 提取之 JSON 欄位遺失時，`calc_params.py` 算出的顯存預估可能為 0。
  - 方案：為 `calc_params.py` 加上容錯設計，使其在 JSON 損壞或不齊全時，能直接作為 repo_name 字串傳入並呼叫 AFSBox API 自動獲取模型屬性。
  - 狀態：已完成 `calc_params.py` 的容錯改造，即使傳入純字串 repo_name，亦可利用 cached token 回拉 API 模型規格。

- [x] **N. Recipe 匹配不精確**
  - 影響：對 sub-1B 模型（如 `Qwen2.5-0.5B`）進行模糊匹配時，因 regex 浮點數解析 Bug，將 `0.5B` 誤識為 `5B`，並進一步錯配到帶有 Qwen3 專用參數的 `Qwen3-4B` 模板上。
  - 方案：修正為支援浮點數的 regex，並在模糊比對中加入「世代匹配（Generation Match）」以及「VL/Text 分流」防護機制。
  - 狀態：已修改 `find_recipe.py`，現在 `0.5B` 可正確比對為 0.5 規格，且會過濾跨世代與 VL/Text 錯配，成功精準 fallback 至相容的 `Qwen2.5-32B` 模板（或推斷模式）。

### P2 — 維護性（建議修）

- [x] **C. 程式碼重複**
  - 影響：`bootstrap_env.py::get_token()` 與 `get_token.py::get_token_via_k8s()` 約 60 行邏輯重複
  - 方案：抽取共用模組 `scripts/auth.py`，兩個腳本 import 共用函數
  - 狀態：已將所有的憑證/K8s 交互登入邏輯統一收攏至 `token_utils.py` 的 `refresh_token()` 函數，原有的重複邏輯全部改為 import 該共用函數。

- [x] **I. stdout/stderr 分離**
  - 影響：progress 行（`phase=Running elapsed=60s`）混在 stdout，AI 解析 JSON 時可能失敗
  - 方案：所有 progress 輸出改用 `file=sys.stderr`
  - 狀態：已將 `poll_download.py` 與 `poll_serving.py` 的所有進度及診斷輸出全面導向 `sys.stderr`，只將最終的 `READY:JSON` / `FAILED:MSG` 結果輸出於 `stdout`，確保自動化解析正常。

### P3 — 功能擴充（有空再修）

- [ ] **E. 部署前驗證 (Pre-flight)**
  - 影響：沒有預檢 GPU 可用性、叢集資源是否足夠、模型檔案完整性
  - 方案：STEP 4 前加入 nvidia-smi 檢查、allocatable 資源檢查、kubectl 連線檢查

- [ ] **G. Rollback / 清理機制**
  - 影響：部署失敗後沒有自動清理 ModelRepository 和失敗的 Serving
  - 方案：失敗時提供自動清理選項（刪除 Serving + 可選刪除 ModelRepository）

- [x] **H. 模型映射表維護機制**
  - 影響：STEP 1 的模型名稱對照表是靜態的，新模型需手動更新
  - 方案：加入 HF API 搜尋 fallback，根據輸入關鍵詞搜尋並請使用者確認

- [ ] **J. LoRA 支援**
  - 影響：目前只支援單一基底模型，不支援 LoRA adapter 加載
  - 方案：SKILL.md 中加入 LoRA 部署的額外步驟和 `--lora-modules` 參數

- [x] **K. vLLM Image Tag 動態化**
  - 影響：SKILL.md STEP 6 寫死 `"values.image": "nvcr.io/nvidia/vllm:26.02-py3"`
  - 方案：從 `find_engine.py::detect_image_source()` 回傳 image，SKILL.md 動態替換

- [ ] **L. 並行部署衝突處理**
  - 影響：同一 Project 同時部署多模型時可能 GPU 記憶體不足
  - 方案：部署前 GET 當前 Servings 並計算總 GPU 用量，評估剩餘資源
