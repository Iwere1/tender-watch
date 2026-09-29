#!/usr/bin/env python3
"""Bistar Tender Watch: finds tenders, EOIs and bids that fit Bistar's work and emails a digest.

Usage:  python tender_watch.py              # normal run (emails new matches)
        python tender_watch.py --dry        # print matches, no email, nothing saved
        python tender_watch.py --test-email
"""
import argparse, hashlib, json, os, re, smtplib, sys
from concurrent.futures import ThreadPoolExecutor
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
UA = {"User-Agent": "Mozilla/5.0 (compatible; BistarTenderWatch/2.0)"}
MAX_AGE_DAYS = 30
MIN_SCORE = 3
MAX_NIGERIA = 50
MAX_INTERNATIONAL = 10

# ===================================================== WHAT BISTAR DOES
# name: (terms, weight). Edit these lists to widen or narrow what gets through.
CATEGORIES = {
    "Water wells and boreholes": (["borehole", "boreholes", "water well", "water wells", "waterwell",
        "deep well", "well drilling", "water drilling", "hydrogeolog", "groundwater", "water abstraction",
        "water supply", "water scheme", "water project", "water reticulation", "water treatment",
        "potable water", "submersible pump", "solar-powered water", "solar powered water", "water tank",
        "overhead tank", "water point", "motorised borehole", "motorized borehole"], 3),
    "Roads, bridges and civil works": (["road construction", "construction of road", "construction of roads",
        "rehabilitation of road", "reconstruction of road", "road rehabilitation", "road project",
        "dualisation", "dualization", "asphalt", "culvert", "bridge construction", "construction of bridge",
        "drainage", "erosion", "flood control", "shore protection", "civil works", "civil engineering",
        "earthworks", "access road", "interlocking", "paving", "embankment", "retaining wall"], 3),
    "Building construction": (["building construction", "construction of building", "commercial building",
        "construction of office", "office complex", "construction of hostel", "construction of school",
        "construction of classroom", "construction of hospital", "construction of health",
        "housing estate", "construction of estate", "construction of houses", "building works",
        "renovation", "remodelling", "remodeling", "fit-out", "construction of complex",
        "construction of hotel", "construction of mall", "construction of market",
        "administrative block", "construction of block"], 3),
    "Oil spill and land remediation": (["remediation", "oil spill", "spill response", "spill clean",
        "clean-up", "cleanup", "clean up", "contaminated site", "contaminated land",
        "hydrocarbon pollution", "hydrocarbon impacted", "hydrocarbon-impacted", "bioremediation",
        "land reclamation", "site restoration", "environmental restoration", "decontamination",
        "hyprep", "impacted site"], 3),
    "Oilfield civil and site works": (["well pad", "site preparation", "right of way", "right-of-way",
        "flow station", "tank farm", "facility civil", "camp construction", "site clearing",
        "jetty", "slipway", "lease road"], 3),
    "General construction works": (["construction of", "rehabilitation of", "reconstruction of",
        "engineering works", "civil and structural", "structural works", "maintenance works"], 1),
}
CATEGORY_ORDER = list(CATEGORIES)

NOTICE = ["tender", "tenders", "eoi", "expression of interest", "expressions of interest", "rfq", "rfp",
          "request for quotation", "request for proposal", "request for qualification",
          "invitation to bid", "invitation to tender", "invitation for bid", "itt", "itb",
          "prequalification", "pre-qualification", "call for bids", "call for proposals", "bid notice",
          "procurement notice", "invitation to pre-qualify", "invites bids", "invites tenders"]

NIG_BUYERS = ["nddc", "nnpc", "nlng", "ncdmb", "hyprep", "nosdra", "shell", "spdc", "snepco",
              "totalenergies", "total energies", "chevron", "renaissance", "seplat", "agip", "naoc",
              "exxonmobil", "oando", "addax", "sahara energy", "first e&p", "aiteo", "waltersmith",
              "ministry of works", "ministry of water resources", "ruwassa", "federal government",
              "state government", "nigerian ports", "nimasa", "fct"]
