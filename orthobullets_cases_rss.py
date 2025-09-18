#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orthobullets Cases -> RSS + JSON (login with email/password, XPath filter, robust fallbacks)
Requires: playwright (chromium), lxml

ENV (defaults shown):
  OTB_EMAIL, OTB_PASSWORD                  # REQUIRED (GitHub Secrets)
  OTB_URL      = https://www.orthobullets.com/Site/ElasticSearch/StandardSearchTiles?contentType=5&s=1,2,3,225,6,7,10
  OTB_XPATH    = //div[contains(@class,"dashboard-item--case")]//a[@href]
  OTB_OUTPUT   = dist/orthobullets_cases.xml
  OTB_JSON     = dist/orthobullets_cases.json
  OTB_STATE    = dist/orthobullets_cases_seen.json
  OTB_UA       = Mozilla/5.0 (Orthobullets RSS Bot)
  OTB_MAX_ITEMS= 50
"""
import os, re, json, hashlib
from datetime import datetime, timezone
from urllib.parse import urljoin
from lxml import html as lxml_html
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# -------- Config (env) ----------
LIST_URL   = os.environ.get("OTB_URL", "https://www.orthobullets.com/Site/ElasticSearch/StandardSearchTiles?contentType=5&s=1,2,3,225,6,7,10")
XPATH      = os.environ.get("OTB_XPATH", '//div[contains(@class,"dashboard-item--case")]//a[@href]')
EMAIL      = os.environ.get("OTB_EMAIL", "")
PASSWORD   = os.environ.get("OTB_PASSWORD", "")
OUTPUT_RSS = os.environ.get("OTB_OUTPUT", "dist/orthobullets_cases.xml")
OUTPUT_JSON= os.environ.get("OTB_JSON",   "dist/orthobullets_cases.json")
STATE_PATH = os.environ.get("OTB_STATE",  "dist/orthobullets_cases_seen.json")
UA         = os.environ.get("OTB_UA", "Mozilla/5.0 (Orthobullets RSS Bot)")
MAX_ITEMS  = int(os.environ.get("OTB_MAX_ITEMS", "50"))
# ---------------------------------

def norm(s: str) -> str:
    return " ".join((s or "").split())

def guid(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()

def now_rfc2822() -> str:
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")

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

def login_and_collect_links(pw):
    """Login with EMAIL/PASSWORD if prompted, then open LIST_URL and extract links using XPATH."""
    if not EMAIL or not PASSWORD:
        raise SystemExit("Missing OTB_EMAIL or OTB_PASSWORD env vars.")

    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 1800})
    page = context.new_page()

    # Open list page; if redirected to login, submit credentials
    page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60_000)
    if "/Site/Account/Login" in page.url.lower() or "login" in page.url.lower():
        # Email field
        for sel in ['input[name="Email"]', 'input#Email', 'input[type="email"]', 'input[name="Username"]']:
            if page.locator(sel).count():
                page.fill(sel, EMAIL); break
        # Password field
        for sel in ['input[name="Password"]', 'input#Password', 'input[type="password"]']:
            if page.locator(sel).count():
                page.fill(sel, PASSWORD); break
        # Submit
        if page.locator('button[type="submit"]').count():
            page.click('button[type="submit"]')
        elif page.get_by_role("button", name=re.compile("sign in|log in", re.I)).count():
            page.get_by_role("button", name=re.compile("sign in|log in", re.I)).click()
        else:
            # Press Enter on password field
            page.keyboard.press("Enter")

        page.wait_for_load_state("domcontentloaded", timeout=60_000)
        # Navigate again to ensure we are on the list view after login
        page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60_000)

    # Nudge lazy content
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
    except Exception:
        pass

    html_text = page.content()
    doc = lxml_html.fromstring(html_text)
    nodes = doc.xpath(XPATH)

    links, seen = [], set()
    for a in nodes:
        href = (a.get("href") or "").strip()
        if not href:
            continue
        if not href.startswith("http"):
            href = urljoin("https://www.orthobullets.com", href)
        # Prefer real case pages; loosen if needed
        if "/Site/Cases/View/" not in href:
            continue
        if href in seen:
            continue
        seen.add(href)
        links.append(href)

    return browser, context, page, links[:MAX_ITEMS]

def extract_case(page, link):
    """Open the case detail and safely extract title, image, doctor with fallbacks."""
    page.goto(link, wait_until="domcontentloaded", timeout=60_000)

    # --- Title ---
    title = None
    try:
        title = page.locator('meta[property="og:title"]').first.get_attribute("content", timeout=5_000)
    except PWTimeout:
        title = None
    if not title:
        try:
            title = page.title()
        except Exception:
            title = link
    title = norm(title)

    # --- Image ---
    image = None
    try:
        image = page.locator('meta[property="og:image"]').first.get_attribute("content", timeout=5_000)
    except PWTimeout:
        image = None
    if not image:
        try:
            img_src = page.locator("img").first.get_attribute("src", timeout=5_000)
            if img_src:
                image = img_src if img_src.startswith("http") else urljoin(link, img_src)
        except Exception:
            image = None

    # --- Doctor / Author ---
    doctor = ""
    try:
        for sel in ['[class*="author"]', '[class*="Author"]', '.dashboard-item__author', '.case-author']:
            if page.locator(sel).count():
                doctor = norm(page.locator(sel).inner_text())
                break
        if not doctor:
            # last-resort, light heuristic for "By <name>"
            snippet = page.locator("body").inner_text()[:2000]
            m = re.search(r"\bBy\s+([^\n|•]+)", snippet, re.I)
            if m:
                doctor = norm(m.group(1))
    except Exception:
        doctor = ""

    return {"title": title or "Untitled case", "link": link, "image": image, "doctor": doctor}

def build_rss(items):
    now = now_rfc2822()
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">',
        "<channel>",
        f"<title>Orthobullets Cases (Login)</title>",
        f"<link>{esc(LIST_URL)}</link>",
        f"<description>RSS generated from a specific XPath; includes doctor & image (login session).</description>",
        f"<lastBuildDate>{now}</lastBuildDate>",
    ]
    for it in items:
        parts.append("<item>")
        parts.append(f"<title>{esc(it['title'])}</title>")
        parts.append(f"<link>{esc(it['link'])}</link>")
        parts.append(f"<guid isPermaLink='false'>{esc(it['id'])}</guid>")
        parts.append(f"<pubDate>{esc(it['pubDate'])}</pubDate>")
        if it['doctor']:
            parts.append(f"<description>{esc('Doctor: ' + it['doctor'])}</description>")
        if it.get("image"):
            parts.append(f"<enclosure url=\"{esc(it['image'])}\" type=\"image/jpeg\"/>")
            parts.append(f"<media:content url=\"{esc(it['image'])}\" medium=\"image\"/>")
        parts.append("</item>")
    parts += ["</channel>", "</rss>"]
    return "\n".join(parts)

def main():
    os.makedirs(os.path.dirname(OUTPUT_RSS) or ".", exist_ok=True)

    with sync_playwright() as pw:
        browser, context, page, links = login_and_collect_links(pw)
        if not links:
            browser.close()
            raise SystemExit("No case links found — check XPATH or that login succeeded.")

        seen = load_state(STATE_PATH)
        items = []
        for link in links:
            info = extract_case(page, link)
            info["id"] = guid(link)
            info["pubDate"] = now_rfc2822() if info["id"] not in seen else now_rfc2822()
            items.append(info)
            seen.add(info["id"])

        # RSS
        rss = build_rss(items[:MAX_ITEMS])
        with open(OUTPUT_RSS, "w", encoding="utf-8") as f:
            f.write(rss)

        # JSON (n8n-friendly)
        with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
            json.dump(items[:MAX_ITEMS], f, ensure_ascii=False, indent=2)

        save_state(STATE_PATH, seen)
        browser.close()

    print(f"Wrote {OUTPUT_RSS} and {OUTPUT_JSON} with {min(len(items), MAX_ITEMS)} items.")

if __name__ == "__main__":
    main()
