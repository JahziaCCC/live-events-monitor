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
# REQUIRED SECRETS
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# GitHub Actions built-ins
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")  # e.g. "user/repo"

# =========================
# CONFIG
# =========================
STATE_ISSUE_TITLE = "osint-alerts-state"   # we store seen hashes here

# Only send items within last X hours
MAX_AGE_HOURS = 6

# Max alerts per run
MAX_ALERTS_PER_RUN = 10

# MUST mention Saudi Arabia (hard filter)
SAUDI_TERMS = [
    "saudi", "saudi arabia", "ksa", "kingdom of saudi arabia",
    "riyadh", "jeddah", "dammam", "dhahran", "neom", "mecca", "makkah", "medina", "madinah",
    "السعودية", "المملكة", "المملكة العربية السعودية",
    "الرياض", "جدة", "الدمام", "الظهران", "نيوم", "مكة", "المدينة"
]

# Optional: Keep a light "event" filter to reduce noise (you can remove if you want ALL Saudi mentions)
EVENT_KEYWORDS = [
    "explosion", "attack", "missile", "drone", "strike", "airstrike", "shelling",
    "clash", "fire", "blast", "intercept", "sirens", "military", "navy",
    "oil spill", "tanker", "ship seized", "seized", "red sea",
    "flood", "storm", "earthquake", "wildfire",
    "انفجار", "هجوم", "صاروخ", "مسيرة", "ضربة", "اشتباك", "حريق",
    "احتجاز", "سفينة", "ناقلة", "تسرب", "فيضان", "سيول", "زلزال", "حرائق"
]

# Google News RSS queries (Saudi only)
GOOGLE_NEWS_QUERIES = [
    '("Saudi Arabia" OR Saudi OR KSA OR السعودية OR "المملكة العربية السعودية")',
]

# GDELT query (Saudi only)
GDELT_QUERY = (
    '("Saudi Arabia" OR Saudi OR KSA OR السعودية OR "المملكة العربية السعودية" OR Riyadh OR Jeddah OR NEOM) '
    'AND (attack OR explosion OR missile OR drone OR strike OR airstrike OR shelling OR fire OR flood OR earthquake OR tanker OR seized)'
)

# GDELT time window (minutes)
GDELT_WINDOW_MIN = 60

# =========================
# Helpers
# =========================
def must_have_env():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID secrets.")
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        raise RuntimeError("Missing GITHUB_TOKEN or GITHUB_REPOSITORY in Actions environment.")

def norm_text(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s

def contains_saudi(text: str) -> bool:
    t = (text or "").lower()
    return any(term.lower() in t for term in SAUDI_TERMS)

def contains_event_keyword(text: str) -> bool:
    t = (text or "").lower()
    return any(k.lower() in t for k in EVENT_KEYWORDS)

def make_hash(*parts: str) -> str:
    base = " | ".join(norm_text(p) for p in parts if p)
    return hashlib.sha256(base.encode("utf-8")).hexdigest()

def translate_ar(text: str) -> str:
    text = norm_text(text)
    if not text:
        return ""
    try:
        return GoogleTranslator(source="auto", target="ar").translate(text)
    except Exception:
        return text

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True
        },
        timeout=30
    ).raise_for_status()

# =========================
# GitHub Issue State Storage (No duplicates across runs)
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

def load_seen_hashes():
    number, body = get_or_create_state_issue()
    try:
        data = json.loads(body) if body else {}
        seen = set(data.get("seen", []))
    except Exception:
        seen = set()
    return number, seen

def save_seen_hashes(issue_number: int, seen: set):
    owner, repo = GITHUB_REPOSITORY.split("/", 1)
    seen_list = list(seen)[-3000:]
    body = json.dumps({"seen": seen_list, "updated_utc": datetime.utcnow().isoformat()}, ensure_ascii=False)
    r = requests.patch(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}",
        headers=gh_headers(),
        json={"body": body},
        timeout=30
    )
    r.raise_for_status()

