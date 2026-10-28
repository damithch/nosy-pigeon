"""The loft: the pigeon's long-term memory in Postgres (optional).

Set PIGEON_DB_URL to a Postgres connection string and every flight is written
to the `pigeon` schema (see loft/001_nest.sql). If the variable is missing or
the database can't be reached, the pigeon carries on exactly as before: the loft
never decides what gets sent, it only remembers.
"""

import sys

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:  # the loft is optional
    psycopg = None


def why(exc):
    """A short, safe reason (never the message itself: it can contain the host)."""
    m = str(exc).lower()
    for key, words in (("password", ("password authentication", "authentication failed")),
                       ("tenant", ("tenant or user not found", "tenant")),
                       ("dns", ("resolve host", "name or service not known", "nodename")),
                       ("timeout", ("timeout", "timed out")),
                       ("refused", ("refused",)),
                       ("ssl", ("ssl",)),
                       ("url", ("invalid dsn", "missing", "invalid connection option", "invalid uri"))):
        if any(w in m for w in words):
            return key
    return type(exc).__name__


class Loft:
    last_problem = None   # read by the health notes

    def __init__(self, conn):
        self.conn = conn

    @classmethod
    def open(cls, url):
        if not url:
            Loft.last_problem = "no url"
            return None
        if psycopg is None:
            Loft.last_problem = "no driver"
            print("loft: psycopg not installed", file=sys.stderr)
            return None
        try:
            # prepare_threshold=None keeps it friendly with connection poolers
            conn = psycopg.connect(url, connect_timeout=10, prepare_threshold=None,
                                   options="-c statement_timeout=30000")
            Loft.last_problem = None
            return cls(conn)
        except Exception as exc:  # never print the message: it can contain the host
            Loft.last_problem = why(exc)
            print(f"loft: unavailable ({Loft.last_problem})", file=sys.stderr)
            return None

    def record(self, windows, results, items, stats):
        """windows: {key: (label, type)}; results: {key: (n_items or None, error or None)};
        items: dicts from the readers, with optional _fp/_liked/_stars/_tags/_cooed;
        stats: checks/errors/new/sent. Returns True if everything was stored."""
        try:
            with self.conn.transaction(), self.conn.cursor() as cur:
                cur.executemany(
                    """insert into pigeon.windows (key, label, kind) values (%s, %s, %s)
                       on conflict (key) do update set label = excluded.label, kind = excluded.kind,
                       updated_at = now()
                       where (windows.label, windows.kind) is distinct from (excluded.label, excluded.kind)""",
                    [(k, label, kind) for k, (label, kind) in windows.items()])
                cur.execute(
                    """insert into pigeon.flights (checks, errors, fresh, cooed)
                       values (%s, %s, %s, %s) returning id""",
                    (stats.get("checks", 0), stats.get("errors", 0), stats.get("new", 0), stats.get("sent", 0)))
                flight = cur.fetchone()[0]
                cur.executemany(
                    """insert into pigeon.visits (flight_id, window_key, ok, items, error)
                       values (%s, %s, %s, %s, %s)""",
                    [(flight, k, err is None, n, err) for k, (n, err) in results.items()])
                rows, done = [], set()
                for it in items:
                    if it["_fp"] in done:
                        continue
                    done.add(it["_fp"])
                    rows.append((it["_fp"], it["source"], it["title"][:500], it.get("org"), it.get("place"),
                                 it.get("kind"), it.get("link"), it.get("published"), it.get("_liked"),
                                 it.get("_stars"), it.get("_tags"), it.get("_cooed")))
                cur.executemany(
                    """insert into pigeon.crumbs (fp, window_key, title, org, place, kind, link, published_at,
                                                 liked, stars, tags, cooed_at)
                       values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       on conflict (fp) do update set
                         last_seen_at = now(),
                         title = excluded.title, org = excluded.org, place = excluded.place,
                         kind = excluded.kind, link = excluded.link,
                         published_at = coalesce(excluded.published_at, crumbs.published_at),
                         liked = coalesce(excluded.liked, crumbs.liked),
                         stars = coalesce(excluded.stars, crumbs.stars),
                         tags = coalesce(excluded.tags, crumbs.tags),
                         cooed_at = coalesce(crumbs.cooed_at, excluded.cooed_at)""",
                    rows)
            print(f"loft: stored {len(results)} visits, {len(rows)} crumbs")
            return True
        except Exception as exc:
            Loft.last_problem = "write " + why(exc)
            print(f"loft: write failed ({Loft.last_problem})", file=sys.stderr)
            try:
                self.conn.rollback()
            except Exception:
                pass
            return False

    def peck(self, fp, who, verdict):
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("""insert into pigeon.pecks (fp, who, verdict)
                           select %s, %s, %s where exists (select 1 from pigeon.crumbs where fp = %s)
                           on conflict (fp, who) do update set verdict = excluded.verdict, at = now()""",
                        (fp, who, verdict, fp))

    def unpeck(self, fp, who):
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("delete from pigeon.pecks where fp = %s and who = %s", (fp, who))

    # ---- applications: what a perch went for, summarised for later ----

    def pending_applications(self, limit=3):
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("""select a.id, a.who, a.title, a.org, a.link, a.attempts, c.window_key
                           from pigeon.applications a join pigeon.crumbs c using (fp)
                           where a.summary_state = 'pending' order by a.applied_at limit %s""", (limit,))
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def save_summary(self, app_id, state, raw_text=None, company=None, role=None, skills=None, closing=None):
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("""update pigeon.applications set summary_state = %s, raw_text = coalesce(%s, raw_text),
                             summary_company = %s, summary_role = %s, skills = %s, closing = %s,
                             attempts = attempts + 1, updated_at = now() where id = %s""",
                        (state, raw_text, company, role, skills, closing, app_id))

    def retry_later(self, app_id, give_up_after=4):
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("""update pigeon.applications set attempts = attempts + 1, updated_at = now(),
                             summary_state = case when attempts + 1 >= %s then 'failed' else summary_state end
                           where id = %s returning summary_state""", (give_up_after, app_id))
            return cur.fetchone()[0]

    # ---- the oracle's opinions and the perches' past choices ----

    def recent_choices(self, limit=15):
        """[(verdict, title, org)] most recent first, for teaching the oracle."""
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("""(select p.verdict, c.title, c.org from pigeon.pecks p join pigeon.crumbs c using (fp)
                            where p.verdict = 'done' and c.window_key <> 'practice' order by p.at desc limit %s)
                           union all
                           (select p.verdict, c.title, c.org from pigeon.pecks p join pigeon.crumbs c using (fp)
                            where p.verdict = 'pass' and c.window_key <> 'practice' order by p.at desc limit %s)""",
                        (limit, limit))
            return cur.fetchall()

    def save_opinions(self, rows):
        """rows: [(fp, fit, stars, reason)]; the crumbs must already be stored."""
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.executemany("""insert into pigeon.opinions (fp, fit, stars, reason)
                               select %s, %s, %s, %s where exists (select 1 from pigeon.crumbs where fp = %s)
                               on conflict (fp) do update set fit = excluded.fit, stars = excluded.stars,
                                 reason = excluded.reason, at = now()""",
                            [(fp, fit, st, rs, fp) for fp, fit, st, rs in rows])

    def note(self, key, value=None):
        """Read (value=None) or write a small JSON note."""
        with self.conn.transaction(), self.conn.cursor() as cur:
            if value is None:
                cur.execute("select value from pigeon.notes where key = %s", (key,))
                row = cur.fetchone()
                return row[0] if row else None
            cur.execute("""insert into pigeon.notes (key, value) values (%s, %s)
                           on conflict (key) do update set value = excluded.value, at = now()""",
                        (key, Jsonb(value)))

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass
