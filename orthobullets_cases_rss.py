#!/usr/bin/env python3
import os, re, json, hashlib
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

OTB_URL = os.environ.get("OTB_URL", "https://www.orthobullets.com/cases")
COOKIE  = os.environ.get("OTB_COOKIE")
OUTPUT  = os.environ.get("OTB_OUTPUT", "orthobullets_cases.xml")
STATE   = os.environ.get("OTB_STATE",  "orthobullets_cases_seen.json")
USER_AGENT = os.environ.get("OTB_UA", "Mozilla/5.0 (Personal RSS bot)")
TIMEOUT = 25
MAX_ITEMS = int(os.environ.get("OTB_MAX_ITEMS", "50"))

if not COOKIE:
    raise SystemExit("Missing OTB_COOKIE env var.")

s = requests.Session()
s.headers.update({"User-Agent": USER_AGENT, "Cookie": COOKIE, "Accept": "text/html,application/xhtml+xml"})

def load_state(p):
    try:
        return set(json.load(open(p, "r", encoding="utf-8")))
    except Exception:
        return set()

def save_state(p, ids):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(sorted(list(ids)), f, ensure_ascii=False, indent=2)

def fetch(url):
    r = s.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

def norm(s): return re.sub(r"\s+", " ", (s or "")).strip()

def find_items(html, base):
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"/case(s)?/|/vignettes?/|/questions?/", href, re.I):
            link = urljoin(base, href)
            if link in seen: continue
            seen.add(link)
            title = norm(a.get_text()) or norm(a.get("title") or a.get("aria-label") or "")
            if not title:
                h = a.find_parent().find(["h1","h2","h3","h4"]) if a.find_parent() else None
                if h: title = norm(h.get_text())
            if not title:
                title = urlparse(link).path.strip("/").replace("-", " ").title()
            if len(title) < 6:  # skip tiny nav labels
                continue
            out.append({"title": title, "link": link})
    return out

def guid(s): return hashlib.sha1(s.encode("utf-8")).hexdigest()
def now_rfc2822(): return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")

def esc(x):
    return (x.replace("&","&amp;").replace("<","&lt;")
             .replace(">","&gt;").replace('"',"&quot;").replace("'","&apos;"))

def build_rss(title, link, desc, items):
    now = now_rfc2822()
    parts = ['<?xml version="1.0" encoding="UTF-8"?>','<rss version="2.0">',"<channel>",
             f"<title>{esc(title)}</title>", f"<link>{esc(link)}</link>",
             f"<description>{esc(desc)}</description>", f"<lastBuildDate>{now}</lastBuildDate>"]
    for it in items[:MAX_ITEMS]:
        parts += ["<item>", f"<title>{esc(it['title'])}</title>",
                  f"<link>{esc(it['link'])}</link>",
                  f"<guid isPermaLink='false'>{esc(it['guid'])}</guid>",
                  f"<pubDate>{it.get('pubDate', now)}</pubDate>", "</item>"]
    parts += ["</channel>","</rss>"]
    return "\n".join(parts)

def main():
    html = fetch(OTB_URL)
    items = find_items(html, OTB_URL)
    if not items:
        raise SystemExit("No case-like links found. Tweak regex if site changed.")
    seen = load_state(STATE)
    prepared, new = [], []
    for it in items:
        g = guid(it["link"])
        it["guid"] = g
        if g not in seen:
            it["pubDate"] = now_rfc2822()
            new.append(it)
        prepared.append(it)
    for it in new: seen.add(it["guid"])
    save_state(STATE, seen)
    uniq, seen_g = [], set()
    for it in (new + prepared):
        if it["guid"] in seen_g: continue
        seen_g.add(it["guid"])
        uniq.append(it)
    rss = build_rss("Orthobullets – Cases (Unofficial RSS)", OTB_URL,
                    "Unofficial RSS generated from Orthobullets Cases using a logged-in session cookie.",
                    uniq)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(rss)
    print(f"Wrote {OUTPUT} with {min(len(uniq), MAX_ITEMS)} items.")

if __name__ == "__main__":
    main()
