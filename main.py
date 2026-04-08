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

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}]
        )

        reply = response.content[0].text

        # Notion登録キーワードが含まれていたら自動で構造化して登録
        if NOTION_API_KEY and NOTION_PAPER_DB_ID and ("notion" in user_message.lower() or "登録" in user_message):
            extract_response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=500,
                system="あなたはデータ抽出専門のAIです。与えられたテキストから論文情報を抽出してJSON形式のみで返してください。他の文章は一切含めないこと。形式: {\"title\": \"論文タイトル\", \"summary\": \"3行要約\", \"score\": 数値}",
                messages=[{"role": "user", "content": f"以下のテキストから論文情報を抽出してください:\n{reply}"}]
            )
            try:
                extracted = json.loads(extract_response.content[0].text)
                title = extracted.get("title", "")
                summary = extracted.get("summary", "")
                score = int(extracted.get("score", 5))
                if title:
                    add_to_notion_paper_db(title, summary, score)
                    reply += "\n\n✅ Notionの論文・知識DBに登録しました"
            except Exception as e:
                print(f"Extraction error: {e}")

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
