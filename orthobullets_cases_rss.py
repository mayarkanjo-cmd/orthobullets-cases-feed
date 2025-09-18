#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orthobullets Cases -> RSS + JSON (login, last-24h filter)
- Logs in with Playwright
- Reads the LIST page, finds each case tile and its relative age ("x hours ago")
- Filters to last 24 hours using that age (fallback to per-page meta if needed)
- For kept cases, opens each case to pull: title, doctor, text, therapy, images
- Always writes dist/orthobullets_cases.xml and dist/orthobullets_cases.json
"""

import os, re, json, hashlib, sys, traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from lxml import html as lxml_html
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------------- Env ----------------
LIST_URL   = os.environ.get("OTB_URL", "https://www.orthobullets.com/Site/ElasticSearch/StandardSearchTiles?contentType=5&s=1,2,3,225,6,7,10")
# XPATH to a single anchor somewhere inside each case tile (used only as fallback)
XPATH_ANCHOR = os.environ.get("OTB_XPATH", '//div[contains(@class,"dashboard-item--case")]//a[@href]')
# XPATH to the whole case tile (we parse age text from inside the tile)
XPATH_TILE  = os.environ.get("OTB_TILE_XPATH", '//div[contains(@class,"dashboard-item--case")]')

EMAIL      = os.environ.get("OTB_EMAIL", "")
PASSWORD   = os.environ.get("OTB_PASSWORD", "")
OUTPUT_RSS = os.environ.get("OTB_OUTPUT", "dist/orthobullets_cases.xml")
OUTPUT_JSON= os.environ.get("OTB_JSON",   "dist/orthobullets_cases.json")
UA         = os.environ.get("OTB_UA", "Mozilla/5.0 (Orthobullets RSS Bot)")
MAX_ITEMS  = int(os.environ.get("OTB_MAX_ITEMS", "60"))
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

def parse_relative_age_to_dt(text: str) -> datetime | None:
    """
    Accepts strings like '2 hours ago', '1 day ago', 'a week ago'
    Returns an approximate UTC datetime by subtracting a timedelta from now.
    """
    if not text: return None
    t = text.lower()
    # normalize 'a'/'an' to 1
    t = re.sub(r"\b(an|a)\b", "1", t)
    m = re.search(r"(\d+)\s*(minute|hour|day|week|month|year)s?\s*ago", t, re.I)
    if not m: 
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("minute"):
        delta = timedelta(minutes=n)
    elif unit.startswith("hour"):
        delta = timedelta(hours=n)
    elif unit.startswith("day"):
        delta = timedelta(days=n)
    elif unit.startswith("week"):
        delta = timedelta(days=7*n)
    elif unit.startswith("month"):
        delta = timedelta(days=30*n)
    elif unit.startswith("year"):
        delta = timedelta(days=365*n)
    else:
        return None
    return now_utc() - delta

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

# ---------------- Playwright ----------------
def login_and_collect_tiles(pw):
    if not EMAIL or not PASSWORD:
        raise RuntimeError("Missing OTB_EMAIL or OTB_PASSWORD.")

    log(f"Opening list URL: {LIST_URL}")
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(user_agent=UA, viewport={"width": 1400, "height": 1800})
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

    # Nudge lazy tiles
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
    except Exception:
        pass

    html_text = page.content()
    doc = lxml_html.fromstring(html_text)

    tiles = doc.xpath(XPATH_TILE)
    log(f"Tile XPath matched {len(tiles)} tiles.")
    out = []

    # If no tiles (layout changed), fall back to anchors
    if not tiles:
        anchors = doc.xpath(XPATH_ANCHOR)
        log(f"Fallback: anchor XPath matched {len(anchors)} anchors.")
        for a in anchors:
            href = abs_url("https://www.orthobullets.com", a.get("href"))
            if not href or "/Site/Cases/View/" not in href: 
                continue
            out.append({"link": href, "pub_dt": None})
        return browser, context, page, out[:MAX_ITEMS]

    # Normal path: parse each tile's link + relative age
    seen = set()
    for tile in tiles:
        # link
        a = tile.xpath('.//a[@href][1]')
        if not a:
            continue
        href = abs_url("https://www.orthobullets.com", a[0].get("href"))
        if not href or "/Site/Cases/View/" not in href:
            continue
        if href in seen:
            continue
        seen.add(href)

        # relative age text
        raw = norm(" ".join(tile.itertext()))
        pub_dt = parse_relative_age_to_dt(raw)

        out.append({"link": href, "pub_dt": pub_dt})

    log(f"Collected {len(out)} case tiles (with relative times where available).")
    return browser, context, page, out[:MAX_ITEMS]

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

    # Parse with lxml for text/therapy and (as fallback) published date
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
        "images": images,
        "published_at": published_at,  # datetime or None
        "text": text,
        "therapy": therapy,
    }

# ---------------- Output builders ----------------
def build_rss(items):
    now = rfc2822(now_utc())
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

# ---------------- Main ----------------
def main() -> int:
    cutoff = now_utc() - timedelta(hours=24)
    kept = []

    try:
        with sync_playwright() as pw:
            browser, context, page, tiles = login_and_collect_tiles(pw)
            log(f"Tiles returned: {len(tiles)}")

            # Pre-filter by list-page relative age
            prelim = []
            for t in tiles:
                link = t["link"]
                pub_dt = t.get("pub_dt")  # may be None if no '... ago' text
                if pub_dt and pub_dt < cutoff:
                    continue
                prelim.append(t)

            log(f"Pre-filter (by list relative time) kept {len(prelim)} tiles.")

            # For each prefiltered tile, open the case and pull details
            for t in prelim:
                link = t["link"]
                try:
                    data = extract_case(page, link)
                except Exception as e:
                    log(f"ERROR extracting {link}: {e}")
                    if DEBUG: traceback.print_exc()
                    continue

                # Decide final pub_dt: prefer tile’s relative time; else page meta; else now()
                pub_dt = t.get("pub_dt")
                if not pub_dt and data["published_at"]:
                    pub_dt = data["published_at"]
                if not pub_dt:
                    # could not determine date; treat as older than 24h (skip)
                    continue

                if pub_dt < cutoff:
                    continue

                kept.append({
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
        log(f"FATAL during run: {e}")
        if DEBUG: traceback.print_exc()

    kept.sort(key=lambda x: x["pub_dt"], reverse=True)
    kept = kept[:MAX_ITEMS]

    # Always write outputs (even if 0 items)
    rss = build_rss(kept)
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
    } for it in kept]
    safe_write_json(OUTPUT_JSON, json_payload)

    log(f"Wrote {OUTPUT_RSS} and {OUTPUT_JSON} with {len(kept)} items (last 24h).")
    return 0  # keep the workflow green

if __name__ == "__main__":
    sys.exit(main())