INTL_BUYERS = ["unicef", "undp", "fao", "world bank", "african development bank", "afdb", "usaid", "ungm"]
NIGERIA = ["nigeria", "niger delta", "port harcourt", "rivers state", "bayelsa", "delta state",
           "akwa ibom", "cross river", "edo state", "imo state", "abia", "anambra", "enugu", "lagos",
           "abuja", "ogun", "oyo", "kaduna", "kano", "borno", "adamawa", "bonny", "onne", "warri",
           "ogoni", "yenagoa", "uyo", "calabar", "owerri", "asaba", "benin city"]
OTHER = ["ghana", "kenya", "uganda", "tanzania", "ethiopia", "cameroon", "senegal", "sierra leone",
         "liberia", "gambia", "zambia", "malawi", "mozambique", "south africa", "angola",
         "ivory coast", "cote d'ivoire", "equatorial guinea", "togo", "india", "pakistan", "bangladesh",
         "united kingdom", "philippines", "nepal"]
EXCLUDE = ["vacancy", "vacancies", "recruitment", "scholarship", "job opening", "hiring", "internship",
           "contract awarded", "award of contract", "notice of award", "drilling rig", "offshore drilling",
           "subsea", "drilling fluid"]

# ============================================================ WHERE TO LOOK
SEARCH_QUERIES = [
    # water wells and boreholes
    "borehole drilling tender Nigeria", "water well drilling tender Nigeria",
    "borehole rehabilitation maintenance tender Nigeria", "water supply scheme tender state government Nigeria",
    "RUWASSA water tender", "UNICEF UNDP borehole tender Nigeria",
    # roads and civil works
    "road construction tender Nigeria", "road rehabilitation tender Rivers State",
    "NDDC invitation to tender", "Federal Ministry of Works tender roads",
    "drainage erosion control tender Nigeria", "civil works EOI Niger Delta",
    # buildings
    "building construction tender Nigeria", "construction of hospital school tender state Nigeria",
    "commercial building construction invitation contractors Nigeria",
    # remediation
    "oil spill remediation tender Nigeria", "HYPREP contractors remediation",
    "land remediation EOI Niger Delta", "contaminated site clean-up tender Nigeria",
    # oil companies and agencies
    "NNPC tender civil works", "NLNG tender contractors", "Shell SPDC contractors prequalification",
    "TotalEnergies Nigeria tender contractors", "Chevron Nigeria contractors tender",
    "Renaissance Africa Energy tender", "Seplat tender contractors", "NCDMB tender notice",
    # outside Nigeria
    "Ghana borehole tender", "Ghana road construction tender", "Ghana civil works EOI",
]
PAGES = [   # portal pages scanned for tender-looking links; failures are reported, not fatal
    "https://nipexng.com/", "https://www.bpp.gov.ng/", "https://ncdmb.gov.ng/",
    "https://nddc.gov.ng/", "https://www.nigerialng.com/",
    "https://www.dgmarket.com/tenders/list.do?countryCode=NG",
]
WORLD_BANK = ("https://search.worldbank.org/api/v2/procnotices?format=json&rows=100"
              "&countryshortname_exact=Nigeria&srt=submission_date&order=desc")

DEADLINE = re.compile(r"(?:deadline|closing date|closes?|submission date|due)[^0-9]{0,30}"
                      r"(\d{1,2}(?:st|nd|rd|th)?[\s/.-]+(?:[A-Za-z]{3,9}|\d{1,2})[\s/.,-]+\d{2,4})", re.I)


# ============================================================== relevance
def hit(term, text):
    return re.search(r"\b" + re.escape(term) + (r"\b" if len(term) <= 4 else ""), text) is not None


