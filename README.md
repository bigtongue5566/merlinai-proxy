# Merlin to OpenAI API Proxy

這個專案提供一個 FastAPI 代理伺服器，將 OpenAI 格式的請求轉換成 Merlin API 格式，並自動用 Firebase 帳密換取 Bearer token。

## 功能

- 支持 `/v1/chat/completions` 端點
- 支持串流與非串流回應
- 支持 OpenAI `tools` / `tool_choice` 欄位轉發與回傳 `tool_calls`
- 自動取得與刷新 Merlin Bearer token
- 使用 `.env` 中的 `PROXY_API_KEY` 保護 proxy 入口
- 自動處理 Merlin API 所需的 UUID 與格式轉換
- 可用 Docker Compose 啟動

## 安裝步驟

1. 確保已安裝 Python 3.8+
2. 安裝依賴套件

```bash
pip install -r requirements.txt
```

3. 建立環境變數檔

```bash
copy .env.example .env
```

4. 編輯 `.env`，填入你的 Merlin 帳號密碼與 proxy API key

## 本機執行

```bash
python main.py
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

## 使用範例

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

### Tool use 範例

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-123" \
  -d '{
    "model": "claude-4.6-sonnet",
    "messages": [{"role": "user", "content": "台北現在幾點？"}],
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "get_current_time",
          "description": "Get current time of a timezone",
          "parameters": {
            "type": "object",
            "properties": {
              "timezone": {"type": "string"}
            },
            "required": ["timezone"]
          }
        }
      }
    ],
    "tool_choice": "auto"
  }'
```

## 環境變數

- `MERLIN_EMAIL`: Merlin 登入信箱
- `MERLIN_PASSWORD`: Merlin 登入密碼
- `MERLIN_FIREBASE_API_KEY`: Firebase Web API key
- `MERLIN_VERSION`: 轉發時使用的 Merlin version header
- `PROXY_API_KEY`: 你的 proxy 對外要求的 API key

## 如何找到 `MERLIN_FIREBASE_API_KEY`

最直接的方法是從 Merlin Web 登入流程的 network request 取得。

1. 打開瀏覽器進入 `https://extension.getmerlin.in`
2. 開啟 DevTools 的 Network 分頁
3. 執行登入流程
4. 找這個請求：

```text
https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=...
```

5. URL 裡 `key=` 後面的值就是 `MERLIN_FIREBASE_API_KEY`

例如：

```text
https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=AIzaSyAvCgtQ4XbmlQGIynDT-v_M8eLaXrKmtiM
```

這裡的：

```text
AIzaSyAvCgtQ4XbmlQGIynDT-v_M8eLaXrKmtiM
```

就是要填進 `.env` 的 `MERLIN_FIREBASE_API_KEY`。

補充：

- 這個值通常是 Merlin 前端所屬 Firebase 專案的 Web API key
- 它通常比帳號密碼穩定很多，但 Merlin 若改 Firebase 專案或更換前端設定，這個值仍可能改變
- 如果登入突然失效，可以先重新抓一次這個 key

## 注意事項

- `.env` 已加入 `.gitignore`，避免把帳密提交進版本庫
- `model` 參數會直接傳給 Merlin，請使用 Merlin 支持的模型名稱
- 若 Merlin 改變串流格式，可能需要調整 `merlin_stream_generator` 的解析邏輯
