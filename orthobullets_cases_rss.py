#!/usr/bin/env python3
import os, re, json, hashlib, time
from datetime import datetime, timezone
from urllib.parse import urljoin
from lxml import html as lxml_html
from playwright.sync_api import sync_playwright

LIST_URL   = os.environ.get("OTB_URL", "https://www.orthobullets.com/Site/ElasticSearch/StandardSearchTiles?contentType=5&s=1,2,3,225,6,7,10")
XPATH      = os.environ.get("OTB_XPATH", '//div[contains(@class,"dashboard-item--case")]//a[@href]')
EMAIL      = os.environ.get("OTB_EMAIL", "")
PASSWORD   = os.environ.get("OTB_PASSWORD", "")
OUTPUT_RSS = os.environ.get("OTB_OUTPUT", "dist/orthobullets_cases.xml")
OUTPUT_JSON= os.environ.get("OTB_JSON",   "dist/orthobullets_cases.json")
STATE_PATH = os.environ.get("OTB_STATE",  "dist/orthobullets_cases_seen.json")
UA         = os.environ.get("OTB_UA", "Mozilla/5.0 (Orthobullets RSS Bot)")
MAX_ITEMS  = int(os.environ.get("OTB_MAX_ITEMS", "50"))

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

def login_and_get_links(pw):
    """Login with email+password, open LIST_URL, return case links from the specific XPath."""
    if not EMAIL or not PASSWORD:
        raise SystemExit("Missing OTB_EMAIL or OTB_PASSWORD env vars.")

    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(user_agent=UA, viewport={"width":1280,"height":1600})
    page = context.new_page()

    # Go directly to list URL; if redirected to login, fill credentials
    page.goto(LIST_URL, wait_until="domcontentloaded")
    if "/Site/Account/Login" in page.url or "login" in page.url.lower():
        # Try common selectors for ASP.NET Identity / OpenID form
        # Email
        for sel in ['input[name="Email"]', 'input#Email', 'input[type="email"]', 'input[name="Username"]']:
            if page.locator(sel).count():
                page.fill(sel, EMAIL); break
        # Password
        for sel in ['input[name="Password"]', 'input#Password', 'input[type="password"]']:
            if page.locator(sel).count():
                page.fill(sel, PASSWORD); break
        # Submit
        # Try button with type submit or text
        if page.locator('button[type="submit"]').count():
            page.click('button[type="submit"]')
        elif page.get_by_role("button", name=re.compile("sign in|log in", re.I)).count():
            page.get_by_role("button", name=re.compile("sign in|log in", re.I)).click()
        else:
            page.press('input[type="password"]', "Enter")

        page.wait_for_load_state("domcontentloaded")
        # After login, navigate again to list url to ensure correct page
        page.goto(LIST_URL, wait_until="domcontentloaded")

    # Ensure we actually see the list content (page may lazy-load)
    # Scroll a bit to trigger tiles loading
    page.evaluate("window.scrollTo(0, document.body.scrollHeight/3)")
    page.wait_for_timeout(1000)

    html_text = page.content()
    # Collect links by XPath on the final HTML
    doc = lxml_html.fromstring(html_text)
    nodes = doc.xpath(XPATH)
    links, seen = [], set()
    for a in nodes:
        href = a.get("href") or ""
        if not href:
            continue
        if not href.startswith("http"):
            href = urljoin("https://www.orthobullets.com", href)
        # Prefer real case pages
        if "/Site/Cases/View/" not in href:
            continue
        if href in seen:
            continue
        seen.add(href)
        links.append(href)

    return browser, context, page, links[:MAX_ITEMS]

def extract_case(page, link):
    """Open the case link in the same logged-in context and pull title/doctor/image."""
    page.goto(link, wait_until="domcontentloaded")
    # OpenGraph title/image are reliable for social sharing
    og_title = page.locator('meta[property="og:title"]').get_attribute("content")
    og_img   = page.locator('meta[property="og:image"]').get_attribute("content")
    title = norm(og_title) if og_title else norm(page.title())
    image = og_img

    # Best-effort doctor/author from common spots
    doctor = ""
    # Try obvious author blocks first
    author_text = ""
    for sel in [
        '[class*="author"]', '[class*="Author"]',
        '.dashboard-item__author', '.case-author'
    ]:
        if page.locator(sel).count():
            author_text = page.locator(sel).inner_text()
            break
    if not author_text:
        # Fallback: search header region for "By ..."
        snippet = page.locator("body").inner_text()[:2000]
        m = re.search(r"\bBy\s+([^\n|•]+)", snippet, re.I)
        if m: author_text = m.group(1)

    doctor = norm(author_text or "")

    return {
        "title": title or "Untitled case",
        "link": link,
        "image": image,
        "doctor": doctor
    }

def build_rss(items):
    now = now_rfc2822()
    header = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">',
        "<channel>",
        f"<title>Orthobullets Cases (Login)</title>",
        f"<link>{esc(LIST_URL)}</link>",
        f"<description>RSS generated from a specific XPath; includes doctor & image (login session).</description>",
        f"<lastBuildDate>{now}</lastBuildDate>"
    ]
    body = []
    for it in items:
        body.append("<item>")
        body.append(f"<title>{esc(it['title'])}</title>")
        body.append(f"<link>{esc(it['link'])}</link>")
        body.append(f"<guid isPermaLink='false'>{esc(it['id'])}</guid>")
        body.append(f"<pubDate>{esc(it['pubDate'])}</pubDate>")
        if it['doctor']:
            body.append(f"<description>{esc('Doctor: ' + it['doctor'])}</description>")
        if it.get("image"):
            body.append(f"<enclosure url=\"{esc(it['image'])}\" type=\"image/jpeg\"/>")
            body.append(f"<media:content url=\"{esc(it['image'])}\" medium=\"image\"/>")
        body.append("</item>")
    footer = ["</channel>", "</rss>"]
    return "\n".join(header + body + footer)

def main():
    os.makedirs(os.path.dirname(OUTPUT_RSS) or ".", exist_ok=True)

    with sync_playwright() as pw:
        browser, context, page, links = login_and_get_links(pw)
        if not links:
            browser.close()
            raise SystemExit("No case links found—check XPATH or that login succeeded.")

        seen = load_state(STATE_PATH)
        items = []
        for link in links:
            info = extract_case(page, link)
            info["id"] = guid(link)
            info["pubDate"] = now_rfc2822() if info["id"] not in seen else now_rfc2822()
            items.append(info)
            seen.add(info["id"])

        # Write RSS
        rss = build_rss(items[:MAX_ITEMS])
        with open(OUTPUT_RSS, "w", encoding="utf-8") as f:
            f.write(rss)

        # Write JSON (for n8n)
        with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
            json.dump(items[:MAX_ITEMS], f, ensure_ascii=False, indent=2)

        save_state(STATE_PATH, seen)
        browser.close()

    print(f"Wrote {OUTPUT_RSS} and {OUTPUT_JSON} with {min(len(items), MAX_ITEMS)} items.")

if __name__ == "__main__":
    main()
