#!/usr/bin/env python3
import os, hashlib, json
from datetime import datetime, timezone
import requests
from lxml import html

# ---- Config ----
URL    = os.environ.get("OTB_URL", "https://www.orthobullets.com/Site/ElasticSearch/StandardSearchTiles?contentType=5&s=1,2,3,225,6,7,10")
COOKIE = os.environ.get("OTB_COOKIE", "")
OUTPUT = os.environ.get("OTB_OUTPUT", "dist/orthobullets_cases.xml")
STATE  = os.environ.get("OTB_STATE", "dist/orthobullets_cases_seen.json")
# XPath targets ONLY case links inside those dashboard-item blocks:
XPATH  = os.environ.get("OTB_XPATH", '//div[contains(@class,"dashboard-item--case")]//a[@href]')
UA     = os.environ.get("OTB_UA", "Mozilla/5.0 (Orthobullets RSS Bot)")
# ----------------

def now_rfc2822():
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")

def guid(s): return hashlib.sha1(s.encode("utf-8")).hexdigest()

def esc(s): return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def load_state(path):
    try: return set(json.load(open(path)))
    except: return set()

def save_state(path, seen):
    with open(path, "w") as f: json.dump(list(seen), f)

def fetch(url):
    headers = {"User-Agent": UA}
    if COOKIE:
        headers["Cookie"] = COOKIE
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text

def main():
    text = fetch(URL)
    doc = html.fromstring(text)
    nodes = doc.xpath(XPATH)

    seen = load_state(STATE)
    items, now = [], now_rfc2822()

    for node in nodes:
        href = node.get("href")
        title = node.text_content().strip()
        if not href or not title:
            continue
        if not href.startswith("http"):
            href = "https://www.orthobullets.com" + href
        g = guid(href)
        items.append({
            "title": title,
            "link": href,
            "guid": g,
            "pubDate": now if g not in seen else now
        })
        seen.add(g)

    save_state(STATE, seen)

    rss = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"><channel>',
        f"<title>Orthobullets Case Feed (XPath)</title>",
        f"<link>{URL}</link>",
        f"<description>RSS generated from specific XPath of Orthobullets</description>",
        f"<lastBuildDate>{now}</lastBuildDate>"
    ]
    for it in items:
        rss.append("<item>")
        rss.append(f"<title>{esc(it['title'])}</title>")
        rss.append(f"<link>{esc(it['link'])}</link>")
        rss.append(f"<guid isPermaLink='false'>{it['guid']}</guid>")
        rss.append(f"<pubDate>{it['pubDate']}</pubDate>")
        rss.append("</item>")
    rss.append("</channel></rss>")

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write("\n".join(rss))

    print(f"Wrote {OUTPUT} with {len(items)} items.")

if __name__ == "__main__":
    main()
