#!/usr/bin/env python3
import os, re, json, hashlib
from datetime import datetime, timezone
from urllib.parse import urljoin
import requests
from lxml import html

# ---------- SETTINGS ----------
LIST_URL   = os.environ.get("OTB_URL", "https://www.orthobullets.com/Site/ElasticSearch/StandardSearchTiles?contentType=5&s=1,2,3,225,6,7,10")
COOKIE     = os.environ.get("OTB_COOKIE", "")
OUTPUT_RSS = os.environ.get("OTB_OUTPUT", "dist/orthobullets_cases.xml")
OUTPUT_JSON= os.environ.get("OTB_JSON",   "dist/orthobullets_cases.json")
STATE_PATH = os.environ.get("OTB_STATE",  "dist/orthobullets_cases_seen.json")
XPATH      = os.environ.get("OTB_XPATH",  '//div[contains(@class,"dashboard-item--case")]//a[@href]')
UA         = os.environ.get("OTB_UA",     "Mozilla/5.0 (Orthobullets RSS Bot)")
TIMEOUT    = int(os.environ.get("OTB_TIMEOUT", "30"))
MAX_ITEMS  = int(os.environ.get("OTB_MAX_ITEMS", "50"))
# --------------------------------

def norm(s: str) -> str:
    return " ".join((s or "").split())

def guid(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()

def now_rfc2822():
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")

def esc(s: str) -> str:
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;").replace("'","&apos;")

def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_state(path, seen):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(list(seen)), f, ensure_ascii=False, indent=2)

session = requests.Session()
session.headers.update({"User-Agent": UA})
if COOKIE.strip():
    session.headers["Cookie"] = COOKIE

def fetch_text(url):
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text, r.headers.get("Content-Type","").lower()

def find_list_links():
    text, _ = fetch_text(LIST_URL)
    doc = html.fromstring(text)
    nodes = doc.xpath(XPATH)
    links = []
    seen = set()
    for a in nodes:
        href = a.get("href") or ""
        if not href:
            continue
        if not href.startswith("http"):
            href = urljoin("https://www.orthobullets.com", href)
        # Prefer true case pages
        if "/Site/Cases/View/" not in href:
            continue
        if href in seen:
            continue
        seen.add(href)
        links.append(href)
    return links

def parse_case(link):
    """Open each case page and pull good fields from OpenGraph + page."""
    text, ctype = fetch_text(link)
    doc = html.fromstring(text)

    # Title
    og_title = doc.xpath('//meta[@property="og:title"]/@content')
    title = norm(og_title[0]) if og_title else None
    if not title:
        tnodes = doc.xpath("//title/text()")
        title = norm(tnodes[0]) if tnodes else link

    # Image
    og_img = doc.xpath('//meta[@property="og:image"]/@content') \
          or doc.xpath('//meta[@name="twitter:image"]/@content')
    image = og_img[0] if og_img else None

    # Doctor / author (best-effort)
    author = None
    cand = doc.xpath('//*[contains(@class,"author") or contains(@class,"Author")]/descendant-or-self::text()')
    if cand:
        author = norm(" ".join(cand))
    if not author:
        # some pages show author name(s) near header
        block = doc.xpath('//div[contains(@class,"case")]//text()')[:120]
        text_snip = norm(" ".join(block))
        m = re.search(r"(By\s+[^|•\n]+)", text_snip, re.I)
        if m:
            author = norm(m.group(1).replace("By", "").strip())
    return {
        "title": title or "Untitled case",
        "link": link,
        "image": image,
        "doctor": author or "",
    }

def build_rss(items):
    now = now_rfc2822()
    header = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">',
        "<channel>",
        f"<title>Orthobullets Cases (clean)</title>",
        f"<link>{esc(LIST_URL)}</link>",
        f"<description>RSS generated from a specific XPath; includes doctor & image.</description>",
        f"<lastBuildDate>{now}</lastBuildDate>"
    ]
    body = []
    for it in items[:MAX_ITEMS]:
        body.append("<item>")
        body.append(f"<title>{esc(it['title'])}</title>")
        body.append(f"<link>{esc(it['link'])}</link>")
        body.append(f"<guid isPermaLink='false'>{esc(it['id'])}</guid>")
        body.append(f"<pubDate>{esc(it['pubDate'])}</pubDate>")
        desc = f"Doctor: {it['doctor']}" if it['doctor'] else ""
        if desc:
            body.append(f"<description>{esc(desc)}</description>")
        if it.get("image"):
            # Standard RSS enclosure + Media RSS
            body.append(f"<enclosure url=\"{esc(it['image'])}\" type=\"image/jpeg\"/>")
            body.append(f"<media:content url=\"{esc(it['image'])}\" medium=\"image\"/>")
        body.append("</item>")
    footer = ["</channel>", "</rss>"]
    return "\n".join(header + body + footer)

def main():
    links = find_list_links()
    if not links:
        raise SystemExit("No case links found. Check XPATH or login.")
    seen = load_state(STATE_PATH)

    items = []
    for link in links:
        info = parse_case(link)
        info["id"] = guid(link)
        info["pubDate"] = now_rfc2822() if info["id"] not in seen else now_rfc2822()
        items.append(info)
        seen.add(info["id"])

    # Write RSS
    os.makedirs(os.path.dirname(OUTPUT_RSS) or ".", exist_ok=True)
    with open(OUTPUT_RSS, "w", encoding="utf-8") as f:
        f.write(build_rss(items))

    # Write JSON (n8n-friendly)
    os.makedirs(os.path.dirname(OUTPUT_JSON) or ".", exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(items[:MAX_ITEMS], f, ensure_ascii=False, indent=2)

    save_state(STATE_PATH, seen)
    print(f"Wrote {OUTPUT_RSS} and {OUTPUT_JSON} with {min(len(items), MAX_ITEMS)} items.")

if __name__ == "__main__":
    main()
