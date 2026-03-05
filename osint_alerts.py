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
# ENV (GitHub Secrets + Actions env)
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")              # provided by Actions
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")    # provided by Actions, e.g. "user/repo"

# =========================
# CONFIG
# =========================
STATE_ISSUE_TITLE = "osint-alerts-state"
MAX_ALERTS_PER_RUN = 25

# Time: Saudi Arabia (UTC+3)
KSA_TZ = timezone(timedelta(hours=3))
MAX_AGE_HOURS = 3  # last 3 hours (KSA)

# HARD FILTER: WAR ONLY (no sports/economy)
WAR_KEYWORDS = [
    # English
    "attack", "attacked", "missile", "missiles", "drone", "drones",
    "strike", "strikes", "airstrike", "airstrikes",
    "explosion", "explosions", "blast", "blasts",
    "clash", "clashes", "shelling", "bombardment",
    "intercept", "intercepted", "rocket", "rockets",
    "military", "navy", "fighter jet", "warplane",
    "base", "bases", "barrage",

    # Arabic
    "هجوم", "هاجم", "صاروخ", "صواريخ", "مسيرة", "مسيرات",
    "ضربة", "ضربات", "قصف", "قصف جوي",
    "انفجار", "انفجارات", "تفجير",
    "اشتباك", "اشتباكات", "اعتراض",
    "قاعدة", "قواعد", "دفعة صواريخ"
]

# Region scope (Saudi + Gulf + Red Sea + nearby)
REGION_TERMS = [
    # English
    "saudi", "saudi arabia", "ksa", "riyadh", "jeddah", "mecca", "medina",
    "gulf", "persian gulf", "arabian gulf",
    "red sea", "hormuz", "strait of hormuz", "bab al-mandab", "aden",
    "yemen", "iran", "iraq", "syria", "lebanon", "israel", "gaza", "qatar", "uae", "kuwait", "bahrain", "oman",
    # Arabic
    "السعودية", "المملكة العربية السعودية", "الرياض", "جدة", "مكة", "المدينة",
    "الخليج", "الخليج العربي", "البحر الأحمر", "مضيق هرمز", "باب المندب", "عدن",
    "اليمن", "إيران", "العراق", "سوريا", "لبنان", "إسرائيل", "غزة", "قطر", "الإمارات", "الكويت", "البحرين", "عمان"
]

# Google News queries (3 feeds = "3 مصادر" عملياً)
GOOGLE_QUERIES = [
    # 1) Saudi/Gulf war mentions
    '("Saudi" OR "Saudi Arabia" OR KSA OR السعودية OR الخليج OR "Red Sea" OR البحر الأحمر) '
    'AND (missile OR drone OR strike OR attack OR interception OR explosion OR صاروخ OR مسيرة OR قصف OR اعتراض OR انفجار)',

    # 2) Strait/Hormuz/Red Sea shipping war incidents
    '("Strait of Hormuz" OR Hormuz OR "Bab al-Mandab" OR "Red Sea" OR البحر الأحمر OR مضيق هرمز OR باب المندب) '
    'AND (attack OR strike OR missile OR drone OR seized OR interception OR هجوم OR قصف OR صاروخ OR مسيرة OR اعتراض)',

    # 3) Regional escalation that affects KSA indirectly
    '(Iran OR Yemen OR Iraq OR Syria OR Lebanon OR Gaza OR إسرائيل OR إيران OR اليمن OR العراق OR سوريا OR لبنان OR غزة) '
    'AND (missile OR drone OR strike OR attack OR explosion OR interception OR صاروخ OR مسيرة OR قصف OR انفجار OR اعتراض)'
]

# GDELT (strong OSINT) – last 3 hours window
GDELT_WINDOW_MIN = 180
GDELT_QUERY = (
    '(saudi OR "saudi arabia" OR ksa OR riyadh OR jeddah OR gulf OR "red sea" OR hormuz OR "bab al-mandab" '
    'OR yemen OR iran OR iraq OR syria OR lebanon OR israel OR gaza OR qatar OR uae OR kuwait OR bahrain OR oman) '
    'AND (attack OR missile OR drone OR strike OR airstrike OR explosion OR shelling OR intercept OR rocket)'
)

# =========================
# Helpers
# =========================
def must_have_env():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID secrets.")
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        raise RuntimeError("Missing GITHUB_TOKEN or GITHUB_REPOSITORY env in Actions.")

def norm(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s

def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", (s or "")).strip()

def contains_any(text: str, words) -> bool:
    t = (text or "").lower()
    return any(w.lower() in t for w in words)

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
    r = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "disable_web_page_preview": True
    }, timeout=30)
    r.raise_for_status()

