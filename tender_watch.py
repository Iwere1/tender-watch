#!/usr/bin/env python3
"""Bistar Tender Watch: scans public sources for tenders/EOIs/bids and emails a digest.

Usage:  python tender_watch.py            # normal run (emails new matches)
        python tender_watch.py --dry      # print matches, no email, no state saved
        python tender_watch.py --test-email
"""
import argparse, hashlib, json, os, re, smtplib, sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from time import mktime
from urllib.parse import quote_plus, urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
SEEN_FILE = ROOT / "seen.json"
FEEDS_FILE = ROOT / "feeds.txt"
UA = {"User-Agent": "Mozilla/5.0 (compatible; BistarTenderWatch/1.0)"}
MAX_AGE_DAYS = 30      # ignore feed items older than this
MIN_SCORE = 3          # needs a sector hit plus a notice-type or location hit
MAX_ITEMS = 60         # cap per digest

# ---------------------------------------------------------------- relevance
NOTICE = ["tender", "eoi", "expression of interest", "expressions of interest", "rfq", "rfp",
          "request for quotation", "request for proposal", "invitation to bid",
          "invitation to tender", "invitation for bid", "itt", "itb", "prequalification",
          "pre-qualification", "call for bids", "call for proposals", "bid notice",
          "procurement notice", "contract opportunity"]
SECTOR = ["borehole", "water well", "waterwell", "water supply", "water scheme", "water treatment",
          "water project", "water reticulation", "hydrogeolog", "drilling", "dredging", "pipeline",
          "flowline", "civil works", "civil engineering", "construction", "engineering services",
          "oil and gas", "oil & gas", "epc", "fabrication", "facility maintenance",
          "electrical installation", "road construction", "building works", "renovation",
          "rehabilitation", "geophysical", "pump", "sanitation"]
GEO = ["nigeria", "niger delta", "port harcourt", "rivers state", "bayelsa", "delta state",
       "akwa ibom", "cross river", "bonny", "onne", "warri", "lagos", "abuja", "ogoni"]
EXCLUDE = ["vacancy", "vacancies", "recruitment", "scholarship", "job opening", "hiring",
           "internship", "contract awarded", "award of contract", "notice of award"]

# ------------------------------------------------------------------ sources
SEARCH_QUERIES = [
    "tender borehole drilling Nigeria",
    "expression of interest water supply Nigeria",
    "invitation to tender civil engineering construction Niger Delta",
    "EOI oil and gas services Nigeria",
    "request for proposal water project Nigeria",
    "prequalification engineering construction Nigeria",
    "tender Port Harcourt Rivers State",
    "invitation to bid Bayelsa Delta Akwa Ibom",
    "NLNG NNPC contractors tender notice",
    "NCDMB NipeX tender notice",
    "RFQ pipeline maintenance fabrication Nigeria",
    "water treatment plant tender Nigeria",
    "UNICEF WASH borehole procurement Nigeria",
    "state government borehole tender",
    "oil and gas contractor prequalification exercise Nigeria",
]
# Portal pages scanned for tender-looking links. Edit freely; failures are reported, not fatal.
PAGES = [
    "https://nipexng.com/",
    "https://www.bpp.gov.ng/",
    "https://ncdmb.gov.ng/",
    "https://nddc.gov.ng/",
    "https://www.nigerialng.com/",
    "https://www.dgmarket.com/tenders/list.do?countryCode=NG",
]
WORLD_BANK = ("https://search.worldbank.org/api/v2/procnotices?format=json&rows=100"
              "&countryshortname_exact=Nigeria&srt=submission_date&order=desc")

DEADLINE = re.compile(r"(?:deadline|closing date|closes?|submission date|due)[^0-9]{0,30}"
                      r"(\d{1,2}(?:st|nd|rd|th)?[\s/.-]+(?:[A-Za-z]{3,9}|\d{1,2})[\s/.,-]+\d{2,4})", re.I)


def hit(term, text):
    return re.search(r"\b" + re.escape(term) + (r"\b" if len(term) <= 4 else ""), text) is not None


def score(text):
    t = text.lower()
    if any(hit(x, t) for x in EXCLUDE):
        return 0
    sector = min(sum(hit(x, t) for x in SECTOR), 3) * 2
    if not sector:
        return 0
    return sector + (1 if any(hit(x, t) for x in NOTICE) else 0) + (1 if any(hit(x, t) for x in GEO) else 0)


def item_id(title):
    t = re.sub(r" - [^-]{2,40}$", "", title.lower())          # drop " - Publisher" suffix
    return hashlib.sha1(re.sub(r"[^a-z0-9]", "", t)[:100].encode()).hexdigest()[:16]


def clean(html, n=220):
    return re.sub(r"\s+", " ", BeautifulSoup(html or "", "html.parser").get_text(" ")).strip()[:n]


def get(url):
    for _ in range(2):
        try:
            r = requests.get(url, headers=UA, timeout=25)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            err = e
    raise err


# ---------------------------------------------------------------- collectors
def from_feed(url, label=None):
    d = feedparser.parse(get(url).content)
    out, cutoff = [], datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    for e in d.entries:
        pub = None
        if getattr(e, "published_parsed", None):
            pub = datetime.fromtimestamp(mktime(e.published_parsed), timezone.utc)
            if pub < cutoff:
                continue
        src = (e.get("source") or {}).get("title") or label or d.feed.get("title", url)
        out.append(dict(title=clean(e.get("title"), 300), link=e.get("link", ""), source=src,
                        published=pub, summary=clean(e.get("summary"))))
    return out


