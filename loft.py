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


class Loft:
    def __init__(self, conn):
        self.conn = conn

    @classmethod
    def open(cls, url):
        if not url:
            return None
        if psycopg is None:
            print("loft: psycopg not installed", file=sys.stderr)
            return None
        try:
            # prepare_threshold=None keeps it friendly with connection poolers
            conn = psycopg.connect(url, connect_timeout=10, prepare_threshold=None,
                                   options="-c statement_timeout=30000")
            return cls(conn)
        except Exception as exc:  # never print the message: it can contain the host
            print(f"loft: unavailable ({type(exc).__name__})", file=sys.stderr)
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
            print(f"loft: write failed ({type(exc).__name__})", file=sys.stderr)
            try:
                self.conn.rollback()
            except Exception:
                pass
            return False

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
