import os
import re
import json
import time
import hashlib
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from deep_translator import GoogleTranslator

# =========================
# Secrets
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")

STATE_ISSUE_TITLE = "osint-alerts-state"
MAX_ALERTS_PER_RUN = 15

# Saudi filter
SAUDI_TERMS = [
    "saudi", "saudi arabia", "ksa",
    "السعودية", "المملكة العربية السعودية",
    "الرياض", "جدة", "الدمام", "مكة", "المدينة"
]

# =========================
def must_have_env():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing Telegram secrets.")
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        raise RuntimeError("Missing GitHub env variables.")

def norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()

def contains_saudi(text):
    t = (text or "").lower()
    return any(k.lower() in t for k in SAUDI_TERMS)

def make_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()

def translate_ar(text):
    try:
        return GoogleTranslator(source="auto", target="ar").translate(text)
    except:
        return text

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "disable_web_page_preview": True
    }, timeout=30)

# =========================
# GitHub state
# =========================
def gh_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

def get_state_issue():
    owner, repo = GITHUB_REPOSITORY.split("/", 1)

    r = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/issues",
        headers=gh_headers(),
        params={"state": "open"},
        timeout=30
    )
    r.raise_for_status()
    issues = r.json()

    for i in issues:
        if i["title"] == STATE_ISSUE_TITLE:
            return i["number"], i.get("body") or ""

    r = requests.post(
        f"https://api.github.com/repos/{owner}/{repo}/issues",
        headers=gh_headers(),
        json={"title": STATE_ISSUE_TITLE, "body": json.dumps({"seen": []})},
        timeout=30
    )
    r.raise_for_status()
    created = r.json()
    return created["number"], created.get("body") or ""

def load_seen():
    num, body = get_state_issue()
    try:
        data = json.loads(body)
        return num, set(data.get("seen", []))
    except:
        return num, set()

def save_seen(issue_number, seen):
    owner, repo = GITHUB_REPOSITORY.split("/", 1)
    body = json.dumps({"seen": list(seen)[-3000:]})
    requests.patch(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}",
        headers=gh_headers(),
        json={"body": body},
        timeout=30
    )

# =========================
# Google News Saudi only
# =========================
def fetch_news():
    url = "https://news.google.com/rss/search"
    params = {
        "q": '"Saudi Arabia" OR السعودية',
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en"
    }

    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()

    root = ET.fromstring(r.content)
    items = root.findall(".//item")

    news = []
    for item in items:
        news.append({
            "title": norm(item.findtext("title", "")),
            "desc": norm(re.sub(r"<[^>]+>", "", item.findtext("description", ""))),
            "link": norm(item.findtext("link", "")),
            "time": norm(item.findtext("pubDate", ""))
        })

    return news

# =========================
def is_today(pub_date):
    try:
        dt = parsedate_to_datetime(pub_date)
        if not dt:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        today = datetime.now(timezone.utc).date()
        return dt.date() == today
    except:
        return False

# =========================
def main():
    must_have_env()
    issue_number, seen = load_seen()
    new_seen = set(seen)

    events = fetch_news()
    sent = 0

    for e in events:
        if sent >= MAX_ALERTS_PER_RUN:
            break

        full_text = f"{e['title']} {e['desc']}"
        h = make_hash(full_text)

        # Only today
        if not is_today(e["time"]):
            continue

        # Must mention Saudi
        if not contains_saudi(full_text):
            continue

        # No duplicates
        if h in seen:
            continue

        title_ar = translate_ar(e["title"])
        desc_ar = translate_ar(e["desc"])

        msg = f"""🚨 خبر اليوم – السعودية

📰 العنوان:
{title_ar}

📄 التفاصيل:
{desc_ar}

🕒 الوقت:
{e['time']}

🔗 الرابط:
{e['link']}
"""

        send_telegram(msg)
        new_seen.add(h)
        sent += 1

    save_seen(issue_number, new_seen)

if __name__ == "__main__":
    main()