def analyse(text):
    t = text.lower()
    if any(hit(x, t) for x in EXCLUDE) or not any(hit(x, t) for x in NOTICE):
        return None
    cats = {}
    for name, (terms, weight) in CATEGORIES.items():
        found = [x for x in terms if hit(x, t)]
        if found:
            cats[name] = (weight, found)
    if not cats:
        return None
    nb = [b for b in NIG_BUYERS if hit(b, t)]
    ib = [b for b in INTL_BUYERS if hit(b, t)]
    nig = [g for g in NIGERIA if hit(g, t)]
    other = [g for g in OTHER if hit(g, t)]
    score = min(sum(w for w, _ in cats.values()), 6) + (2 if nb or ib else 0) + (1 if nig or nb else 0)
    if score < MIN_SCORE:
        return None
    primary = max(cats, key=lambda c: (cats[c][0], len(cats[c][1])))
    international = bool(other) and not nig and not nb
    why = cats[primary][1][:2] + [b.upper() if len(b) <= 6 else b.title() for b in (nb + ib)[:1]]
    place = other[0].title() if international else (nig[0].title() if nig else "")
    return dict(score=score, category=primary, why=why, international=international, place=place)


def parse_date(s):
    s = re.sub(r"(\d)(st|nd|rd|th)", r"\1", s.strip().rstrip(",."), flags=re.I)
    s = re.sub(r"[,\s]+", " ", s)
    for text, fmts in ((s, ("%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y")),
                       (re.sub(r"[-/.]", " ", s), ("%d %B %Y", "%d %b %Y", "%d %B %y"))):
        for f in fmts:
            try:
                return datetime.strptime(text, f)
            except ValueError:
                pass
    return None


def item_id(title):
    t = re.sub(r" - [^-]{2,40}$", "", title.lower())
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


