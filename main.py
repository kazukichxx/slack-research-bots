import os
import hmac
import hashlib
import json
import threading
from flask import Flask, request, jsonify
import anthropic

app = Flask(__name__)

SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
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
    import urllib.request
    payload = json.dumps({"channel": channel, "text": text}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json"
        }
    )
    urllib.request.urlopen(req)

def handle_event(event, event_id):
    try:
        user_message = event.get("text", "")
        channel = event.get("channel")
        print(f"Calling Claude API for channel: {channel}")
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}]
        )
        print(f"Claude responded, sending to Slack...")
        send_slack_message(channel, response.content[0].text)
        print(f"Message sent successfully")
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")

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
