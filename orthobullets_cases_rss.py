#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orthobullets Cases -> RSS + JSON (login, XPath, last-24h filter, full scrape)
Outputs:
  dist/orthobullets_cases.xml
  dist/orthobullets_cases.json

Secrets needed:
  OTB_EMAIL, OTB_PASSWORD

Environment (defaults shown):
  OTB_URL, OTB_XPATH, OTB_OUTPUT, OTB_JSON, OTB_STATE, OTB_UA, OTB_MAX_ITEMS, OTB_INCLUDE_UNDATED
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

def rfc2822(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")

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

def abs_url(base: str, href: str | None) -> str | None:
    if not href: return None
    return href if href.startswith("http") else urljoin(base, href)

def parse_iso_dt(s: str) -> datetime | None:
    if not s: return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        pass
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(:\d{2})?)", s)
    if m:
        try:
            hhmmss = m.group(2) if len(m.groups()) >= 2 else "00:00:00"
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
        raise SystemExit("Missing OTB_EMAIL or OTB_PASSWORD.")

    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 1800})
    page = context.new_page()

    page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60_000)
    if "/Site/Account/Login" in page.url.lower() or "login" in page.url.lower():
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
        page.wait_for_load_state("domcontentl_