# ============================================================== collectors
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
    out = []
    for n in get(WORLD_BANK).json().get("procnotices", []):
        title = n.get("bid_description") or n.get("project_name") or ""
        out.append(dict(title=clean(title, 300) + " (World Bank, Nigeria)",
                        link="https://projects.worldbank.org/en/projects-operations/procurement-detail/"
                             + str(n.get("id", "")),
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

    def run(job):
        try:
            return job[0], job[1](), None
        except Exception as e:
            return job[0], [], type(e).__name__

    items, errors, ok = [], [], 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for name, res, err in pool.map(run, jobs):
            if err:
                errors.append(f"{name} ({err})")
            else:
                ok += 1
                items += res
    return items, errors, ok


# ================================================================== email
def render_item(it):
    bits = [f"Source: {it['source']}"]
    if it["published"]:
        bits.append(f"Published: {it['published'].strftime('%d %B')}")
    if it["deadline"]:
        bits.append(f"Closing date: {it['deadline']}")
    if it["place"]:
        bits.append(f"Location: {it['place']}")
    meta = ". ".join(bits) + "."
    why = "Why it fits: " + ", ".join(it["why"]) + "." if it["why"] else ""
    html = (f'<p style="margin:0 0 16px"><a href="{escape(it["link"])}" style="color:#0b57d0;'
            f'font-weight:600;text-decoration:none">{escape(it["title"])}</a><br>'
            f'<span style="color:#555">{escape(meta)}<br>{escape(why)}</span></p>')
    text = f"{it['title']}\n{meta} {why}\n{it['link']}\n"
    return html, text


def build_email(items, errors):
    n = len(items)
    day = datetime.now().strftime("%d %B %Y")
    found = "one new opportunity" if n == 1 else f"{n} new opportunities"
    intro = (f"Good morning,\n\nI went through today's tender and EOI notices and found {found} that "
             f"fit Bistar's work. They are grouped by type of work, with the closest matches first.")
    ngr = [i for i in items if not i["international"]]
    intl = [i for i in items if i["international"]]
    sections = [(c, [i for i in ngr if i["category"] == c]) for c in CATEGORY_ORDER]
    sections.append(("Outside Nigeria (Ghana and elsewhere)", intl))

    html = [f'<div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.5;max-width:680px">',
            "".join(f"<p>{escape(p)}</p>" for p in intro.split("\n\n"))]
    text = [intro]
    for name, rows in sections:
        if not rows:
            continue
        html.append(f'<h3 style="margin:22px 0 10px;font-size:15px">{escape(name)} ({len(rows)})</h3>')
        text.append(f"\n{name.upper()} ({len(rows)})\n")
        for it in rows:
            h, t = render_item(it)
            html.append(h)
            text.append(t)
    close = ("Please confirm the closing dates and requirements on the original notice before acting, "
             "as I read them automatically and can occasionally get one wrong.")
    html.append(f"<p>{close}</p><p>Kind regards,<br>Bistar Tender Watch</p>")
    text.append(f"\n{close}\n\nKind regards,\nBistar Tender Watch")
    if errors:
        note = "A few sources could not be reached today: " + "; ".join(errors[:8]) + "."
        html.append(f'<p style="color:#999;font-size:11px">{escape(note)}</p>')
        text.append("\n" + note)
    html.append("</div>")
    subject = f"Tender opportunities for Bistar: {n} new ({day})"
    return subject, "".join(html), "\n".join(text)


def send(subject, html, text):
    host = os.getenv("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.getenv("SMTP_PORT") or 465)
    user, pw = os.environ["SMTP_USER"], os.environ["SMTP_PASS"]
    to = os.getenv("MAIL_TO") or user
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, f"Bistar Tender Watch <{user}>", to
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


# =================================================================== main
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
        send("Tender Watch test", "<p>Good morning, the email setup is working.</p>",
             "Good morning, the email setup is working.")
        print("Test email sent.")
        return

    seen = load_seen()
    items, errors, ok = collect()
    new, batch, today = [], set(), datetime.now()
    no_notice_word = irrelevant = already_sent = expired = 0
    sample_titles = []
    for it in items:
        text = it["title"] + " " + it["summary"]
        if len(sample_titles) < 8 and it["title"]:
            sample_titles.append(it["title"])
        a = analyse(text)
        if not a:
            t = text.lower()
            if not any(hit(x, t) for x in NOTICE):
                no_notice_word += 1
            else:
                irrelevant += 1
            continue
        iid = item_id(it["title"])
        if iid in seen or iid in batch:
            already_sent += 1
            continue
        m = DEADLINE.search(text)
        dl = m.group(1) if m else ""
        d = parse_date(dl) if dl else None
        if d and d.date() < today.date():
            expired += 1
            continue   # closing date already passed
        batch.add(iid)
        it.update(a, id=iid, deadline=dl)
        new.append(it)
    print(f"DEBUG: {len(items)} raw items | no notice-word: {no_notice_word} | "
          f"off-topic: {irrelevant} | already sent: {already_sent} | expired: {expired}")
    print("DEBUG sample titles seen:")
    for s in sample_titles:
        print("  -", s)

    def order(x):
        return (-x["score"], x["published"] is None, -(x["published"].timestamp() if x["published"] else 0))
    ngr = sorted([i for i in new if not i["international"]], key=order)[:MAX_NIGERIA]
    intl = sorted([i for i in new if i["international"]], key=order)[:MAX_INTERNATIONAL]
    new = ngr + intl

    print(f"{ok} sources ok, {len(errors)} failed, {len(new)} new matches")
    if args.dry:
        for it in new:
            print(f'[{it["score"]}] {it["category"]} | {it["title"]}\n     {it["link"]} ({it["source"]}) {it["deadline"]}')
        print("Errors:", errors)
        return

    if ok == 0:
        send("Tender Watch could not reach any sources",
             "<p>Hello, none of the tender sources could be reached today. Please check the workflow log on GitHub.</p>",
             "Hello, none of the tender sources could be reached today. Please check the workflow log on GitHub.")
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
