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
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # provided by Actions
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")  # provided by Actions, e.g. "user/repo"

# =========================
# Config
# =========================
STATE_ISSUE_TITLE = "osint-alerts-state"
MAX_ALERTS_PER_RUN = 20

# Saudi timezone
KSA_TZ = timezone(timedelta(hours=3))

# Only last 3 hours (KSA time)
MAX_AGE_HOURS = 3

# Must mention Saudi terms
SAUDI_TERMS = [
    "saudi", "saudi arabia", "ksa", "kingdom of saudi arabia",
    "riyadh", "jeddah", "dammam", "dhahran", "neom", "mecca", "makkah", "medina", "madinah",
    "السعودية", "المملكة", "المملكة العربية السعودية",
    "الرياض", "جدة", "الدمام", "الظهران", "نيوم", "مكة", "المدينة"
]

# Google News query (Saudi mentions)
GOOGLE_NEWS_QUERY = '("Saudi" OR "Saudi Arabia" OR KSA OR السعودية OR "المملكة العربية السعودية" OR الرياض OR جدة OR مكة OR المدينة)'

# =========================
def must_have_env():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID secrets.")
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        raise RuntimeError("Missing GITHUB_TOKEN or GITHUB_REPOSITORY environment variables.")

def norm(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s

def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", (s or "")).strip()

def contains_saudi(text: str) -> bool:
    t = (text or "").lower()
    return any(term.lower() in t for term in SAUDI_TERMS)

def sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

def translate_ar(text: str) -> str:
    text = norm(text)
    if not text:
        return ""
    try:
        return GoogleTranslator(source="auto", target="ar").translate(text)
    except Exception:
        return text

def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "disable_web_page_preview": True
        },
        timeout=30
    )
    r.raise_for_status()

# =========================
# GitHub Issue state (prevents duplicates across runs)
# =========================
def gh_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def get_or_create_state_issue():
    owner, repo = GITHUB_REPOSITORY.split("/", 1)

    r = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/issues",
        headers=gh_headers(),
        params={"state": "open", "per_page": 100},
        timeout=30
    )
    r.raise_for_status()
    issues = r.json()

    for it in issues:
        if it.get("title") == STATE_ISSUE_TITLE:
            return it["number"], it.get("body") or ""

    r = requests.post(
        f"https://api.github.com/repos/{owner}/{repo}/issues",
        headers=gh_headers(),
        json={"title": STATE_ISSUE_TITLE, "body": json.dumps({"seen": []}, ensure_ascii=False)},
        timeout=30
    )
    r.raise_for_status()
    created = r.json()
    return created["number"], created.get("body") or ""

def load_seen():
    issue_number, body = get_or_create_state_issue()
    try:
        data = json.loads(body) if body else {}
        seen = set(data.get("seen", []))
    except Exception:
        seen = set()
    return issue_number, seen

def save_seen(issue_number: int, seen: set):
    owner, repo = GITHUB_REPOSITORY.split("/", 1)
    seen_list = list(seen)[-5000:]  # cap
    body = json.dumps(
        {"seen": seen_list, "updated_utc": datetime.utcnow().isoformat()},
        ensure_ascii=False
    )

    r = requests.patch(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}",
        headers=gh_headers(),
        json={"body": body},
        timeout=30
    )
    r.raise_for_status()

# =========================
# Time filter (KSA last 3 hours)
# =========================
def within_last_hours_ksa(pubdate: str, hours: int) -> bool:
    """
    pubdate example: 'Sat, 28 Feb 2026 23:40:00 GMT'
    We parse it, convert to KSA timezone, then compare with now(KSA).
    """
    if not pubdate:
        return False
    try:
        dt = parsedate_to_datetime(pubdate)
        if not dt:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        dt_ksa = dt.astimezone(KSA_TZ)
        now_ksa = datetime.now(KSA_TZ)
        return (now_ksa - dt_ksa) <= timedelta(hours=hours)
    except Exception:
        return False

# =========================
# Fetch Google News RSS
# =========================
def fetch_google_news():
    url = "https://news.google.com/rss/search"
    params = {
        "q": GOOGLE_NEWS_QUERY,
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
        title = norm(item.findtext("title", ""))
        link = norm(item.findtext("link", ""))
        pub = norm(item.findtext("pubDate", ""))
        desc = norm(strip_html(item.findtext("description", "")))

        events.append({
            "title": title,
            "desc": desc,
            "link": link,
            "time": pub
        })
    return events

# =========================
def main():
    must_have_env()

    issue_number, seen = load_seen()
    new_seen = set(seen)

    events = fetch_google_news()

    sent = 0
    for e in events:
        if sent >= MAX_ALERTS_PER_RUN:
            break

        title = e.get("title", "")
        desc = e.get("desc", "")
        pub = e.get("time", "")
        link = e.get("link", "")

        full_text = f"{title} {desc}".strip()

        # Must mention Saudi
        if not contains_saudi(full_text):
            continue

        # Only last 3 hours in KSA time
        if not within_last_hours_ksa(pub, MAX_AGE_HOURS):
            continue

        # Unique hash per item (use title+link)
        h = sha(f"{title} | {link}")

        # Prevent duplicates across runs
        if h in seen:
            continue

        # Translate full
        title_ar = translate_ar(title)
        desc_ar = translate_ar(desc)

        # Show time in KSA
        try:
            dt = parsedate_to_datetime(pub)
            if dt and dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            pub_ksa = dt.astimezone(KSA_TZ).strftime("%Y-%m-%d %H:%M KSA")
        except Exception:
            pub_ksa = pub

        msg = (
            "🚨 تنبيه خبر – السعودية (آخر 3 ساعات)\n\n"
            f"📰 العنوان:\n{title_ar}\n\n"
            f"📄 التفاصيل:\n{desc_ar}\n\n"
            f"🕒 الوقت:\n{pub_ksa}\n\n"
            f"🔗 المصدر:\n{link}"
        )

        send_telegram(msg)
        new_seen.add(h)
        sent += 1

        time.sleep(0.6)

    save_seen(issue_number, new_seen)

if __name__ == "__main__":
    main()