def from_page(url):
    soup = BeautifulSoup(get(url).text, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        text = re.sub(r"\s+", " ", a.get_text(" ")).strip()
        link = urljoin(url, a["href"])
        if len(text) < 20 or link in seen or link.startswith(("mailto:", "javascript:")):
            continue
        seen.add(link)
        out.append(dict(title=text[:300], link=link, source=url.split("/")[2], published=None, summary=""))
    return out


def from_worldbank():
    data = get(WORLD_BANK).json().get("procnotices", [])
    out = []
    for n in data:
        title = n.get("bid_description") or n.get("project_name") or ""
        nid = n.get("id", "")
        out.append(dict(title=clean(title, 300) + " (World Bank, Nigeria)",
                        link=f"https://projects.worldbank.org/en/projects-operations/procurement-detail/{nid}",
                        source="World Bank", published=None,
                        summary=clean(f"{n.get('notice_type','')} {n.get('project_name','')} "
                                      f"deadline {n.get('submission_deadline_date','')}")))
    return out


def collect():
    jobs = []
    for q in SEARCH_QUERIES:
        jobs.append((f"Google News: {q}", lambda q=q: from_feed(
            f"https://news.google.com/rss/search?q={quote_plus(q + ' when:14d')}&hl=en-NG&gl=NG&ceid=NG:en")))
        jobs.append((f"Bing News: {q}", lambda q=q: from_feed(
            f"https://www.bing.com/news/search?q={quote_plus(q)}&format=rss")))
    if FEEDS_FILE.exists():
        for line in FEEDS_FILE.read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                jobs.append((f"Feed: {line.strip()}", lambda u=line.strip(): from_feed(u)))
    jobs += [(f"Page: {u}", lambda u=u: from_page(u)) for u in PAGES]
    jobs.append(("World Bank notices", from_worldbank))

    items, errors, ok = [], [], 0
    for name, fn in jobs:
        try:
            items += fn()
            ok += 1
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}")
    return items, errors, ok


# --------------------------------------------------------------------- email
def build_email(items, errors):
    date = datetime.now().strftime("%d %b %Y")
    rows, txt = [], []
    for it in items:
        meta = " · ".join(x for x in [it["source"], it["published"].strftime("%d %b") if it["published"] else "",
                                       f"Deadline: {it['deadline']}" if it["deadline"] else ""] if x)
        rows.append(f'<p style="margin:0 0 14px"><a href="{escape(it["link"])}" style="font-weight:600;'
                    f'color:#0b57d0;text-decoration:none">{escape(it["title"])}</a><br>'
                    f'<span style="color:#666;font-size:12px">{escape(meta)}</span></p>')
        txt.append(f'- {it["title"]}\n  {meta}\n  {it["link"]}')
    foot = f'<p style="color:#999;font-size:11px">Sources with errors: {escape("; ".join(errors))}</p>' if errors else ""
    html = (f'<div style="font-family:Arial,sans-serif;font-size:14px;max-width:680px">'
            f'<h3 style="margin:0 0 16px">Tender Watch: {len(items)} new ({date})</h3>{"".join(rows)}{foot}</div>')
    text = f"Tender Watch: {len(items)} new ({date})\n\n" + "\n\n".join(txt)
    return f"Tender Watch: {len(items)} new opportunities ({date})", html, text


def send(subject, html, text):
    host = os.getenv("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.getenv("SMTP_PORT") or 465)
    user, pw = os.environ["SMTP_USER"], os.environ["SMTP_PASS"]
    to = os.getenv("MAIL_TO") or user
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    if port == 465:
        s = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        s = smtplib.SMTP(host, port, timeout=30)
        s.starttls()
    with s:
        s.login(user, pw)
        s.sendmail(user, [x.strip() for x in to.split(",")], msg.as_string())


# ---------------------------------------------------------------------- main
def load_seen():
    try:
        return json.loads(SEEN_FILE.read_text())
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--test-email", action="store_true")
    args = ap.parse_args()

    if args.test_email:
        send("Tender Watch test", "<p>Email setup works.</p>", "Email setup works.")
        print("Test email sent.")
        return

    seen = load_seen()
    items, errors, ok = collect()
    new, batch = [], set()
    for it in items:
        s = score(it["title"] + " " + it["summary"])
        if s < MIN_SCORE:
            continue
        iid = item_id(it["title"])
        if iid in seen or iid in batch:
            continue
        batch.add(iid)
        m = DEADLINE.search(it["title"] + " " + it["summary"])
        it.update(score=s, id=iid, deadline=m.group(1) if m else "")
        new.append(it)
    new.sort(key=lambda x: (-x["score"], x["published"] is None,
                            -(x["published"].timestamp() if x["published"] else 0)))
    new = new[:MAX_ITEMS]

    print(f"{ok} sources ok, {len(errors)} failed, {len(new)} new matches")
    if args.dry:
        for it in new:
            print(f'[{it["score"]}] {it["title"]}\n     {it["link"]} ({it["source"]}) {it["deadline"]}')
        print("Errors:", errors)
        return

    if ok == 0:
        send("Tender Watch: all sources failed", "<p>Every source failed. Check the workflow logs.</p>",
             "Every source failed. Check the workflow logs.")
        sys.exit(1)
    if new:
        send(*build_email(new, errors))
        now = datetime.now(timezone.utc).isoformat()
        for it in new:
            seen[it["id"]] = now
    cutoff = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    SEEN_FILE.write_text(json.dumps({k: v for k, v in seen.items() if v > cutoff}, indent=0))


if __name__ == "__main__":
    main()