def parse_dt_any(s: str):
    """
    Parses:
    - RSS pubDate: 'Sat, 28 Feb 2026 23:40:00 GMT'
    - GDELT seendate: '20260305101000' (often) or ISO-ish
    Returns timezone-aware UTC datetime if possible.
    """
    s = norm(s)
    if not s:
        return None

    # RSS style
    try:
        dt = parsedate_to_datetime(s)
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except Exception:
        pass

    # GDELT seendate sometimes: YYYYMMDDHHMMSS
    m = re.fullmatch(r"(\d{14})", s)
    if m:
        try:
            dt = datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass

    # ISO fallback
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def within_last_hours_ksa(time_str: str, hours: int) -> bool:
    dt_utc = parse_dt_any(time_str)
    if not dt_utc:
        return False  # strict: if no time, skip (prevents old/unknown spam)
    dt_ksa = dt_utc.astimezone(KSA_TZ)
    now_ksa = datetime.now(KSA_TZ)
    return (now_ksa - dt_ksa) <= timedelta(hours=hours)

# =========================
# GitHub Issue State (100% dedupe across runs)
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
    seen_list = list(seen)[-8000:]  # cap
    body = json.dumps({"seen": seen_list, "updated_utc": datetime.utcnow().isoformat()}, ensure_ascii=False)
    r = requests.patch(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}",
        headers=gh_headers(),
        json={"body": body},
        timeout=30
    )
    r.raise_for_status()

# =========================
# Sources: Google News RSS (3 feeds)
# =========================
def fetch_google_rss(query: str):
    url = "https://news.google.com/rss/search"
    params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()

    root = ET.fromstring(r.content)
    items = root.findall(".//item")
    out = []
    for item in items:
        title = norm(item.findtext("title", ""))
        link = norm(item.findtext("link", ""))
        pub = norm(item.findtext("pubDate", ""))
        desc = strip_html(item.findtext("description", ""))
        out.append({
            "source": "GoogleNews",
            "title": title,
            "desc": norm(desc),
            "link": link,
            "time": pub,
        })
    return out

# =========================
# Source: GDELT (strong OSINT)
# =========================
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
            "title": norm(art.get("title", "")),
            "desc": norm(art.get("snippet", "")),
            "link": norm(art.get("url", "")),
            "time": norm(art.get("seendate", "")),
        })
    return out

# =========================
def format_time_ksa(time_str: str) -> str:
    dt_utc = parse_dt_any(time_str)
    if not dt_utc:
        return time_str
    return dt_utc.astimezone(KSA_TZ).strftime("%Y-%m-%d %H:%M KSA")

# =========================
def main():
    must_have_env()

    issue_number, seen = load_seen()
    new_seen = set(seen)

    events = []

    # Google News (3 feeds)
    for q in GOOGLE_QUERIES:
        try:
            events.extend(fetch_google_rss(q))
            time.sleep(1)
        except Exception:
            continue

    # GDELT
    try:
        events.extend(fetch_gdelt())
    except Exception:
        pass

    # Deduplicate within run + filter (war + region + last 3 hours KSA)
    local = set()
    filtered = []

    for e in events:
        title = e.get("title", "")
        desc = e.get("desc", "")
        link = e.get("link", "")
        t = e.get("time", "")

        # strict time filter (KSA last 3 hours)
        if not within_last_hours_ksa(t, MAX_AGE_HOURS):
            continue

        full = f"{title} {desc}".strip()

        # war only
        if not contains_any(full, WAR_KEYWORDS):
            continue

        # region scope
        if not contains_any(full, REGION_TERMS):
            continue

        # in-run dedupe
        h_local = sha(f"{e.get('source','')}|{title}|{link}")
        if h_local in local:
            continue
        local.add(h_local)

        # cross-run dedupe key
        e["_hash"] = h_local
        filtered.append(e)

    sent = 0
    for e in filtered:
        if sent >= MAX_ALERTS_PER_RUN:
            break

        if e["_hash"] in seen:
            continue

        title_ar = translate_ar(e.get("title", ""))
        desc_ar = translate_ar(e.get("desc", ""))

        msg = (
            "🚨 تنبيه حرب – آخر 3 ساعات (KSA)\n"
            f"🗞 المصدر: {e.get('source','')}\n\n"
            f"📰 العنوان:\n{title_ar}\n\n"
            f"📄 التفاصيل:\n{desc_ar}\n\n"
            f"🕒 الوقت:\n{format_time_ksa(e.get('time',''))}\n\n"
            f"🔗 الرابط:\n{e.get('link','')}"
        )

        send_telegram(msg)
        new_seen.add(e["_hash"])
        sent += 1
        time.sleep(0.6)

    save_seen(issue_number, new_seen)

if __name__ == "__main__":
    main()
