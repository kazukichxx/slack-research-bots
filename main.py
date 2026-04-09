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
NOTION_GAP_DB_ID = os.environ.get("NOTION_GAP_DB_ID")
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
    try:
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode())
            if not result.get("ok"):
                print(f"Slack error: {result.get('error')}")
    except Exception as e:
        print(f"Slack send error: {e}")


def notion_api_post(endpoint, payload):
    if not NOTION_API_KEY:
        return None
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"https://api.notion.com/v1/{endpoint}",
        data=data,
        headers={
            "Authorization": f"Bearer {NOTION_API_KEY}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28"
        }
    )
    try:
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode())
            print(f"Notion created: {result.get('id')}")
            return result
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"Notion HTTP error: {e.code} - {body}")
    except Exception as e:
        print(f"Notion error: {e}")
    return None


def format_db_id(raw_id):
    db_id = raw_id.replace("-", "")
    return f"{db_id[0:8]}-{db_id[8:12]}-{db_id[12:16]}-{db_id[16:20]}-{db_id[20:]}"


def add_to_notion_paper_db(title, summary, score=5, author="", url="", year=None, tags=None, insight=""):
    if not NOTION_PAPER_DB_ID:
        return
    valid_tags = ["プロセスマイニング", "デジタルツイン", "因果推論", "ベイジアンネットワーク", "医療", "製造業", "シミュレーション", "XAI"]
    properties = {
        "タイトル": {"title": [{"text": {"content": title[:2000]}}]},
        "3行要約": {"rich_text": [{"text": {"content": summary[:2000]}}]},
        "重要度スコア": {"number": score},
        "ステータス": {"status": {"name": "未読"}}
    }
    if author:
        properties["著者"] = {"rich_text": [{"text": {"content": author[:2000]}}]}
    if url and url.startswith("http"):
        properties["URL"] = {"url": url}
    if year:
        try:
            properties["発行年"] = {"number": int(year)}
        except:
            pass
    if tags and isinstance(tags, list):
        filtered = [{"name": t} for t in tags if t in valid_tags]
        if filtered:
            properties["タグ"] = {"multi_select": filtered}
    if insight:
        properties["洞察・仮説"] = {"rich_text": [{"text": {"content": insight[:2000]}}]}
    notion_api_post("pages", {
        "parent": {"database_id": format_db_id(NOTION_PAPER_DB_ID)},
        "properties": properties
    })


def add_to_notion_gap_db(title, rq="", limitation="", approach="", priority="高", tags=None):
    if not NOTION_GAP_DB_ID:
        return
    valid_tags = ["プロセスマイニング", "デジタルツイン", "因果推論", "ベイジアンネットワーク", "医療", "製造業", "XAI"]
    properties = {
        "ギャップタイトル": {"title": [{"text": {"content": title[:2000]}}]},
        "ステータス": {"select": {"name": "特定済"}},
        "優先度": {"select": {"name": priority if priority in ["高", "中", "低"] else "中"}}
    }
    if rq:
        properties["RQ（リサーチクエスチョン）"] = {"rich_text": [{"text": {"content": rq[:2000]}}]}
    if limitation:
        properties["既存研究の限界"] = {"rich_text": [{"text": {"content": limitation[:2000]}}]}
    if approach:
        properties["提案アプローチ"] = {"rich_text": [{"text": {"content": approach[:2000]}}]}
    if tags and isinstance(tags, list):
        filtered = [{"name": t} for t in tags if t in valid_tags]
        if filtered:
            properties["関連タグ"] = {"multi_select": filtered}
    notion_api_post("pages", {
        "parent": {"database_id": format_db_id(NOTION_GAP_DB_ID)},
        "properties": properties
    })


def extract_and_register_notion(user_message, reply, db_type):
    if db_type == "paper":
        system = '与えられたテキストから論文情報を抽出し、JSONのみ返してください。必ずオブジェクト形式で。形式: {"title":"","summary":"","score":5,"author":"","url":"","year":null,"tags":[],"insight":""}'
    else:
        system = '与えられたテキストからリサーチギャップ情報を抽出し、JSONのみ返してください。複数ある場合も必ず単一オブジェクトで最重要の1件のみ返すこと。有効タグ:["プロセスマイニング","デジタルツイン","因果推論","ベイジアンネットワーク","医療","製造業","XAI"]。形式: {"title":"ギャップタイトル","rq":"リサーチクエスチョン","limitation":"既存研究の限界","approach":"提案アプローチ","priority":"高","tags":[]}'

    extract_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=system,
        messages=[{"role": "user", "content": f"以下から情報を抽出:\n{reply[:3000]}"}]
    )
    raw = extract_response.content[0].text.strip()

    # コードブロック除去
    if "```" in raw:
        for part in raw.split("```"):
            part = part.strip().lstrip("json").strip()
            if part.startswith("{") or part.startswith("["):
                raw = part
                break

    # 配列の場合は最初の要素を取得
    raw = raw.strip()
    if raw.startswith("["):
        try:
            arr = json.loads(raw)
            if arr and isinstance(arr, list):
                raw = json.dumps(arr[0])
        except:
            pass

    try:
        extracted = json.loads(raw.strip())
        if db_type == "paper":
            title = extracted.get("title", "")
            if title:
                add_to_notion_paper_db(
                    title,
                    extracted.get("summary", ""),
                    int(extracted.get("score", 5)),
                    extracted.get("author", "") or "",
                    extracted.get("url", "") or "",
                    extracted.get("year"),
                    extracted.get("tags", []),
                    extracted.get("insight", "") or ""
                )
                return True
        else:
            title = extracted.get("title", "")
            if title:
                add_to_notion_gap_db(
                    title,
                    extracted.get("rq", ""),
                    extracted.get("limitation", ""),
                    extracted.get("approach", ""),
                    extracted.get("priority", "高"),
                    extracted.get("tags", [])
                )
                return True
    except Exception as e:
        print(f"JSON parse error: {e}, raw: {raw[:200]}")
    return False


def handle_event(event, event_id):
    try:
        user_message = event.get("text", "")
        channel = event.get("channel")

        tools = [{"type": "web_search_20250305", "name": "web_search"}]
        messages = [{"role": "user", "content": user_message}]

        while True:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2000,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages
            )
            reply_parts = []
            tool_uses = []
            for block in response.content:
                if hasattr(block, "text"):
                    reply_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_uses.append(block)

            if not tool_uses or response.stop_reason == "end_turn":
                break

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

        should_register = NOTION_API_KEY and "登録" in user_message and len(reply) > 50

        if should_register:
            is_darwin = any(kw in SYSTEM_PROMPT for kw in ["ストラテジスト", "Darwin", "ダーウィン"])
            if is_darwin and NOTION_GAP_DB_ID:
                success = extract_and_register_notion(user_message, reply, "gap")
                if success:
                    reply += "\n\n✅ Notionのリサーチギャップに登録しました"
            elif NOTION_PAPER_DB_ID and any(kw in user_message for kw in ["論文", "paper"]):
                success = extract_and_register_notion(user_message, reply, "paper")
                if success:
                    reply += "\n\n✅ Notionの論文・知識DBに登録しました"

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
