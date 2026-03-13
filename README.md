# merlinai-adapter-server

這個專案提供一個 FastAPI adapter server，將 OpenAI 格式的請求轉換成 Merlin API 格式，並自動用 Firebase 帳密換取 Bearer token。

## 功能

- 支持 `/v1/chat/completions` 端點
- 支持串流與非串流回應
- 自動取得與刷新 Merlin Bearer token
- 使用 `.env` 中的 `PROXY_API_KEY` 保護 proxy 入口
- 自動處理 Merlin API 所需的 UUID 與格式轉換
- 當請求帶有 `tools` 時，額外啟用 tool-calling 相容層，將 Merlin 輸出轉成 OpenAI `tool_calls`
- 若 `tool_choice` 為 `required` 或指定函式，但上游仍未產生有效工具呼叫，會回傳 `422`，避免上游誤判為成功純文字回覆
- 可用 Docker Compose 啟動
- 可用 `LOG_LEVEL=DEBUG` 檢查 Roo Code / OpenCode 實際送入的 payload
- 可將 debug 訊息透過 `loguru` 同步寫入專案內的 `logs/proxy.log`，也可關閉檔案寫入

## 安裝步驟

1. 確保已安裝 Python 3.12+
2. 安裝依賴套件

```bash
uv sync
```

3. 建立環境變數檔

```bash
copy .env.example .env
```

4. 編輯 `.env`，填入你的 Merlin 帳號密碼與 proxy API key

## 本機執行

```bash
uv run python main.py
```

伺服器將在 `http://0.0.0.0:8000` 啟動。

## Docker Compose

建立好 `.env` 後，直接執行：

```bash
docker compose up --build -d
```

查看 logs：

```bash
docker compose logs -f
```

停止服務：

```bash
docker compose down
```

啟動後服務會在：

```text
http://localhost:8000
```

## Tool calling 相容說明

當 client 傳入 OpenAI `tools` / `tool_choice` 時，proxy 會：

1. 保留 `mcpConfig.tools` 與 `mcpConfig.toolChoice` 給 Merlin
2. 同時把對話與工具定義包成嚴格 JSON 輸出指令，降低 Merlin 只回自然語言的機率
3. 若回來內容可解析為 `{"type":"tool_calls"...}`，proxy 會轉成 OpenAI `message.tool_calls`
4. 若 `tool_choice` 是 `required` 或指定函式，但仍解析不到工具呼叫，proxy 直接回 `422`

這樣上游就不會再收到「明明要求必需工具呼叫，卻被當成一般文字成功回覆」的假成功結果。

## Debug Roo Code / OpenCode payload

如果你要分析 tool calling，先把 `.env` 裡這個值調成：

```text
LOG_LEVEL=DEBUG
```

如果你只想輸出到 console、不想寫檔，也可以設定：

```text
LOG_TO_FILE=false
```

之後重新啟動 proxy。每次 `/v1/chat/completions` 都會輸出：

- 原始 request body
- 是否帶了 `tools`
- `tool_choice`
- 轉發給 Merlin 的 payload
- Merlin 回來的 event 摘要
- 最後回給客戶端的 OpenAI 格式 response

這些內容除了印到 console，也會透過 `loguru` 寫入專案內的 `logs/proxy.log`；超過約 1 MB 後會保留 3 份輪替檔。若 `LOG_LEVEL=INFO`，這些 debug payload 不會輸出；若 `LOG_LEVEL=DEBUG`，則會完整輸出。若 `LOG_TO_FILE=false`，則只輸出到 console。

這樣就能直接看 Roo Code / OpenCode 是不是有送 `tools`，以及 Merlin 回來有沒有任何可映射成 `tool_calls` 的結構。

## 使用範例

可用模型目前包含：`gpt-5.4`、`grok-4.1-fast`、`gemini-3.1-flash-lite`、`gemini-3.1-pro`、`claude-4.6-sonnet`、`claude-4.6-opus`、`glm-5`、`minimax-m2.5`。

呼叫 proxy 時要帶你自己的 proxy API key：

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-123" \
  -d '{
    "model": "claude-4.6-sonnet",
    "messages": [{"role": "user", "content": "你好！"}],
    "stream": true
  }'
```

## 環境變數

- `MERLIN_EMAIL`: Merlin 登入信箱
- `MERLIN_PASSWORD`: Merlin 登入密碼
- `MERLIN_FIREBASE_API_KEY`: Firebase Web API key
- `MERLIN_VERSION`: 轉發時使用的 Merlin version header
- `PROXY_API_KEY`: 你的 proxy 對外要求的 API key
- `LOG_LEVEL`: logger 層級，預設 `INFO`；設成 `DEBUG` 會輸出 request/response debug logs
- `LOG_TO_FILE`: 是否寫入檔案 log，預設 `true`

## 如何找到 `MERLIN_FIREBASE_API_KEY`

最直接的方法是從 Merlin Web 登入流程的 network request 取得。

1. 打開瀏覽器進入 `https://extension.getmerlin.in`
2. 開啟 DevTools 的 Network 分頁
3. 執行登入流程
4. 找這個請求：

```text
https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=...
```
