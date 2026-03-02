import requests
import json
import os
import hashlib
from deep_translator import GoogleTranslator

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

FEED_URL = "https://iran.liveuamap.com/en/data.json"
STATE_FILE = "seen_hashes.json"

# كلمات تشغيل مهمة (تقدر تزيد/تنقص)
KEYWORDS = [
    "explosion","attack","missile","drone",
    "clash","fire","alert","security",
    "strike","airstrike","shelling"
]

def send_telegram(msg: str):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID in environment secrets.")
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }, timeout=30)

def translate(text: str) -> str:
    if not text:
        return ""
    try:
        return GoogleTranslator(source="auto", target="ar").translate(text)
    except Exception:
        return text

def load_seen():
    if not os.path.exists(STATE_FILE):
        return []
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_seen(data):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

def important(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in KEYWORDS)

def make_hash(text: str) -> str:
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()

def fetch_events():
    r = requests.get(FEED_URL, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("features", [])

def main():
    seen = load_seen()
    events = fetch_events()
    new_seen = list(seen)

    for ev in events:
        props = ev.get("properties", {}) or {}

        title = props.get("title", "") or ""
        desc = props.get("description", "") or ""
        time = props.get("time", "") or ""
        link = props.get("url", "https://iran.liveuamap.com") or "https://iran.liveuamap.com"

        full_text = f"{title} {desc}".strip()
        h = make_hash(full_text)

        # منع تكرار
        if h in seen:
            continue

        # فلترة ذكية
        if not important(full_text):
            continue

        # ترجمة كاملة
        title_ar = translate(title)
        desc_ar = translate(desc)

        msg = (
            "🚨 تنبيه مباشر – LiveUAMap (Smart)\n\n"
            f"📰 الخبر:\n{title_ar}\n\n"
            f"📄 التفاصيل:\n{desc_ar}\n\n"
            f"🕒 الوقت:\n{time}\n\n"
            f"🔗 المصدر:\n{link}\n"
        )

        send_telegram(msg)
        new_seen.append(h)

    # خفّف حجم الحالة
    save_seen(new_seen[-300:])

if __name__ == "__main__":
    main()
