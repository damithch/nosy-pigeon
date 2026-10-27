#!/usr/bin/env python3
"""
nosy-pigeon: watches a few web pages and pings you when something new shows up.

Config comes from the PIGEON_CONFIG environment variable (a JSON string, kept
as a repo secret) or, locally, from config.json. See config.example.json.

Notifier environment variables (all optional, use at least one):
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
  DISCORD_WEBHOOK_URL
  WHATSAPP_PHONE + WHATSAPP_APIKEY   (CallMeBot)

Flags:
  --dry-run              print matches, send nothing, don't update seen.json
  --seed                 remember everything currently visible, send nothing
  --verbose              print matched titles (never use in public CI logs)
  --file NAME=PATH       read source NAME from a saved local file (repeatable)
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import feedparser
import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
SEEN_PATH = ROOT / "seen.json"
SEEN_LIMIT = 5000
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
LOCAL_TZ = timedelta(hours=5, minutes=30)


# ---------- helpers ----------

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def load_config():
    raw = os.getenv("PIGEON_CONFIG")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            sys.exit(f"PIGEON_CONFIG is not valid JSON (line {exc.lineno}, col {exc.colno})")
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        sys.exit("No config. Set the PIGEON_CONFIG secret, or create config.json from config.example.json.")
    return cfg


def fingerprint(item_id):
    """Store hashes, not links, so the public seen.json reveals nothing."""
    return hashlib.sha256(item_id.encode("utf-8")).hexdigest()[:20]


def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def contains_any(text, words):
    t = (text or "").lower()
    hits = []
    for w in words:
        w = w.lower()
        # word-boundary match so short tokens like "qa" don't match inside words
        if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", t):
            hits.append(w)
    return hits


def fetch(url, method="GET", json_body=None, params=None):
    """HTTP or a local file path (for offline tests). Returns bytes."""
    if url and Path(url).exists():
        return Path(url).read_bytes()
    resp = requests.request(method, url, json=json_body, params=params, timeout=20,
                            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    resp.raise_for_status()
    return resp.content


def slug_title(url):
    """'.../careers/283/2026-09-account-manager-sri-lanka' -> 'Account Manager Sri Lanka'"""
    seg = [x for x in urlparse(url).path.split("/") if x]
    slug = seg[-1] if seg else ""
    slug = re.sub(r"^\d{4}-\d{2}-", "", slug)                 # leading 2026-09-
    slug = re.sub(r"(-ref-?\d+|-copy|-\d{1,2})$", "", slug, flags=re.I)
    words = re.sub(r"[-_]+", " ", slug).strip()
    return " ".join(w if w.isupper() and len(w) > 1 else w.capitalize() for w in words.split())


def new_item(name, src, *, id, title, link, place="", kind="", published=None, body="", closing=""):
    return {"id": id, "source": name, "company": True, "title": title.strip(), "org": src.get("org") or src.get("label", name),
            "place": place or src.get("place", ""), "kind": kind or "", "closing": closing,
            "link": link, "published": published, "body": body or ""}


def epoch_ms(v):
    try:
        return datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def iso(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        try:
            return datetime.strptime(str(v)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


# ---------- source type: rss ----------

def labelled_field(text, label, all_labels):
    others = "|".join(re.escape(l) for l in all_labels)
    m = re.search(rf"{re.escape(label)}:\s*(.+?)(?=\s+(?:{others}):|$)", text)
    return m.group(1).strip() if m else ""


def read_rss(name, src, get):
    feed = feedparser.parse(get(src["url"]))
    if feed.bozo and not feed.entries:
        raise ValueError("feed could not be parsed")
    labels = src.get("field_labels", {})  # e.g. {"org": "Company", "place": "Location", "kind": "Type"}
    all_labels = list(labels.values()) + src.get("extra_labels", [])
    items = []
    for entry in feed.entries:
        raw = entry.content[0].get("value", "") if entry.get("content") else entry.get("summary", "")
        body = strip_html(raw)
        published = None
        if entry.get("published_parsed"):
            published = datetime.fromtimestamp(time.mktime(entry.published_parsed), tz=timezone.utc)
        title = html.unescape(entry.get("title", "")).strip()
        org = labelled_field(body, labels["org"], all_labels) if "org" in labels else ""
        org = org or src.get("org") or entry.get("author", "") or (src.get("label") if src.get("company") else "")
        if org:  # titles look like "Thing at Org": keep just the thing
            title = re.sub(rf"\s+at\s+{re.escape(org)}\s*$", "", title, flags=re.I)
        place = labelled_field(body, labels["place"], all_labels) if "place" in labels else ""
        items.append({
            "id": entry.get("id") or entry.get("link"),
            "source": name,
            "title": title,
            "org": org,
            "place": place or src.get("place") or (strip_html(entry.get("description", "")).split(":", 1)[-1].strip()[:80] if labels else ""),
            "kind": labelled_field(body, labels["kind"], all_labels) if "kind" in labels else "",
            "closing": "",
            "link": entry.get("link", ""),
            "published": published,
            "body": body,
        })
    return items


# ---------- source type: table (rows with a JS onclick holding link params) ----------

def read_table(name, src, get):
    page = get(src["url"]).decode("utf-8", errors="replace")
    row_re = re.compile(
        rf"<tr id=\"tr\d+\"\s+onclick=\"{re.escape(src['onclick_fn'])}\('(\d+)','([^']*)','([^']*)','([^']*)'[^\"]*\">(.*?)</tr>",
        re.S,
    )
    items = []
    for rid, ac, jc, ec, row in row_re.findall(page):
        row = re.sub(r"<!--.*?-->", "", row, flags=re.S)
        t = re.search(r"<h2>\s*<span>(.*?)</span>", row, re.S)
        o = re.search(r"<h1>(.*?)</h1>", row, re.S)
        cells = [strip_html(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        # cells: [#, ref, title+org, description, opened, closes, place]
        title = strip_html(t.group(1)) if t else ""
        title = re.sub(r"\s*\(\d+\)\s*$", "", title).lstrip(".?� -").strip()
        opened = cells[4] if len(cells) > 4 else ""
        try:
            published = datetime.strptime(opened, "%a %b %d %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            published = None
        items.append({
            "id": f"{name}:{jc}",
            "source": name,
            "title": title,
            "org": strip_html(o.group(1)).lstrip("?� ").strip() if o else "",
            "place": cells[6] if len(cells) > 6 else "",
            "kind": "",
            "closing": cells[5] if len(cells) > 5 else "",
            "link": src["link_template"].format(rid=rid, ac=ac, jc=jc, ec=ec),
            "published": published,
            "body": cells[3] if len(cells) > 3 else "",
        })
    return items


# ---------- company sources: hiring-system feeds ----------

def read_lever(name, src, get):
    data = json.loads(get(f"https://api.lever.co/v0/postings/{src['company']}?mode=json"))
    return [new_item(name, src, id=f"lever:{j['id']}", title=j.get("text", ""), link=j.get("hostedUrl", ""),
                     place=(j.get("categories") or {}).get("location", ""),
                     kind=(j.get("categories") or {}).get("commitment", ""),
                     published=epoch_ms(j.get("createdAt")), body=j.get("descriptionPlain", ""))
            for j in data]


def read_workable(name, src, get):
    data = json.loads(get(f"https://apply.workable.com/api/v1/widget/accounts/{src['company']}"))
    return [new_item(name, src, id=f"workable:{j.get('shortcode') or j.get('url')}", title=j.get("title", ""),
                     link=j.get("url") or j.get("shortlink", ""),
                     place=", ".join(x for x in (j.get("city"), j.get("country")) if x),
                     kind=j.get("employment_type", ""), published=iso(j.get("published_on")))
            for j in data.get("jobs", [])]


def read_smartrecruiters(name, src, get):
    url = f"https://api.smartrecruiters.com/v1/companies/{src['company']}/postings?limit=100"
    if src.get("country"):
        url += f"&country={src['country']}"
    data = json.loads(get(url))
    out = []
    for j in data.get("content", []):
        loc = j.get("location") or {}
        out.append(new_item(name, src, id=f"sr:{j['id']}", title=j.get("name", ""),
                            link=f"https://jobs.smartrecruiters.com/{src['company']}/{j['id']}",
                            place=", ".join(x for x in (loc.get("city"), (loc.get("country") or "").upper()) if x),
                            kind=(j.get("typeOfEmployment") or {}).get("label", ""),
                            published=iso(j.get("releasedDate"))))
    return out


def read_workday(name, src, get):
    host, tenant, site = src["host"], src["tenant"], src["site"]
    body = {"limit": 20, "offset": 0, "searchText": src.get("search", ""), "appliedFacets": {}}
    data = json.loads(get(f"https://{host}/wday/cxs/{tenant}/{site}/jobs", method="POST", json_body=body))
    return [new_item(name, src, id=f"wd:{j.get('externalPath')}", title=j.get("title", ""),
                     link=f"https://{host}/{site}{j.get('externalPath', '')}", place=j.get("locationsText", ""))
            for j in data.get("jobPostings", [])]


def read_recruiterbox(name, src, get):
    data = json.loads(get(f"https://jsapi.recruiterbox.com/v1/openings?client_name={src['company']}&limit=100"))
    out = []
    for j in data.get("objects", []):
        loc = j.get("location") or {}
        out.append(new_item(name, src, id=f"rbox:{j['id']}", title=j.get("title", ""),
                            link=j.get("hosted_url", ""),
                            place=", ".join(x for x in (loc.get("city"), loc.get("state"), loc.get("country")) if x),
                            kind=(j.get("position_type") or "").replace("_", " "),
                            body=strip_html(j.get("description", ""))))
    return out


# ---------- company sources: careers pages ----------

GENERIC_TEXT = re.compile(r"^(read more|view more|view|more|apply|apply now|find out more|more details|details|"
                          r"learn more|see more|view job|view details)?$", re.I)


def page_jobpostings(page):
    """schema.org JobPosting objects embedded as JSON-LD (what Google for Jobs reads)."""
    found = []
    for m in re.finditer(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", page, re.S | re.I):
        try:
            stack = [json.loads(m.group(1).strip())]
        except ValueError:
            continue
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack += x
            elif isinstance(x, dict):
                t = x.get("@type")
                if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                    found.append(x)
                stack += [v for v in x.values() if isinstance(v, (list, dict))]
    return found


def read_jsonld(name, src, get):
    page = get(src["url"]).decode("utf-8", errors="replace")
    out = []
    for j in page_jobpostings(page):
        loc = j.get("jobLocation") or {}
        loc = loc[0] if isinstance(loc, list) and loc else loc
        addr = (loc.get("address") or {}) if isinstance(loc, dict) else {}
        place = ", ".join(x for x in (addr.get("addressLocality"), addr.get("addressCountry")) if isinstance(x, str) and x)
        link = j.get("url") or src["url"]
        out.append(new_item(name, src, id=f"ld:{link}#{j.get('title', '')}", title=strip_html(j.get("title", "")),
                            link=link, place=place, kind=str(j.get("employmentType") or ""),
                            published=iso(j.get("datePosted")), body=strip_html(j.get("description", ""))[:3000],
                            closing=str(j.get("validThrough") or "")[:10]))
    return out


def read_links(name, src, get):
    """Job links on a careers page, picked out by a URL pattern from the config."""
    page = get(src["url"]).decode("utf-8", errors="replace")
    pattern = re.compile(src["pattern"], re.I)
    cut = re.compile(src["title_cut"], re.I) if src.get("title_cut") else None
    by_link = {}
    for m in re.finditer(r"<a\b[^>]*?href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", page, re.S | re.I):
        href = urljoin(src["url"], html.unescape(m.group(1)).strip())
        if not pattern.search(href):
            continue
        u = urlparse(href)
        link = urlunparse(u._replace(query="", fragment="")) if src.get("drop_query", True) else href
        text = strip_html(m.group(2))
        if cut and text:
            text = cut.split(text)[0].strip(" -–|·")
        prev = by_link.get(link, "")
        if len(text) > len(prev) and not GENERIC_TEXT.match(text):
            by_link[link] = text
        else:
            by_link.setdefault(link, prev)
    out = []
    for link, text in by_link.items():
        title = slug_title(link) if src.get("title_from") == "slug" or not text or GENERIC_TEXT.match(text) else text
        if src.get("title_strip"):
            title = re.sub(src["title_strip"], "", title, flags=re.I).strip()
        out.append(new_item(name, src, id=f"link:{link}", title=title, link=link,
                            body=text if src.get("title_from") == "slug" else ""))
    return out


READERS = {"rss": read_rss, "table": read_table, "lever": read_lever, "workable": read_workable,
           "smartrecruiters": read_smartrecruiters, "workday": read_workday, "recruiterbox": read_recruiterbox,
           "jsonld": read_jsonld, "links": read_links}
COMPANY_TYPES = {"lever", "workable", "smartrecruiters", "workday", "recruiterbox", "jsonld", "links"}


def due(name, src, now_min):
    """Company pages are checked about hourly, staggered so they don't all go at once."""
    every = int(src.get("every_min", 60 if src["type"] in COMPANY_TYPES or src.get("company") else 5))
    if every <= 5:
        return True
    offset = (int(fingerprint(name), 16) % (every // 5)) * 5
    return (now_min - offset) % every < 5


def location_ok(item, src):
    want = src.get("location_any")
    if not want:
        return True
    place = "" if item["place"] == src.get("place") else item["place"]  # ignore the config's default place
    hay = " ".join([place, item["title"], item["body"][:400], re.sub(r"[-_/]", " ", item["link"])])
    return bool(contains_any(hay, want))


# ---------- filtering ----------

def matches(item, cfg):
    title = item["title"]
    if not contains_any(title, cfg["title_include"]):
        return False, []
    # company pages list every department, so their titles must also name tech work
    if item.get("company") and cfg.get("company_title_require") and \
            not contains_any(title, cfg["company_title_require"]):
        return False, []
    if contains_any(title, cfg.get("title_exclude", [])):
        return False, []
    # For "A / B" or "A | B" titles, keep it if any option is wanted and not too senior.
    options = [a for a in re.split(r"\s[/|]\s|\s*\|\s*", title) if a.strip()]
    if not any(contains_any(a, cfg["title_include"]) and
               not contains_any(a, cfg.get("seniority_exclude", []))
               for a in options):
        return False, []
    if contains_any(item["body"], cfg.get("body_exclude", [])):
        return False, []
    places = cfg.get("allowed_locations") or []
    if places and item["place"] and not contains_any(item["place"], places):
        return False, []
    max_age = cfg.get("max_age_days")
    if max_age and item["published"]:
        if (datetime.now(timezone.utc) - item["published"]).days > max_age:
            return False, []
    return True, contains_any(item["body"], cfg.get("tech_keywords", []))


# ---------- notifiers ----------

def message_lines(item, tags, label, bold):
    lines = [
        f"🐦 {bold(item['title'])}",
        f"🏢 {item['org']}",
        f"📍 {item['place'] or '—'}" + (f"  ·  {item['kind']}" if item["kind"] else ""),
    ]
    if tags:
        lines.append(f"🧰 {', '.join(tags)}")
    if item["closing"]:
        lines.append(f"⏳ Closes {item['closing']}")
    elif item["published"]:
        lines.append(f"🕒 {(item['published'] + LOCAL_TZ).strftime('%a %d %b, %I:%M %p')}")
    lines.append(f"🌐 {label}")
    return lines


def telegram_text(token, chat, text):
    """Send HTML text to every chat id in TELEGRAM_CHAT_ID ("111,222"). True if any got it."""
    ok_any = False
    for i, cid in enumerate(c.strip() for c in chat.split(",") if c.strip()):
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": cid, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=20,
        )
        if r.ok:
            ok_any = True
        else:  # recipient number only, never the id: logs are public
            print(f"  telegram recipient {i + 1}: HTTP {r.status_code}", file=sys.stderr)
    return ok_any


def roll_call(cfg):
    """Tell the perch which windows the pigeon is watching (names come only from the secret config)."""
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        sys.exit("roll call needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    boards, companies = [], []
    for name, src in cfg.get("sources", {}).items():
        if not src.get("enabled", True):
            continue
        is_company = src["type"] in COMPANY_TYPES or src.get("company")
        (companies if is_company else boards).append(html.escape(src.get("label", name)))
    lines = ["🐦 <b>Nosy Pigeon roll call</b>", f"Watching {len(boards) + len(companies)} places.", ""]
    if boards:
        lines += ["<b>Job boards</b> (every 5 min)"] + [f"• {b}" for b in boards] + [""]
    if companies:
        lines += [f"<b>Companies</b> ({len(companies)}, about hourly)"] + [f"• {c}" for c in sorted(companies, key=str.lower)]
    ok = telegram_text(token, chat, "\n".join(lines))
    print(f"roll call: {len(boards)} boards, {len(companies)} companies, sent={ok}")


def send_telegram(item, tags, label):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    e = html.escape
    esc = {k: e(v) if isinstance(v, str) else v for k, v in item.items()}
    lines = message_lines(esc, [e(t) for t in tags], e(label), lambda s: f"<b>{s}</b>")
    lines.append(f'🔗 <a href="{e(item["link"])}">Open</a>')
    return telegram_text(token, chat, "\n".join(lines))


def send_discord(item, tags, label):
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if not url:
        return False
    embed = {
        "title": item["title"][:250],
        "url": item["link"],
        "description": "\n".join(message_lines(item, tags, label, lambda s: s)[1:])[:2000],
        "color": 0x2B7FFF,
    }
    payload = {"username": "Nosy Pigeon", "embeds": [embed]}
    r = requests.post(url, json=payload, timeout=20)
    if r.status_code == 429:
        time.sleep(float(r.json().get("retry_after", 2)))
        r = requests.post(url, json=payload, timeout=20)
    if not r.ok:
        print(f"  discord: HTTP {r.status_code}", file=sys.stderr)
    return r.ok


def send_whatsapp(item, tags, label):
    phone, key = os.getenv("WHATSAPP_PHONE"), os.getenv("WHATSAPP_APIKEY")
    if not (phone and key):
        return False
    lines = message_lines(item, tags, label, lambda s: f"*{s}*") + [f"🔗 {item['link']}"]
    r = requests.get(
        "https://api.callmebot.com/whatsapp.php",
        params={"phone": phone, "text": "\n".join(lines), "apikey": key},
        timeout=20,
    )
    ok = r.ok and "error" not in r.text.lower()[:300]
    if not ok:
        print(f"  whatsapp: HTTP {r.status_code}", file=sys.stderr)
    return ok


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--file", action="append", default=[], metavar="NAME=PATH")
    ap.add_argument("--all", action="store_true", help="check every source now, ignoring intervals")
    ap.add_argument("--roll-call", action="store_true", help="send the list of watched places to Telegram and stop")
    args = ap.parse_args()
    files = dict(f.split("=", 1) for f in args.file)
    verbose = args.verbose or args.dry_run

    cfg = load_config()
    if args.roll_call:
        roll_call(cfg)
        return
    labels, now_min = {}, int(time.time() // 60)
    jobs = []
    for i, (name, src) in enumerate(cfg.get("sources", {}).items(), 1):
        if not src.get("enabled", True):
            continue
        labels[name] = src.get("label", name)
        if args.all or name in files or due(name, src, now_min):
            jobs.append((i, name, src))

    def run(job):
        i, name, src = job
        local = files.get(name)
        getter = (lambda url, **kw: fetch(local)) if local else fetch
        try:
            return i, name, [x for x in READERS[src["type"]](name, src, getter) if location_ok(x, src)], None
        except Exception as exc:  # one broken source shouldn't stop the others
            return i, name, None, exc

    items, failures, ok_sources = [], 0, []
    with ThreadPoolExecutor(8) as pool:
        for i, name, found, exc in sorted(pool.map(run, jobs), key=lambda r: r[0]):
            if exc is not None:
                failures += 1
                # only the exception type: messages can contain the URL
                print(f"window {i}: failed ({type(exc).__name__})", file=sys.stderr)
                continue
            print(f"window {i}: {len(found)} items")
            ok_sources.append(name)
            items += found
    if jobs and not ok_sources:
        sys.exit("All windows failed.")

    channels = [n for n, on in (
        ("telegram", os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")),
        ("discord", os.getenv("DISCORD_WEBHOOK_URL")),
        ("whatsapp", os.getenv("WHATSAPP_PHONE") and os.getenv("WHATSAPP_APIKEY")),
    ) if on]
    if not channels and not (args.dry_run or args.seed):
        sys.exit("No notifier configured (see README).")

    seen = load_json(SEEN_PATH, [])
    # migrate any old plain-text ids to fingerprints
    seen = [s if re.fullmatch(r"[0-9a-f]{20}", s) else fingerprint(s) for s in seen]
    seen_set = set(seen)

    # A source seen for the first time is learned silently, so adding a company never floods you.
    new_fps = []
    fresh = {n for n in ok_sources if fingerprint(f"seeded:{n}") not in seen_set}
    for n in fresh:
        new_fps.append(fingerprint(f"seeded:{n}"))
        seen_set.add(new_fps[-1])
    learned = 0
    for item in items:
        if item["source"] in fresh:
            fp = fingerprint(item["id"])
            if fp not in seen_set:
                seen_set.add(fp); new_fps.append(fp); learned += 1
    if fresh:
        print(f"{len(fresh)} new window(s): remembered {learned} items quietly")

    items.sort(key=lambda x: x["published"] or datetime.min.replace(tzinfo=timezone.utc))
    sent, matched = 0, 0
    for item in items:
        fp = fingerprint(item["id"])
        if fp in seen_set:
            continue
        seen_set.add(fp)
        ok, tags = matches(item, cfg)
        if args.seed or not ok:
            new_fps.append(fp)
            continue
        matched += 1
        if verbose:
            print(f"  MATCH [{labels[item['source']]}] {item['title']} - {item['org']} [{item['place'] or '-'}] {', '.join(tags)}")
        if args.dry_run:
            continue
        label = labels[item["source"]]
        if any([send_telegram(item, tags, label), send_discord(item, tags, label), send_whatsapp(item, tags, label)]):
            new_fps.append(fp)
            sent += 1
            time.sleep(1)
        # if every channel failed, leave it unseen so the next run retries

    if args.dry_run:
        print(f"[dry run] {len(items)} items, {matched} would be sent.")
        return
    SEEN_PATH.write_text(json.dumps((seen + new_fps)[-SEEN_LIMIT:], indent=0) + "\n", encoding="utf-8")
    if args.seed:
        print(f"Remembered {len(new_fps)} items.")
    else:
        print(f"{len(items)} items checked, {sent} ping(s) sent.")


if __name__ == "__main__":
    main()
