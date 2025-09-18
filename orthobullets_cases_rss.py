#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orthobullets Cases -> RSS + JSON (login, XPath, last-24h filter, robust + always writes files)
Outputs:
  dist/orthobullets_cases.xml
  dist/orthobullets_cases.json
"""

import os, re, json, hashlib, sys, traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from lxml import html as lxml_html
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------------- Env ----------------
LIST_URL   = os.environ.get("OTB_URL", "https://www.orthobullets.com/Site/ElasticSearch/StandardSearchTiles?contentType=5&s=1,2,3,225,6,7,10")
XPATH      = os.environ.get("OTB_XPATH", '//div[contains(@class,"dashboard-item--case")]//a[@href]')
EMAIL      = os.environ.get("OTB_EMAIL", "")
PASSWORD   = os.environ.get("OTB_PASSWORD", "")
OUTPUT_RSS = os.environ.get("OTB_OUTPUT", "dist/orthobullets_cases.xml")
OUTPUT_JSON= os.environ.get("OTB_JSON",   "dist/orthobullets_cases.json")
STATE_PATH = os.environ.get("OTB_STATE",  "dist/orthobullets_cases_seen.json")
UA         = os.environ.get("OTB_UA", "Mozilla/5.0 (Orthobullets RSS Bot)")
MAX_ITEMS  = int(os.environ.get("OTB_MAX_ITEMS", "50"))
INCLUDE_UNDATED = os.environ.get("OTB_INCLUDE_UNDATED", "0") == "1"
DEBUG      = os.environ.get("OTB_DEBUG", "0") == "1"
# -------------------------------------

def log(msg: str):
    print(f"[scraper] {msg}", flush=True)

def norm(s: str) -> str:
    return " ".join((s or "").split())

def guid(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def rfc2822(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")

def esc(s: str) -> str:
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;").replace("'","&apos;")

def abs_url(base: str, href: str | None) -> str | None:
    if not href: return None
    return href if href.startswith("http") else urljoin(base, href)

def parse_iso_dt(s: str) -> datetime | None:
    if not s: return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        pass
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(:\d{2})?)", s or "")
    if m:
        try:
            hhmmss = m.group(2)
            if len(hhmmss) == 5: hhmmss += ":00"
            return datetime.fromisoformat(f"{m.group(1)}T{hhmmss}+00:00").astimezone(timezone.utc)
        except Exception:
            return None
    return None

def section_text(doc: lxml_html.HtmlElement, keywords: list[str]) -> str:
    headings = []
    for level in ("h1","h2","h3"):
        for k in keywords:
            xp = f'//{level}[contains(translate(.,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"{k.lower()}")]'
            headings += doc.xpath(xp)
    if not headings:
        return ""
    h = headings[0]
    parts = []
    for sib in h.itersiblings():
        if sib.tag.lower() in ("h1","h2","h3"):
            break
        txt = norm(" ".join(sib.itertext()))
        if txt:
            parts.append(txt)
    return "\n\n".join(parts[:8])

def main_content_text(doc: lxml_html.HtmlElement) -> str:
    for xp in [
        '//article',
        '//*[contains(@class,"case") and contains(@class,"content")]',
        '//*[contains(@class,"case-content")]',
        '//*[@id="content"]',
        '//main'
    ]:
        n = doc.xpath(xp)
        if n:
            return norm(" ".join(n[0].itertext()))
    return norm(" ".join(doc.xpath("//body//text()")))[:4000]

def login_and_collect_links(pw):
    if not EMAIL or not PASSWORD:
        raise RuntimeError("Missing OTB_EMAIL or OTB_PASSWORD.")

    log(f"Opening list URL: {LIST_URL}")
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 1800})
    page = context.new_page()

    page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60_000)
    if "/Site/Account/Login" in page.url.lower() or "login" in page.url.lower():
        log("Login page detected. Submitting credentials.")
        for sel in ['input[name="Email"]', '#Email', 'input[type="email"]', 'input[name="Username"]']:
            if page.locator(sel).count():
                page.fill(sel, EMAIL); break
        for sel in ['input[name="Password"]', '#Password', 'input[type="password"]']:
            if page.locator(sel).count():
                page.fill(sel, PASSWORD); break
        if page.locator('button[type="submit"]').count():
            page.click('button[type="submit"]')
        else:
            page.keyboard.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=60_000)
        page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60_000)

    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
    except Exception:
        pass

    html_text = page.content()
    doc = lxml_html.fromstring(html_text)
    nodes = doc.xpath(XPATH)
    log(f"XPath matched {len(nodes)} anchors.")

    links, seen = [], set()
    for a in nodes:
        href = abs_url("https://www.orthobullets.com", a.get("href"))
        if not href: continue
        if "/Site/Cases/View/" not in href:    # keep real case pages
            continue
        if href in seen: continue
        seen.add(href)
        links.append(href)

    log(f"Collected {len(links)} case links.")
    return browser, context, page, links[:MAX_ITEMS]

def extract_case(page, link):
    if DEBUG: log(f"Opening case: {link}")
    page.goto(link, wait_until="domcontentloaded", timeout=60_000)

    # Title
    title = None
    try:
        title = page.locator('meta[property="og:title"]').first.get_attribute("content", timeout=5_000)
    except PWTimeout:
        pass
    if not title:
        try: title = page.title()
        except Exception: title = link
    title = norm(title)

    # Images
    images = []
    try:
        for img in page.locator("img").all():
            src = img.get_attribute("src")
            u = abs_url(link, src)
            if u and u not in images:
                images.append(u)
    except Exception:
        pass

    # Doctor
    doctor = ""
    try:
        for sel in ['[class*="author"]','[class*="Author"]','.case-author','.dashboard-item__author']:
            if page.locator(sel).count():
                doctor = norm(page.locator(sel).inner_text()); break
        if not doctor:
            snippet = page.locator("body").inner_text()[:2000]
            m = re.search(r"\bBy\s+([^\n|•]+)", snippet, re.I)
            if m: doctor = norm(m.group(1))
    except Exception:
        pass

    doc = lxml_html.fromstring(page.content())

    published_at = None
    for xp in [
        '//meta[@property="article:published_time"]/@content',
        '//meta[@name="pubdate"]/@content',
        '//time/@datetime',
        '//meta[@property="og:updated_time"]/@content'
    ]:
        vals = doc.xpath(xp)
        if vals:
            published_at = parse_iso_dt(vals[0])
            if published_at: break

    text = main_content_text(doc)[:8000]
    therapy = section_text(doc, ["treatment", "therapy", "management"])[:4000]

    return {
        "title": title or "Untitled case",
        "link": link,
        "doctor": doctor,
        "published_at": published_at.isoformat() if published_at else None,
        "text": text,
        "therapy": therapy,
        "images": images
    }

def build_rss(items):
    now = rfc2822(now_utc())
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">',
        "<channel>",
        f"<title>Orthobullets Cases (last 24h)</title>",
        f"<link>{esc(LIST_URL)}</link>",
        f"<description>Cases in the last 24 hours; includes doctor, image, therapy snippet.</description>",
        f"<lastBuildDate>{now}</lastBuildDate>",
    ]
    for it in items:
        out.append("<item>")
        out.append(f"<title>{esc(it['title'])}</title>")
        out.append(f"<link>{esc(it['link'])}</link>")
        out.append(f"<guid isPermaLink='false'>{esc(it['id'])}</guid>")
        out.append(f"<pubDate>{esc(rfc2822(it['pub_dt']))}</pubDate>")
        desc = []
        if it['doctor']: desc.append(f"Doctor: {it['doctor']}")
        if it['therapy']:
            snip = it['therapy'][:300] + ('…' if len(it['therapy']) > 300 else '')
            desc.append(f"Therapy: {snip}")
        if desc:
            out.append(f"<description>{esc(' | '.join(desc))}</description>")
        if it['images']:
            out.append(f"<enclosure url=\"{esc(it['images'][0])}\" type=\"image/jpeg\"/>")
            out.append(f"<media:content url=\"{esc(it['images'][0])}\" medium=\"image\"/>")
        out.append("</item>")
    out += ["</channel>", "</rss>"]
    return "\n".join(out)

def safe_write(path, content: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def safe_write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def run() -> int:
    items = []
    cutoff = now_utc() - timedelta(hours=24)

    try:
        with sync_playwright() as pw:
            browser, context, page, links = login_and_collect_links(pw)
            if not links:
                log("No links found after XPath filter.")

            for link in links:
                try:
                    data = extract_case(page, link)
                except Exception as e:
                    log(f"ERROR extracting {link}: {e}")
                    if DEBUG: traceback.print_exc()
                    continue

                if data["published_at"]:
                    pub_dt = parse_iso_dt(data["published_at"])
                    if not pub_dt or pub_dt < cutoff:
                        continue
                else:
                    if not INCLUDE_UNDATED:
                        continue
                    pub_dt = now_utc()

                items.append({
                    "title": data["title"],
                    "link": data["link"],
                    "doctor": data["doctor"],
                    "text": data["text"],
                    "therapy": data["therapy"],
                    "images": data["images"],
                    "id": guid(data["link"]),
                    "pub_dt": pub_dt,
                })

            try: browser.close()
            except Exception: pass

    except Exception as e:
        log(f"FATAL: {e}")
        if DEBUG: traceback.print_exc()

    # Sort & cap
    items.sort(key=lambda x: x["pub_dt"], reverse=True)
    items = items[:MAX_ITEMS]

    # Always write outputs so the workflow sees files
    rss = build_rss(items)
    safe_write(OUTPUT_RSS, rss)

    json_payload = [{
        "title": it["title"],
        "link": it["link"],
        "doctor": it["doctor"],
        "published_at": it["pub_dt"].isoformat(),
        "text": it["text"],
        "therapy": it["therapy"],
        "images": it["images"],
        "id": it["id"],
    } for it in items]
    safe_write_json(OUTPUT_JSON, json_payload)

    log(f"Wrote {OUTPUT_RSS} and {OUTPUT_JSON} with {len(items)} items (last 24h).")
    # Return non-zero if nothing matched, so you notice — but files are present
    return 0 if items else 2

if __name__ == "__main__":
    sys.exit(run())
