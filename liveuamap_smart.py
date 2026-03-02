import requests
import json
import os
import hashlib
from deep_translator import GoogleTranslator

# ===============================
# Telegram settings
# ===============================
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ===============================
# LiveUAMap feed
# ===============================
FEED_URL = "https://iran.liveuamap.com/en/data.json"
STATE_FILE = "seen_hashes.json"

# ===============================
# Smart keywords (important news only)
# ===============================
KEYWORDS = [
    "explosion", "attack", "missile", "drone",
    "clash", "fire", "alert", "security",
    "strike", "airstrike", "shelling"
]

# ===============================
# Telegram sender
# ===============================
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }, timeout=30)

# ===============================
# Translation
# ===============================
def translate(text):
    if not text:
        return ""
    try:
        return GoogleTranslator(source="auto", target="ar").translate(text)
    except:
        return text

# ===============================
# Load saved hashes
# ===============================
def load_seen():
    if not os.path.exists(STATE_FILE):
        return []
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []

def save_seen(data):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

# ===============================
# Smart filter
# ===============================
def important(text):
    text = (text or "").lower()
    return any(k in text for k in KEYWORDS)

# ===============================
# Hash generator
# ===============================
def make_hash(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()

# ===============================
# Fetch events (FIX 403 ERROR)
# ===============================
def fetch_events():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": "https://iran.liveuamap.com/",
    }

    r = requests.get(FEED_URL, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("features", [])

# ===============================
# Main
# ===============================
def main():
    seen = load_seen()
    events = fetch_events()

    new_seen = list(seen)

    for ev in events:
        props = ev.get("properties", {}) or {}

        title = props.get("title", "")
        desc = props.get("description", "")
        time = props.get("time", "")
        link = props.get("url", "https://iran.liveuamap.com")

        full_text = f"{title} {desc}".strip()
        h = make_hash(full_text)

        # prevent duplicates
        if h in seen:
            continue

        # smart filtering
        if not important(full_text):
            continue

        # translate
        title_ar = translate(title)
        desc_ar = translate(desc)

        msg = (
            "🚨 تنبيه مباشر – LiveUAMap (Smart)\n\n"
            f"📰 الخبر:\n{title_ar}\n\n"
            f"📄 التفاصيل:\n{desc_ar}\n\n"
            f"🕒 الوقت:\n{time}\n\n"
            f"🔗 المصدر:\n{link}"
        )

        send_telegram(msg)
        new_seen.append(h)

    save_seen(new_seen[-300:])

# ===============================
if __name__ == "__main__":
    main()
