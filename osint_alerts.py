import os
import re
import json
import time
import hashlib
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
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

# Saudi timezone
KSA_TZ = timezone(timedelta(hours=3))
MAX_AGE_HOURS = 3
MAX_ALERTS_PER_RUN = 20

# Must mention Saudi
SAUDI_TERMS = [
    "saudi", "saudi arabia", "ksa",
    "السعودية", "المملكة العربية السعودية",
    "الرياض", "جدة", "مكة", "المدينة"
]

# WAR ONLY
WAR_KEYWORDS = [
    "attack", "missile", "drone", "strike", "airstrike",
    "explosion", "clash", "military", "shelling",
    "intercept", "rocket", "navy", "armed",
    "هجوم", "صاروخ", "مسيرة", "ضربة",
    "انفجار", "اشتباك", "عسكري", "قصف", "اعتراض"
]

GOOGLE_QUERY = (
    '("Saudi" OR "Saudi Arabia" OR السعودية OR الرياض OR جدة) '
    'AND (attack OR missile OR drone OR strike OR explosion OR military OR rocket)'
)

# =========================
def norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()

def strip_html(s):
    return re.sub(r"<[^>]+>", "", (s or "")).strip()

def contains_any(text, words):
    t = (text or "").lower()
    return any(w.lower() in t for w in words)

def sha(text):
    return hashlib.sha256((text or "").encode()).hexdigest()

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
# GitHub state memory
# =========================
def gh_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

def get_state():
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
            return i["number"], json.loads(i.get("body") or "{}")

    r = requests.post(
        f"https://api.github.com/repos/{owner}/{repo}/issues",
        headers=gh_headers(),
        json={"title": STATE_ISSUE_TITLE, "body": json.dumps({"seen": []})},
        timeout=30
    )
    r.raise_for_status()
    created = r.json()
    return created["number"], {"seen": []}

def save_state(issue_number, seen):
    owner, repo = GITHUB_REPOSITORY.split("/", 1)
    body = json.dumps({"seen": list(seen)[-5000:]})
    requests.patch(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}",
        headers=gh_headers(),
        json={"body": body},
        timeout=30
    )

# =========================
def within_last_3_hours(pub):
    try:
        dt = parsedate_to_datetime(pub)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_ksa = dt.astimezone(KSA_TZ)
        now_ksa = datetime.now(KSA_TZ)
        return (now_ksa - dt_ksa) <= timedelta(hours=3)
    except:
        return False

# =========================
def fetch_news():
    url = "https://news.google.com/rss/search"
    params = {
        "q": GOOGLE_QUERY,
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en"
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()

    root = ET.fromstring(r.content)
    items = root.findall(".//item")

    events = []
    for item in items:
        events.append({
            "title": norm(item.findtext("title", "")),
            "desc": norm(strip_html(item.findtext("description", ""))),
            "link": norm(item.findtext("link", "")),
            "time": norm(item.findtext("pubDate", ""))
        })
    return events

# =========================
def main():
    issue_number, state = get_state()
    seen = set(state.get("seen", []))
    new_seen = set(seen)

    events = fetch_news()
    sent = 0

    for e in events:
        if sent >= MAX_ALERTS_PER_RUN:
            break

        full_text = f"{e['title']} {e['desc']}"

        if not contains_any(full_text, SAUDI_TERMS):
            continue

        if not contains_any(full_text, WAR_KEYWORDS):
            continue

        if not within_last_3_hours(e["time"]):
            continue

        h = sha(e["title"] + e["link"])

        if h in seen:
            continue

        title_ar = translate_ar(e["title"])
        desc_ar = translate_ar(e["desc"])

        msg = f"""🚨 تنبيه عسكري – السعودية (آخر 3 ساعات)

📰 العنوان:
{title_ar}

📄 التفاصيل:
{desc_ar}

🕒 الوقت:
{e['time']}

🔗 المصدر:
{e['link']}
"""

        send_telegram(msg)
        new_seen.add(h)
        sent += 1

    save_state(issue_number, new_seen)

if __name__ == "__main__":
    main()
