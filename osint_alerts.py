import os
import re
import json
import time
import hashlib
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from deep_translator import GoogleTranslator

# =========================
# REQUIRED SECRETS (GitHub -> Settings -> Secrets -> Actions)
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# GitHub built-in token + repo info (no need to create PAT)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")  # e.g. "user/repo"

# =========================
# CONFIG
# =========================
STATE_ISSUE_TITLE = "osint-alerts-state"  # we store seen hashes here

# Keywords for "important" filtering (you can expand)
KEYWORDS = [
    # English
    "explosion", "attack", "missile", "drone", "strike", "airstrike", "shelling",
    "clash", "fire", "blast", "intercept", "sirens", "military", "navy",
    "oil spill", "tanker", "ship seized", "seized", "houthi", "red sea",
    "iran", "tehran", "israel", "gaza", "yemen", "iraq", "syria", "lebanon",
    "hormuz", "bab al-mandab", "gulf", "persian gulf",

    # Arabic (if found in titles/snippets)
    "انفجار", "هجوم", "صاروخ", "مسيرة", "ضربة", "اشتباك", "حريق",
    "اعتراض", "استهداف", "احتجاز", "سفينة", "ناقلة", "تسرب", "البحر الأحمر",
    "مضيق هرمز", "باب المندب", "الخليج"
]

# Google News RSS queries (edit as you like)
GOOGLE_NEWS_QUERIES = [
    # Iran
    "iran explosion OR attack OR missile OR drone",
    # Gulf / Hormuz / maritime
    "\"Strait of Hormuz\" OR Hormuz tanker OR ship seized OR Persian Gulf",
    # Red Sea / Bab al-Mandab
    "\"Red Sea\" OR \"Bab al-Mandab\" OR Houthi attack OR shipping",
    # Iraq / Syria / Lebanon / Yemen (optional)
    "iraq attack OR explosion OR drone",
    "syria strike OR airstrike OR explosion",
    "lebanon strike OR missile OR explosion",
    "yemen strike OR missile OR drone OR red sea",
]

# GDELT (free) query - broad OSINT stream
GDELT_QUERY = (
    '(iran OR tehran OR "strait of hormuz" OR hormuz OR "red sea" OR "bab al-mandab" '
    'OR "persian gulf" OR gaza OR yemen OR iraq OR syria OR lebanon) '
    'AND (explosion OR attack OR missile OR drone OR strike OR airstrike OR shelling OR seized OR tanker)'
)

# Time window for GDELT (minutes)
GDELT_WINDOW_MIN = 60

# How many items max to notify per run (anti-spam)
MAX_ALERTS_PER_RUN = 10

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

def is_important(text: str) -> bool:
    t = (text or "").lower()
    return any(k.lower() in t for k in KEYWORDS)

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
    # search issues by title (list open issues)
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

    # create issue
    r = requests.post(
        f"https://api.github.com/repos/{owner}/{repo}/issues",
        headers=gh_headers(),
        json={
            "title": STATE_ISSUE_TITLE,
            "body": json.dumps({"seen": []}, ensure_ascii=False)
        },
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
    # cap size
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
    # Google News RSS search
    url = "https://news.google.com/rss/search"
    params = {
        "q": query,
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en"
    }
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

        # Keep it clean: description sometimes contains HTML
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
    # GDELT DOC 2.1 API (free)
    # https://blog.gdeltproject.org/gdelt-2-1-api-debuts/
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=GDELT_WINDOW_MIN)

    # GDELT uses "startdatetime" / "enddatetime" format YYYYMMDDHHMMSS
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
        title = norm_text(art.get("title", ""))
        link = norm_text(art.get("url", ""))
        seendate = norm_text(art.get("seendate", ""))
        snippet = norm_text(art.get("snippet", ""))

        out.append({
            "source": "GDELT",
            "title": title,
            "desc": snippet,
            "link": link,
            "time": seendate,
        })
    return out

# =========================
# Main
# =========================
def main():
    must_have_env()

    issue_number, seen = load_seen_hashes()
    new_seen = set(seen)

    events = []

    # Google News (multiple queries)
    for q in GOOGLE_NEWS_QUERIES:
        try:
            events.extend(fetch_google_news_rss(q))
            time.sleep(1)  # gentle
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
        h = make_hash(e["source"], e["title"], e.get("link", ""))
        if h in local_seen:
            continue
        local_seen.add(h)
        e["_hash"] = h
        unique.append(e)

    # Sort newest-ish (not perfect, but OK)
    unique = unique[:200]

    sent = 0
    for e in unique:
        if sent >= MAX_ALERTS_PER_RUN:
            break

        full = f"{e.get('title','')} {e.get('desc','')}".strip()

        # Important filter
        if not is_important(full):
            continue

        # Global dedupe (across runs)
        if e["_hash"] in seen:
            continue

        # Translate full
        title_ar = translate_ar(e.get("title", ""))
        desc_ar = translate_ar(e.get("desc", ""))

        msg = (
            f"🚨 تنبيه خبر (OSINT)\n"
            f"🗞 المصدر: {e.get('source','')}\n\n"
            f"📰 العنوان:\n{title_ar}\n\n"
            f"📄 التفاصيل:\n{desc_ar}\n\n"
            f"🕒 الوقت:\n{e.get('time','')}\n\n"
            f"🔗 الرابط:\n{e.get('link','')}"
        )

        send_telegram(msg)
        new_seen.add(e["_hash"])
        sent += 1

    # Save state
    save_seen_hashes(issue_number, new_seen)

if __name__ == "__main__":
    main()
