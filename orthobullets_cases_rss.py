#!/usr/bin/env python3
import os, re, json, time, hashlib
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

# ------------------ Config ------------------
OTB_URL = os.environ.get("OTB_URL", "https://www.orthobullets.com/cases")
COOKIE   = os.environ.get("OTB_COOKIE")  # REQUIRED
OUTPUT   = os.environ.get("OTB_OUTPUT", "orthobullets_cases.xml")
STATE    = os.environ.get("OTB_STATE",  "orthobullets_cases_seen.json")
USER_AGENT = os.environ.get("OTB_UA", "Mozilla/5.0 (RSS fetcher for personal use)")
TIMEOUT = 25
MAX_ITEMS = int(os.environ.get("OTB_MAX_ITEMS", "50"))  # cap the feed length
# -------------------------------------------

if not COOKIE:
    raise SystemExit("Missing OTB_COOKIE env var with your Orthobullets session cookies.")

session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Cookie": COOKIE,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

def load_state(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()
    except Exception:
        return set()

def save_state(path: str, ids):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(list(ids)), f, ensure_ascii=False, indent=2)

def fetch_html(url: str) -> str:
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def guess_items_from_cases_page(html: str, base_url: str):
    """
    Generic extractor:
      - find anchors whose href looks like a case link
      - title = anchor text or nearest heading
    """
    soup = BeautifulSoup(html, "html.parser")

    candidates = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # heuristics for case links
        if re.search(r"/case(s)?/|/vignettes?/|/questions?/", href, flags=re.I):
            link = urljoin(base_url, href)
            title = normalize_space(a.get_text())
            # sometimes anchor text is empty; try parent or aria-label
            if not title:
                title = normalize_space(a.get("aria-label") or a.get("title") or "")
            if not title:
                # look upwards for a header
                h = a.find_parent().find(["h1","h2","h3","h4"]) if a.find_parent() else None
                if h:
                    title = normalize_space(h.get_text())
            # last resort: use URL path
            if not title:
                title = urlparse(link).path.strip("/").replace("-", " ").title()

            # de-dupe obvious nav/filters by ignoring tiny or generic texts
            if len(title) < 6:
                continue

            candidates.append({"title": title, "link": link})

    # de-duplicate by link
    seen_links = set()
    items = []
    for it in candidates:
        if it["link"] in seen_links:
            continue
        seen_links.add(it["link"])
        items.append(it)

    return items

def make_guid(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def rfc2822_now():
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")

def build_rss(channel_title, channel_link, channel_desc, items):
    now = rfc2822_now()
    parts = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append('<rss version="2.0">')
    parts.append("<channel>")
    parts.append(f"<title>{escape_xml(channel_title)}</title>")
    parts.append(f"<link>{escape_xml(channel_link)}</link>")
    parts.append(f"<description>{escape_xml(channel_desc)}</description>")
    parts.append(f"<lastBuildDate>{now}</lastBuildDate>")
    for it in items[:MAX_ITEMS]:
        title = escape_xml(it['title'])
        link  = escape_xml(it['link'])
        guid  = escape_xml(it['guid'])
        pub   = it.get('pubDate') or now
        parts.append("<item>")
        parts.append(f"<title>{title}</title>")
        parts.append(f"<link>{link}</link>")
        parts.append(f"<guid isPermaLink='false'>{guid}</guid>")
        parts.append(f"<pubDate>{pub}</pubDate>")
        if it.get("description"):
            parts.append(f"<description>{escape_xml(it['description'])}</description>")
        parts.append("</item>")
    parts.append("</channel>")
    parts.append("</rss>")
    return "\n".join(parts)

def escape_xml(s: str) -> str:
    return (s.replace("&","&amp;")
             .replace("<","&lt;")
             .replace(">","&gt;")
             .replace('"',"&quot;")
             .replace("'","&apos;"))

def main():
    html = fetch_html(OTB_URL)

    items = guess_items_from_cases_page(html, OTB_URL)

    if not items:
        raise SystemExit("No case-like links found. If Orthobullets changed markup, tweak the regex in guess_items_from_cases_page().")

    # Load seen IDs
    seen = load_state(STATE)

    # Transform to RSS items, filtering new ones first (top of page assumed newest)
    new_items = []
    for it in items:
        guid = make_guid(it["link"])
        it["guid"] = guid
        # if page shows dates, you could parse them into it['pubDate']
        # fallback: use current time for new items
        if guid not in seen:
            it["pubDate"] = rfc2822_now()
            new_items.append(it)

    # Update seen state
    for it in new_items:
        seen.add(it["guid"])
    save_state(STATE, seen)

    # Keep most recent list (prefer showing recent, not entire history)
    final_items = new_items + [it for it in items if make_guid(it["link"]) in seen]
    # ensure unique by guid, keep order
    unique, seen_guid = [], set()
    for it in final_items:
        g = it["guid"]
        if g in seen_guid:
            continue
        seen_guid.add(g)
        unique.append(it)

    rss = build_rss(
        channel_title="Orthobullets – Cases (Unofficial RSS)",
        channel_link=OTB_URL,
        channel_desc="Unofficial RSS generated from Orthobullets Cases using a logged-in session cookie.",
        items=unique
    )

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(rss)

    print(f"Written RSS with {min(len(unique), MAX_ITEMS)} items -> {OUTPUT}")

if __name__ == "__main__":
    main()
