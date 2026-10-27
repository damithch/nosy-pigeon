# 🐦 nosy-pigeon

> *Some birds sing. This one just watches.*

A small, nosy bird that sits on the rooftops of the internet, keeps an eye on a few windows, and coos on Telegram when something new appears. The rest of the time it stays quiet.

## 🪶 The Pigeon Code

**F**lies a quiet patrol every five minutes, day and night.
**I**gnores the shiny noise; it has seen enough of that.
**N**ever coos about the same thing twice. A pigeon remembers.
**D**rops a single note when something new shows up below.
**M**ost birds wander. This one always knows its way home.
**Y**ou won't see it working; you'll just hear it when it matters.
**N**o crumbs are left behind in the logs.
**E**very window it watches was chosen on purpose.
**S**ome call it nosy. It prefers *well-informed*.
**T**he pigeon is patient; the right perch always turns up.

## 🧭 Pigeon Sense

Pigeons are famous for one thing: wherever they're released, they find their way back to where they belong. Nobody fully knows how. Some say magnetism, some say the sun, some say they just *know*.

This pigeon works the same way. It doesn't search everywhere; it watches the right places and waits.

## 🏠 Rooftops it watches
The pigeon keeps its rooftops secret. Its real map lives in a repo secret called **`PIGEON_CONFIG`** (a JSON string), so nobody else can see where it flies.

`config.example.json` shows the shape of the map:
- `sources`: windows to watch. Close one with `"enabled": false`. Kinds of window:
  - `rss` feeds and `table` pages: busy rooftops, checked every flight
  - `lever`, `workable`, `smartrecruiters`, `workday`, `recruiterbox`: nests that publish their own feed (needs `company`, Workday also `host`/`tenant`/`site`)
  - `links`: any page; crumbs are links matching `pattern`, titled from the link text or the link itself (`"title_from": "slug"`)
  - `jsonld`: pages that describe their crumbs in schema.org JSON-LD
  - per window: `location_any` (only crumbs from these places), `place` (default place), `every_min` (nests are visited about hourly)
- `title_include`: crumbs it likes
- `company_title_require`: nest crumbs must also carry one of these words
- `seniority_exclude`: crumbs too big for its beak, checked per option in titles like "A / B"
- `title_exclude`: crumbs it never touches
- `body_exclude`: phrases that put it off its food
- `tech_keywords`: shiny bits it mentions when it spots them
- `buttons`: words on the two buttons under each coo (`{"done": "...", "pass": "..."}`); taps are kept in the loft
- `people`: names for the perches (`{"<chat id>": "name"}`), used when storing taps
- `rank`: which crumbs get ⭐⭐⭐ or ⭐⭐ (`{"3": [...], "2": [...]}`); everything else gets ⭐
- `allowed_locations`: rooftops it cares about (empty means all)
- `max_age_days`: stale crumbs are ignored
- `warn_after`: how many failed visits in a row before the pigeon says a window is stuck (default 6)
- `stale_days`: a nest that has gone quiet this many days gets a gentle note (default 7)
- `summary_hour`: local hour after which yesterday's check-in is sent (default 8)

A new window is learned quietly on its first visit, so adding one never floods the perch.

For local practice, copy it to `config.json` (ignored by git).

## 📯 Where it coos
Repo secrets (**Settings → Secrets and variables → Actions**):

| Secret | Perch |
|---|---|
| `PIGEON_CONFIG` | the secret map (required) |
| `PIGEON_DB_URL` | Postgres connection string for the loft, its long-term memory (optional; see `loft/001_nest.sql`) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram (home perch; several chat ids allowed, comma-separated) |
| `DISCORD_WEBHOOK_URL` | Discord (optional) |
| `WHATSAPP_PHONE`, `WHATSAPP_APIKEY` | WhatsApp via CallMeBot (optional) |

It needs **Settings → Actions → General → Workflow permissions → Read and write** to keep its memory (`seen.json`). Memories are stored as fingerprints, not links, so they mean nothing to anyone else.

## 🛠️ Training the bird
```bash
pip install -r requirements.txt
python pigeon.py --dry-run                     # practice flight; no cooing
python pigeon.py --dry-run --file a=feed.xml   # practice on a saved copy of window "a"
python pigeon.py --seed                        # look around, remember everything, say nothing
python pigeon.py --dry-run --all               # visit every window now, ignoring the hourly rhythm
```

## 🌙 Notes from the rooftop
- The logs only ever say how many crumbs it saw, never what they were.
- If a window keeps failing, or a busy rooftop suddenly looks empty, the pigeon coos a ⚠️ warning, and a ✅ when it comes back.
- Every morning it sends a short check-in about the day before: flights flown, windows visited, new crumbs, coos sent.
- Its health notes live in `health.json`: fingerprints and counts only.
- If a coo doesn't get through, it tries again next round.
- GitHub's clock is a little lazy, so flights can be 5–15 minutes late.
- It flies every 5 minutes around the clock. Pigeons in public repos get free Actions minutes, so no rest is needed.

---

<sub>*Read the Code from top to bottom, one letter at a time.* 🐦</sub>