# =========================
# Sources
# =========================
def fetch_google_news_rss(query: str):
    url = "https://news.google.com/rss/search"
    params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}

    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()

    root = ET.fromstring(r.content)
    items = root.findall(".//item")
    out = []

    for item in items:
        title = norm_text(item.findtext("title", ""))
        link = norm_text(item.findtext("link", ""))
        pub = norm_text(item.findtext("pubDate", ""))
        desc = norm_text(item.findtext("description", ""))

        desc = re.sub(r"<[^>]+>", "", desc).strip()

        out.append({
            "source": "GoogleNews",
            "title": title,
            "desc": desc,
            "link": link,
            "time": pub,
        })
    return out

def fetch_gdelt():
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=GDELT_WINDOW_MIN)

    def fmt(dt):
        return dt.strftime("%Y%m%d%H%M%S")

    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    params = {
        "query": GDELT_QUERY,
        "mode": "ArtList",
        "format": "json",
        "startdatetime": fmt(start),
        "enddatetime": fmt(end),
        "maxrecords": 50,
        "sourcelang": "English"
    }

    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()

    out = []
    for art in data.get("articles", []) or []:
        out.append({
            "source": "GDELT",
            "title": norm_text(art.get("title", "")),
            "desc": norm_text(art.get("snippet", "")),
            "link": norm_text(art.get("url", "")),
            "time": norm_text(art.get("seendate", "")),
        })
    return out

# =========================
# Main
# =========================
def within_time_window(time_str: str) -> bool:
    """
    Filters out old news. Works well for Google News RSS pubDate.
    If parsing fails, we allow it (to avoid missing).
    """
    if not time_str:
        return True
    try:
        dt = parsedate_to_datetime(time_str)
        if not dt:
            return True
        # Ensure UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        return age_hours <= MAX_AGE_HOURS
    except Exception:
        return True

def main():
    must_have_env()

    issue_number, seen = load_seen_hashes()
    new_seen = set(seen)

    events = []

    # Google News
    for q in GOOGLE_NEWS_QUERIES:
        try:
            events.extend(fetch_google_news_rss(q))
            time.sleep(1)
        except Exception:
            continue

    # GDELT
    try:
        events.extend(fetch_gdelt())
    except Exception:
        pass

    # Deduplicate within run
    unique = []
    local_seen = set()
    for e in events:
        h = make_hash(e["source"], e.get("title", ""), e.get("link", ""))
        if h in local_seen:
            continue
        local_seen.add(h)
        e["_hash"] = h
        unique.append(e)

    sent = 0
    for e in unique:
        if sent >= MAX_ALERTS_PER_RUN:
            break

        title = e.get("title", "")
        desc = e.get("desc", "")
        full_text = f"{title} {desc}".strip()

        # 1) MUST mention Saudi
        if not contains_saudi(full_text):
            continue

        # 2) Time filter (avoid old)
        if not within_time_window(e.get("time", "")):
            continue

        # 3) Optional event filter (remove هذه الشرط إذا تبي كل خبر فيه السعودية حتى لو اقتصادي/رياضة)
        if not contains_event_keyword(full_text):
            continue

        # 4) No duplicates across runs
        if e["_hash"] in seen:
            continue

        # Translate full (title + desc)
        title_ar = translate_ar(title)
        desc_ar = translate_ar(desc)

        msg = (
            "🚨 تنبيه خبر (السعودية فقط)\n"
            f"🗞 المصدر: {e.get('source','')}\n\n"
            f"📰 العنوان:\n{title_ar}\n\n"
            f"📄 التفاصيل:\n{desc_ar}\n\n"
            f"🕒 الوقت:\n{e.get('time','')}\n\n"
            f"🔗 الرابط:\n{e.get('link','')}"
        )

        send_telegram(msg)
        new_seen.add(e["_hash"])
        sent += 1

    save_seen_hashes(issue_number, new_seen)

if __name__ == "__main__":
    main()
