import requests
import os
import hashlib
import xml.etree.ElementTree as ET
from deep_translator import GoogleTranslator

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Google News search (Iran security related)
FEED_URL = "https://news.google.com/rss/search?q=iran+explosion+attack+missile&hl=en-US&gl=US&ceid=US:en"

STATE_FILE = "seen_hashes.txt"

KEYWORDS = [
    "explosion","attack","missile","drone",
    "clash","fire","strike","airstrike"
]

# ===============================
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": msg,
        "disable_web_page_preview": False
    }, timeout=30)

# ===============================
def translate(text):
    try:
        return GoogleTranslator(source="auto", target="ar").translate(text)
    except:
        return text

# ===============================
def load_seen():
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE, "r") as f:
        return set(f.read().splitlines())

def save_seen(data):
    with open(STATE_FILE, "w") as f:
        f.write("\n".join(data))

# ===============================
def important(text):
    text = text.lower()
    return any(k in text for k in KEYWORDS)

# ===============================
def make_hash(text):
    return hashlib.md5(text.encode()).hexdigest()

# ===============================
def fetch_events():
    r = requests.get(FEED_URL, timeout=30)
    r.raise_for_status()

    root = ET.fromstring(r.content)
    items = root.findall(".//item")

    events = []
    for item in items:
        title = item.findtext("title", "")
        link = item.findtext("link", "")
        pub = item.findtext("pubDate", "")

        events.append({
            "title": title,
            "link": link,
            "time": pub
        })

    return events

# ===============================
def main():
    seen = load_seen()
    new_seen = set(seen)

    events = fetch_events()

    for ev in events:
        full_text = ev["title"]
        h = make_hash(full_text)

        if h in seen:
            continue

        if not important(full_text):
            continue

        title_ar = translate(ev["title"])

        msg = f"""
🚨 تنبيه مباشر

📰 الخبر:
{title_ar}

🕒 الوقت:
{ev["time"]}

🔗 المصدر:
{ev["link"]}
"""

        send_telegram(msg)
        new_seen.add(h)

    save_seen(new_seen)

# ===============================
if __name__ == "__main__":
    main()
