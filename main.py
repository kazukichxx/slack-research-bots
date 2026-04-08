import os
import hmac
import hashlib
import json
import threading
import urllib.request
import urllib.error
from flask import Flask, request, jsonify
import anthropic

app = Flask(__name__)

SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
NOTION_API_KEY = os.environ.get("NOTION_API_KEY")
NOTION_PAPER_DB_ID = os.environ.get("NOTION_PAPER_DB_ID")
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "You are a helpful assistant.")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

processed_events = set()
lock = threading.Lock()

def verify_slack_signature(req):
    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")
    signature = req.headers.get("X-Slack-Signature", "")
    body = req.get_data(as_text=True)
    sig_basestring = f"v0:{timestamp}:{body}"
    computed = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed, signature)

def send_slack_message(channel, text):
    payload = json.dumps({"channel": channel, "text": text}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json"
        }
    )
    with urllib.request.urlopen(req) as response:
        result = json.loads(response.read().decode())
        if not result.get("ok"):
            print(f"Slack error: {result.get('error')}")

def add_to_notion_paper_db(title, summary, score=5):
    if not NOTION_API_KEY or not NOTION_PAPER_DB_ID:
        return
    payload = json.dumps({
        "parent": {"database_id": NOTION_PAPER_DB_ID},
        "properties": {
            "タイトル": {
                "title": [{"text": {"content": title}}]
            },
            "3行要約": {
                "rich_text": [{"text": {"content": summary}}]
            },
            "重要度スコア": {
                "number": score
            },
            "ステータス": {
                "select": {"name": "要約済"}
            }
        }
    }).encode()
    req = urllib.request.Request(
        "https://api.notion.com/v1/pages",
        data=payload,
        headers={
            "Authorization": f"Bearer {NOTION_API_KEY}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28"
        }
    )
    try:
        with urllib.request.urlopen(req) as response:
            print(f"Notion page created successfully")
    except Exception as e:
        print(f"Notion error: {e}")
        
def handle_event(event, event_id):
    try:
        user_message = event.get("text", "")
        channel = event.get("channel")

        # web検索ツールを設定
        tools = [{"type": "web_search_20250305", "name": "web_search"}]

        messages = [{"role": "user", "content": user_message}]
        
        # ツール使用ループ
        while True:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2000,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages
            )
            
            # テキストブロックを収集
            reply_parts = []
            tool_uses = []
            for block in response.content:
                if hasattr(block, "text"):
                    reply_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_uses.append(block)
            
            # ツール使用がなければ終了
            if not tool_uses or response.stop_reason == "end_turn":
                break
                
            # ツール結果をメッセージに追加して継続
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for tool_use in tool_uses:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": "検索を実行しました"
                })
            messages.append({"role": "user", "content": tool_results})

        reply = "\n".join(reply_parts) if reply_parts else "処理しました"

        # Notion登録処理
        if NOTION_API_KEY and NOTION_PAPER_DB_ID and ("notion" in user_message.lower() or "登録" in user_message):
            if len(reply) > 50:
                extract_response = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=500,
                    system='あなたはJSON抽出専門AIです。与えられたテキストから論文情報を抽出し、必ずJSONのみを返してください。マークダウンや説明文は一切含めないこと。形式: {"title": "論文タイトル", "summary": "3行要約", "score": 数値}',
                    messages=[{"role": "user", "content": f"以下から論文情報を抽出:\n{reply[:2000]}"}]
                )
                raw = extract_response.content[0].text.strip()
                # JSONブロックの抽出
                if "```" in raw:
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                try:
                    extracted = json.loads(raw.strip())
                    title = extracted.get("title", "")
                    summary = extracted.get("summary", "")
                    score = int(extracted.get("score", 5))
                    if title:
                        add_to_notion_paper_db(title, summary, score)
                        reply += "\n\n✅ Notionの論文・知識DBに登録しました"
                except Exception as e:
                    print(f"JSON parse error: {e}, raw: {raw[:200]}")

        send_slack_message(channel, reply)

    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        send_slack_message(event.get("channel"), f"エラーが発生しました: {e}")
        
@app.route("/slack/events", methods=["POST"])
def slack_events():
    data = request.json

    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    if not verify_slack_signature(request):
        return jsonify({"error": "Invalid signature"}), 403

    event = data.get("event", {})
    event_id = data.get("event_id", "")

    with lock:
        if event_id in processed_events:
            return jsonify({"status": "duplicate"}), 200
        processed_events.add(event_id)

    if event.get("type") == "app_mention" and not event.get("bot_id"):
        thread = threading.Thread(target=handle_event, args=(event, event_id))
        thread.start()

    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
