#!/usr/bin/env python3
import os, re, json, hashlib
from datetime import datetime, timezone
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

# ---------- Config ----------
OTB_URL   = os.environ.get("OTB_URL", "https://www.orthobullets.com/Site/ElasticSearch/StandardSearchTiles?contentType=5&s=1,2,3,225,6,7,10")
COOKIE    = os.environ.get("OTB_COOKIE", "")  # optional if endpoint is public
OUTPUT    = os.environ.get("OTB_OUTPUT", "orthobullets_cases.xml")
STATE     = os.environ.get("OTB_STATE",  "orthobullets_cases_seen.json")
USER_AGENT= os.environ.get("OTB_UA", "Mozilla/5.0 (Orthobullets RSS bot; personal use)")
TIMEOUT   = 30
MAX_ITEMS = int(os.environ.get("OTB_MAX_ITEMS", "50"))
# ----------------------------

sess = requests.Session()
headers = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.9,*/*;q=0.8",
}
if COOKIE.strip():
    headers["Cookie"] = COOKIE
sess.headers.update(headers)

def now_rfc2822():
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")

def esc(s: str) -> str:
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;").replace("'","&apos;")

def guid(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()

def load_state(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_state(path: str, ids):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sorted(list(ids)), f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def fetch(url: str) -> (str, str):
    r = sess.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    ctype = r.headers.get("Content-Type","").lower()
    return r.text, ctype

def parse_from_json(txt: str, base: str):
    items = []
    try:
        data = json.loads(txt)
    except Exception:
        return items
    # Try common shapes: list or dict with "items"/"results"
    candidates = []
    if isinstance(data, list):
        candidates = data
    elif isinstance(data, dict):
        for key in ("items", "results", "data", "Tiles", "tiles"):
            if key in data and isinstance(data[key], list):
                candidates = data[key]
                break
    for obj in candidates:
        # Guess title/url fields
        title = (obj.get("title") or obj.get("Title") or obj.get("name") or obj.get("Name") or "").strip()
        url   = (obj.get("url")   or obj.get("Url")   or obj.get("link") or obj.get("Link")   or "").strip()
        if not url and "slug" in obj:
            url = f"https://www.orthobullets.com/{obj['slug']}"
        if not (title and url):
            continue
        full = url if url.startswith("http") else urljoin(base, url)
        items.append({"title": title, "link": full})
    return items

def parse_from_html(txt: str, base: str):
    soup = BeautifulSoup(txt, "html.parser")
    out, seen = [], set()
    # look for tile anchors
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        title = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
        if not href:
            continue
        full = href if href.startswith("http") else urljoin(base, href)
        # keep only links that look like case content
        if not re.search(r"/case|/cases|/vignette|/question", full, re.I):
            # loosen: allow any orthobullets link, many tiles still point to case pages
            if "orthobullets.com" not in full:
                continue
        if not title:
            title = re.sub(r"https?://", "", full)
        if len(title) < 4:
            continue
        if full in seen:
            continue
        seen.add(full)
        out.append({"title": title, "link": full})
    return out

def build_rss(channel_title, channel_link, channel_desc, items):
    now = now_rfc2822()
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        f"<title>{esc(channel_title)}</title>",
        f"<link>{esc(channel_link)}</link>",
        f"<description>{esc(channel_desc)}</description>",
        f"<lastBuildDate>{now}</lastBuildDate>",
    ]
    for it in items[:MAX_ITEMS]:
        parts.append("<item>")
        parts.append(f"<title>{esc(it['title'])}</title>")
        parts.append(f"<link>{esc(it['link'])}</link>")
        parts.append(f"<guid isPermaLink='false'>{esc(it['guid'])}</guid>")
        parts.append(f"<pubDate>{it.get('pubDate', now)}</pubDate>")
        if it.get("description"):
            parts.append(f"<description>{esc(it['description'])}</description>")
        parts.append("</item>")
    parts += ["</channel>", "</rss>"]
    return "\n".join(parts)

def main():
    text, ctype = fetch(OTB_URL)

    if "application/json" in ctype or text.strip().startswith(("{","[")):
        items = parse_from_json(text, "https://www.orthobullets.com/")
    else:
        items = parse_from_html(text, "https://www.orthobullets.com/")

    if not items:
        raise SystemExit("No items found from the provided URL (try checking if login/cookie is required).")

    # De-dup, add GUIDs, tag new ones with pubDate
    seen = load_state(STATE)
    prepared = []
    new_count = 0
    for it in items:
        g = guid(it["link"])
        it["guid"] = g
        if g not in seen:
            it["pubDate"] = now_rfc2822()
            new_count += 1
        prepared.append(it)
        seen.add(g)
    save_state(STATE, seen)

    rss = build_rss(
        channel_title="Orthobullets Cases (from StandardSearchTiles)",
        channel_link=OTB_URL,
        channel_desc="RSS generated from Orthobullets ElasticSearch StandardSearchTiles endpoint.",
        items=prepared
    )
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(rss)
    print(f"Wrote {OUTPUT} with {len(prepared)} items (new={new_count}).")

if __name__ == "__main__":
    main()
