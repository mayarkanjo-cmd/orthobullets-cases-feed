#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orthobullets Cases -> RSS + JSON (login, XPath, last-24h filter, full scrape)
Requires: playwright (chromium), lxml

Secrets (GitHub Actions):
  OTB_EMAIL, OTB_PASSWORD  # required

Env (defaults shown):
  OTB_URL      = https://www.orthobullets.com/Site/ElasticSearch/StandardSearchTiles?contentType=5&s=1,2,3,225,6,7,10
  OTB_XPATH    = //div[contains(@class,"dashboard-item--case")]//a[@href]
  OTB_OUTPUT   = dist/orthobullets_cases.xml
  OTB_JSON     = dist/orthobullets_cases.json
  OTB_STATE    = dist/orthobullets_cases_seen.json   # still used to assign pubDate if needed
  OTB_UA       = Mozilla/5.0 (Orthobullets RSS Bot)
  OTB_MAX_ITEMS= 50
  OTB_INCLUDE_UNDATED = 0   # set to "1" to include cases with unknown date
"""
import os, re, json, hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from lxml import html as lxml_html
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# -------- Config ----------
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
# --------------------------

def norm(s: str) -> str:
    return " ".join((s or "").split())

def guid(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def now_rfc2822(dt: datetime | None = None) -> str:
    d = dt or now_utc()
    return d.strftime("%a, %d %b %Y %H:%M:%S %z")

def esc(s: str) -> str:
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;").replace("'","&apos;")

def load_state(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_state(path: str, seen):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(list(seen)), f, ensure_ascii=False, indent=2)

# ---- helpers to parse within a page (lxml content) ----
def abs_url(base: str, href: str | None) -> str | None:
    if not href: return None
    return href if href.startswith("http") else urljoin(base, href)

def parse_iso_dt(s: str) -> datetime | None:
    # Try strict ISO first, then a few fallbacks
    try:
        # handles '2024-10-22T13:45:00Z' or with offset
        return datetime.fromisoformat(s.replace("Z","+00:00")).astimezone(timezone.utc)
    except Exception:
        pass
    # yyyy-mm-dd hh:mm
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", s or "")
    if m:
        try:
            return datetime.fromisoformat(m.group(1)+"T"+m.group(2)+"+00:00").astimezone(timezone.utc)
        except Exception:
            return None
    return None

def section_text(doc: lxml_html.HtmlElement, keywords: list[str]) -> str:
    """
    Find a section by heading keywords (e.g., 'Treatment', 'Therapy', 'Management')
    and gather text until the next heading sibling.
    """
    # find headings
    heading_xpath = "|".join([f'//h1[contains(translate(.,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"{k.lower()}")]' for k in keywords] +
                             [f'//h2[contains(translate(.,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"{k.lower()}")]' for k in keywords] +
                             [f'//h3[contains(translate(.,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"{k.lower()}")]' for k in keywords])
    nodes = doc.xpath(heading_xpath)
    if not nodes:
        return ""
    h = nodes[0]
    parts = []
    # Gather following siblings until next heading of same depth
    for sib in h.itersiblings():
        if sib.tag.lower() in ("h1","h2","h3"):
            break
        txt = norm(" ".join(sib.itertext()))
        if txt:
            parts.append(txt)
    return "\n\n".join(parts[:8])  # keep it reasonable

def main_content_text(doc: lxml_html.HtmlElement) -> str:
    # Try common containers first
    for xp in [
        '//article',
        '//*[contains(@class,"case") and contains(@class,"content")]',
        '//*[contains(@class,"case-content")]',
        '//*[contains(@class,"dashboard-item")]',
        '//*[@id="content"]',
        '//main'
    ]:
        nodes = doc.xpath(xp)
        if nodes:
            return norm(" ".join(nodes[0].itertext()))
    # Fallback: body (trim)
    return norm(" ".join(doc.xpath("//body//text()")))[:4000]

# ---- Playwright login and scraping ----
def login_and_collect_links(pw):
    if not EMAIL or not PASSWORD:
        raise SystemExit("Missing OTB_EMAIL or OTB_PASSWORD env vars.")

    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 1800})
    page = context.new_page()

    page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60_000)
    if "/Site/Account/Login" in page.url.lower() or "login" in page.url.lower():
        # Fill email
        for sel in ['input[name="Email"]', '#Email', 'input[type="email"]', 'input[name="Username"]']:
            if page.locator(sel).count():
                page.fill(sel, EMAIL); break
        # Fill password
        for sel in ['input[name="Password"]', '#Password', 'input[type="password"]']:
            if page.locator(sel).count():
                page.fill(sel, PASSWORD); break
        # Submit
        if page.locator('button[type="submit"]').count():
            page.click('button[type="submit"]')
        else:
            page.keyboard.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=60_000)
        page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60_000)

    # Nudge lazy content
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
    except Exception:
        pass

    doc = lxml_html.fromstring(page.content())
    nodes = doc.xpath(XPATH)

    links, seen = [], set()
    for a in nodes:
        href = abs_url("https://www.orthobullets.com", a.get("href"))
        if not href: continue
        # prefer real case pages
        if "/Site/Cases/View/" not in href:
            continue
        if href in seen: continue
        seen.add(href)
        links.append(href)

    return browser, context, page, links[:MAX_ITEMS]

def extract_case(page, link):
    """Open detail and extract: title, doctor, published_at, text, therapy, images."""
    page.goto(link, wait_until="domcontentloaded", timeout=60_000)

    # Title (OG or <title>)
    title = None
    try:
        title = page.locator('meta[property="og:title"]').first.get_attribute("content", timeout=5_000)
    except PWTimeout:
        pass
    if not title:
        try: title = page.title()
        except Exception: title = link
    title = norm(title)

    # Images (all)
    imgs = []
    try:
        for i in page.locator("img").all():
            src = i.get_attribute("src")
            u = abs_url(link, src)
            if u and u not in imgs:
                imgs.append(u)
    except Exception:
        pass

    # Doctor (best-effort)
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

    # Grab full HTML once and parse with lxml for richer queries
    doc = lxml_html.fromstring(page.content())

    # Published time
    published_at = None
    # try common meta tags and <time>
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

    # Text (main body) and Therapy section
    text = main_content_text(doc)[:8000]  # keep it bounded
    therapy = section_text(doc, ["treatment", "therapy", "management"])[:4000]

    return {
        "title": title or "Untitled case",
        "link": link,
        "doctor": doctor,
        "published_at": published_at.isoformat() if published_at else None,
        "text": text,
        "therapy": therapy,
        "images": imgs,
    }

# ---- Builders ----
def build_rss(items):
    now = now_rfc2822()
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">',
        "<channel>",
        f"<title>Orthobullets Cases (last 24h)</title>",
        f"<link>{esc(LIST_URL)}</link>",
        f"<description>Cases in the last 24 hours; includes doctor, first image, therapy snippet.</description>",
        f"<lastBuildDate>{now}</lastBuildDate>",
    ]
    for it in items:
        out.append("<item>")
        out.append(f"<title>{esc(it['title'])}</title>")
        out.append(f"<link>{esc(it['link'])}</link>")
        out.append(f"<guid isPermaLink='false'>{esc(it['id'])}</guid>")
        out.append(f"<pubDate>{esc(now_rfc2822(it['pub_dt']))}</pubDate>")
        desc_bits = []
        if it['doctor']: desc_bits.append(f"Doctor: {it['doctor']}")
        if it['therapy']: desc_bits.append(f"Therapy: {it['therapy'][:300]}{'…' if len(it['therapy'])>300 else ''}")
        if desc_bits:
            out.append(f"<description>{esc(' | '.join(desc_bits))}</description>")
        if it['images']:
            out.append(f"<enclosure url=\"{esc(it['images'][0])}\" type=\"image/jpeg\"/>")
            out.append(f"<media:content url=\"{esc(it['images'][0])}\" medium=\"image\"/>")
        out.append("</item>")
    out += ["</channel>", "</rss>"]
    return "\n".join(out)

# ---- Main ----
def main():
    os.makedirs(os.path.dirname(OUTPUT_RSS) or ".", exist_ok=True)

    # Load state (only used for stable GUID/pubDate fallback if needed)
    seen = load_state(STATE_PATH)
    twentyfour_ago = now_utc() - timedelta(hours=24)

    with sync_playwright() as pw:
        browser, context, page, links = login_and_collect_links(pw)
        if not links:
            browser.close()
            raise SystemExit("No case links found — check XPATH or that login succeeded.")

        items_raw = []
        for link in links:
            data = extract_case(page, link)

            # filter last 24h
            if data["published_at"]:
                pub_dt = parse_iso_dt(data["published_at"])
                if not pub_dt or pub_dt < twentyfour_ago:
                    continue
            else:
                if not INCLUDE_UNDATED:
                    # skip if we cannot verify it's within 24h
                    continue
                pub_dt = now_utc()

            data["id"] = guid(link)
            data["pub_dt"] = pub_dt
            items_raw.append(data)

        # sort newest first
        items_raw.sort(key=lambda x: x["pub_dt"], reverse=True)
        items = items_raw[:MAX_ITEMS]

        # Write RSS
        rss = build_rss(items)
        with open(OUTPUT_RSS, "w", encoding="utf-8") as f:
            f.write(rss)

        # Write JSON (full detail for n8n)
        json_payload = [
            {
                "title": it["title"],
                "link": it["link"],
                "doctor": it["doctor"],
                "published_at": it["pub_dt"].isoformat(),
                "text": it["text"],
                "therapy": it["therapy"],
                "images": it["images"],
                "id": it["id"],
            }
            for it in items
        ]
        with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
            json.dump(json_payload, f, ensure_ascii=False, indent=2)

        # save state for stability (not required for 24h filter; kept anyway)
        for it in items:
            seen.add(it["id"])
        save_state(STATE_PATH, seen)

        browser.close()

    print(f"Wrote {OUTPUT_RSS} and {OUTPUT_JSON} with {len(items)} items (last 24h).")

if __name__ == "__main__":
    main()
