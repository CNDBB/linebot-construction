import os, json, base64, hashlib, hmac, urllib.request
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from linebot.v3.messaging import (
    AsyncApiClient, AsyncMessagingApi, Configuration,
    ReplyMessageRequest, TextMessage
)
import anthropic
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

LINE_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_TOKEN  = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
CLAUDE      = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
line_config = Configuration(access_token=LINE_TOKEN)


# ── 驗簽 ──────────────────────────────────────────────
def validate_signature(body: bytes, signature: str) -> bool:
    h = hmac.new(LINE_SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(h).decode(), signature)


# ── Notion 儲存 ───────────────────────────────────────
def save_record(record: dict):
    try:
        url = "https://api.notion.com/v1/pages"
        headers = {
            "Authorization": f"Bearer {os.environ['NOTION_API_KEY']}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28"
        }
        payload = {
            "parent": {"database_id": os.environ["NOTION_DATABASE_ID"]},
            "properties": {
                "timestamp": {
                    "title": [{"text": {"content": record.get("timestamp", "")}}]
                },
                "group_id": {
                    "rich_text": [{"text": {"content": record.get("group_id", "")}}]
                },
                "sender_id": {
                    "rich_text": [{"text": {"content": record.get("sender_id", "")}}]
                },
                "raw_message": {
                    "rich_text": [{"text": {"content": record.get("raw_message", "")[:2000]}}]
                },
                "work_items": {
                    "rich_text": [{"text": {"content": record.get("work_items", "")}}]
                },
                "location": {
                    "rich_text": [{"text": {"content": record.get("location", "")}}]
                },
                "status": {
                    "select": {"name": record.get("status", "in_progress")}
                },
                "quantity": {
                    "rich_text": [{"text": {"content": str(record.get("quantity", ""))}}]
                },
                "issue_description": {
                    "rich_text": [{"text": {"content": str(record.get("issue_description", ""))}}]
                },
                "confidence": {
                    "number": float(record.get("confidence", 0))
                },
            }
        }
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(url, data=data, headers=headers, method="POST")
        urllib.request.urlopen(req)
        print(f"[NOTION SAVED] {record.get('raw_message', '')[:30]}")
    except Exception as e:
        print(f"[NOTION ERROR] {e}")


# ── Claude 文字分析 ───────────────────────────────────
def analyze_text(text: str, sender: str, group: str) -> dict:
    prompt = f"""你是台灣營造工地的施工記錄助理。
分析以下工班傳送的訊息，提取施工資訊並回傳 JSON。

訊息內容：{text}
傳送者：{sender}
群組（工地）：{group}

回傳格式（只回 JSON，不要其他文字）：
{{
  "work_items": ["工項1"],
  "location": "區域或樓層",
  "status": "completed 或 in_progress 或 issue",
  "quantity": null,
  "issue_description": null,
  "confidence": 0.85
}}

台灣工地常用語：打底=樓板混凝土、立模=模板組立、紮筋=鋼筋綁紮。
若訊息與施工無關，回傳 {{"irrelevant": true}}"""

    resp = CLAUDE.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    try:
        return json.loads(resp.content[0].text)
    except Exception:
        return {"error": "parse_failed"}


# ── 背景任務：處理文字訊息 ────────────────────────────
async def process_text_event(event: dict, reply_token: str):
    text    = event["message"]["text"]
    user_id = event["source"].get("userId", "unknown")
    group   = event["source"].get("groupId", "direct")

    result = analyze_text(text, user_id, group)

    if result.get("irrelevant") or result.get("error"):
        return

    save_record({
        "timestamp":         datetime.now().isoformat(),
        "group_id":          group,
        "sender_id":         user_id,
        "source_type":       "text",
        "raw_message":       text,
        "work_items":        "、".join(result.get("work_items", [])),
        "location":          result.get("location", ""),
        "status":            result.get("status", "in_progress"),
        "quantity":          result.get("quantity", "") or "",
        "issue_description": result.get("issue_description", "") or "",
        "confidence":        result.get("confidence", 0),
    })

    status_map = {
        "completed":   "✅ 完成",
        "in_progress": "🔄 進行中",
        "issue":       "⚠️ 異常"
    }
    items_str  = "、".join(result.get("work_items", ["（未能判讀）"]))
    status_str = status_map.get(result.get("status", ""), "")
    reply_text = (
        f"📋 已記錄\n"
        f"工項：{items_str}\n"
        f"區域：{result.get('location', '—')}\n"
        f"狀態：{status_str}"
    )

    async with AsyncApiClient(line_config) as client:
        api = AsyncMessagingApi(client)
        await api.reply_message(ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text=reply_text)]
        ))


# ── Webhook 端點 ──────────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    body      = await request.body()
    signature = request.headers.get("x-line-signature", "")

    if not validate_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    data = json.loads(body)
    for event in data.get("events", []):
        if event.get("type") != "message":
            continue
        if event["message"]["type"] == "text":
            background_tasks.add_task(
                process_text_event, event, event.get("replyToken", "")
            )

    return {"status": "ok"}


# ── 健康確認 ──────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "running"}
