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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
SEEN_PATH = ROOT / "seen.json"
SEEN_LIMIT = 5000
USER_AGENT = "Mozilla/5.0 (compatible; nosy-pigeon/1.0)"
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


def get(source_or_path):
    if source_or_path and Path(source_or_path).exists():
        return Path(source_or_path).read_bytes()
    resp = requests.get(source_or_path, headers={"User-Agent": USER_AGENT}, timeout=15)
    resp.raise_for_status()
    return resp.content


# ---------- source type: rss ----------

def labelled_field(text, label, all_labels):
    others = "|".join(re.escape(l) for l in all_labels)
    m = re.search(rf"{re.escape(label)}:\s*(.+?)(?=\s+(?:{others}):|$)", text)
    return m.group(1).strip() if m else ""


def read_rss(name, src, data):
    feed = feedparser.parse(data)
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
        org = org or entry.get("author", "")
        if org:  # titles look like "Thing at Org": keep just the thing
            title = re.sub(rf"\s+at\s+{re.escape(org)}\s*$", "", title, flags=re.I)
        place = labelled_field(body, labels["place"], all_labels) if "place" in labels else ""
        items.append({
            "id": entry.get("id") or entry.get("link"),
            "source": name,
            "title": title,
            "org": org,
            "place": place or strip_html(entry.get("description", "")).split(":", 1)[-1].strip(),
            "kind": labelled_field(body, labels["kind"], all_labels) if "kind" in labels else "",
            "closing": "",
            "link": entry.get("link", ""),
            "published": published,
            "body": body,
        })
    return items


# ---------- source type: table (rows with a JS onclick holding link params) ----------

def read_table(name, src, data):
    page = data.decode("utf-8", errors="replace")
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


READERS = {"rss": read_rss, "table": read_table}


# ---------- filtering ----------

def matches(item, cfg):
    title = item["title"]
    if not contains_any(title, cfg["title_include"]):
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


def send_telegram(item, tags, label):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    e = html.escape
    esc = {k: e(v) if isinstance(v, str) else v for k, v in item.items()}
    lines = message_lines(esc, [e(t) for t in tags], e(label), lambda s: f"<b>{s}</b>")
    lines.append(f'🔗 <a href="{e(item["link"])}">Open</a>')
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": "\n".join(lines), "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=20,
    )
    if not r.ok:
        print(f"  telegram: HTTP {r.status_code}", file=sys.stderr)
    return r.ok


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
    args = ap.parse_args()
    files = dict(f.split("=", 1) for f in args.file)
    verbose = args.verbose or args.dry_run

    cfg = load_config()
    items, labels, failures = [], {}, 0
    for i, (name, src) in enumerate(cfg.get("sources", {}).items(), 1):
        if not src.get("enabled", True):
            continue
        labels[name] = src.get("label", name)
        try:
            found = READERS[src["type"]](name, src, get(files.get(name) or src["url"]))
            print(f"window {i}: {len(found)} items")
            if not found:
                print(f"  window {i}: 0 rows parsed, the layout may have changed", file=sys.stderr)
            items += found
        except Exception as exc:  # one broken source shouldn't stop the others
            failures += 1
            # only the exception type: messages can contain the URL
            print(f"window {i}: failed ({type(exc).__name__})", file=sys.stderr)
    if not items and failures:
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

    items.sort(key=lambda x: x["published"] or datetime.min.replace(tzinfo=timezone.utc))
    new_fps, sent, matched = [], 0, 0
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
