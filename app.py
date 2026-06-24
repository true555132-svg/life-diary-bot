"""
生活紀錄 LINE Bot
- 文字/圖片都存進 Postgres (diary_entries)
- 被動分析：訊息含「分析/總結/建議/整理/回顧」等關鍵字時，立即回覆 AI 摘要+建議
- 主動分析：APScheduler 每天/每週定時 push 摘要給所有曾互動過的使用者
"""
import os, time, threading, json
from flask import Flask, request, abort, jsonify, render_template_string

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, PushMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent

import db
import analyzer

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "diary-images")
ADMIN_KEY = os.getenv("ADMIN_KEY", "changeme")
TZ_OFFSET_HOURS = 8  # Asia/Taipei

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

db.init_db()

ANALYZE_KEYWORDS = ["分析", "總結", "建議", "整理", "回顧", "幫我看", "幫我看看"]
PERIOD_KEYWORDS = [
    (["今天", "今日", "本日"], 1, "今天"),
    (["這週", "本週", "這周", "本周", "七天", "一週", "一周"], 7, "這週"),
    (["上週", "上周"], 14, "最近兩週"),  # 簡化：抓兩週讓 AI 自己抓上週
    (["這個月", "本月", "三十天", "30天"], 30, "這個月"),
]
DEFAULT_PERIOD_DAYS = 7
DEFAULT_PERIOD_LABEL = "最近七天"


def upload_image_to_supabase(filename: str, data: bytes, content_type: str = "image/jpeg") -> str:
    if not SUPABASE_SERVICE_KEY:
        return ""
    import urllib.request
    try:
        url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{filename}"
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": content_type,
            "x-upsert": "true",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{filename}"
    except Exception as e:
        print(f"[Supabase Upload Error] {e}")
        return ""


def now_str():
    return time.strftime("%Y/%m/%d %H:%M:%S", time.gmtime(time.time() + TZ_OFFSET_HOURS * 3600))


def detect_period(text: str):
    for keywords, days, label in PERIOD_KEYWORDS:
        if any(k in text for k in keywords):
            return days, label
    return DEFAULT_PERIOD_DAYS, DEFAULT_PERIOD_LABEL


def is_analyze_command(text: str) -> bool:
    return any(k in text for k in ANALYZE_KEYWORDS)


def reply_text(reply_token: str, text: str):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)])
        )


def push_text(user_id: str, text: str):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=user_id, messages=[TextMessage(text=text)])
        )


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        print(f"[Webhook Error] {e}")
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    if is_analyze_command(text):
        days, label = detect_period(text)
        entries = db.get_entries(user_id, days)
        summary = analyzer.analyze(entries, label)
        reply_text(event.reply_token, summary)
        return

    db.insert_entry(user_id, "text", content=text)
    reply_text(event.reply_token, "已記錄 📝")


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    import urllib.request
    user_id = event.source.user_id
    msg_id = event.message.id
    image_url = ""
    try:
        dl_url = f"https://api-data.line.me/v2/bot/message/{msg_id}/content"
        req = urllib.request.Request(dl_url, headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
        filename = f"{int(time.time())}_{msg_id}.jpg"
        image_url = upload_image_to_supabase(filename, data)
    except Exception as e:
        print(f"[Image Download Error] {e}")

    caption = analyzer.describe_image(image_url) if image_url else ""
    db.insert_entry(user_id, "image", content=caption, image_url=image_url)
    reply_text(event.reply_token, f"已記錄這張照片 📷\n{caption}" if caption else "已記錄這張照片 📷")


# ── 主動分析（排程） ──────────────────────────────────────

def run_scheduled_analysis(days: int, label: str, header: str):
    user_ids = db.get_distinct_user_ids()
    for uid in user_ids:
        entries = db.get_entries(uid, days)
        if not entries:
            continue
        summary = analyzer.analyze(entries, label)
        try:
            push_text(uid, f"{header}\n\n{summary}")
        except Exception as e:
            print(f"[Push Error] user={uid} {e}")


def daily_job():
    run_scheduled_analysis(1, "今天", "📅 每日小結")


def weekly_job():
    run_scheduled_analysis(7, "這週", "🗒️ 本週回顧與建議")


def start_scheduler():
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        sched = BackgroundScheduler(timezone="Asia/Taipei")
        sched.add_job(daily_job, CronTrigger(hour=21, minute=0))
        sched.add_job(weekly_job, CronTrigger(day_of_week="sun", hour=20, minute=0))
        sched.start()
        print("[Scheduler] started: daily 21:00, weekly Sun 20:00 (Asia/Taipei)")
    except Exception as e:
        print(f"[Scheduler] failed to start: {e}")


start_scheduler()


@app.route("/cron/<job>", methods=["POST", "GET"])
def cron_trigger(job):
    """備援：若排程因服務重啟漏跑，可由外部 cron service 打這個 endpoint。"""
    if request.args.get("key", "") != ADMIN_KEY:
        abort(403)
    if job == "daily":
        threading.Thread(target=daily_job, daemon=True).start()
    elif job == "weekly":
        threading.Thread(target=weekly_job, daemon=True).start()
    else:
        abort(404)
    return jsonify({"ok": True})


# ── 簡單檢視頁 ─────────────────────────────────────────────

VIEW_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>生活紀錄</title>
<style>
body{font-family:-apple-system,sans-serif;background:#f5f6f8;margin:0;padding:16px}
.entry{background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 2px rgba(0,0,0,.08)}
.entry-time{font-size:11px;color:#9aa0a6;margin-bottom:4px}
.entry-content{font-size:14px;color:#1a1a1a;white-space:pre-wrap}
.entry img{max-width:240px;border-radius:8px;display:block;margin-top:6px}
</style></head><body>
<h2>生活紀錄（最近 {{days}} 天）</h2>
{% for e in entries %}
<div class="entry">
  <div class="entry-time">{{e.created_at}}</div>
  <div class="entry-content">{{e.content}}</div>
  {% if e.image_url %}<img src="{{e.image_url}}">{% endif %}
</div>
{% endfor %}
</body></html>"""


@app.route("/view/<user_id>")
def view_entries(user_id):
    if request.args.get("key", "") != ADMIN_KEY:
        abort(403)
    days = int(request.args.get("days", 30))
    entries = db.get_entries(user_id, days)
    entries = list(reversed(entries))
    return render_template_string(VIEW_HTML, entries=entries, days=days)


@app.route("/")
def index():
    return "Life Diary Bot is running."


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5001)))
