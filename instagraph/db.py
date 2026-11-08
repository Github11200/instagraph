"""SQLite storage for the crawled graph.

users.deg is the number of *distinct* neighbours a user has in everything
crawled so far (maintained by a trigger). For an account that hasn't been
crawled itself, that is "how many crawled people it's connected to", which is
what the UI's "min. links" pruning uses.

users.status:  seen | crawling | partial | done | skipped | error
  partial = following list fetched but followers still pending (Instagram was
            temporarily blocking followers lists).
"""

from __future__ import annotations

import heapq
import json
import sqlite3
import threading
import time
from pathlib import Path

UNKNOWN_DEPTH = 99

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS users(
    pk TEXT PRIMARY KEY,
    username TEXT,
    full_name TEXT DEFAULT '',
    is_private INTEGER DEFAULT 0,
    is_verified INTEGER DEFAULT 0,
    pic TEXT,
    depth INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'seen',
    deg INTEGER NOT NULL DEFAULT 0,
    follower_count INTEGER,
    following_count INTEGER,
    note TEXT,
    crawled_at REAL
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_deg ON users(deg);
CREATE TABLE IF NOT EXISTS edges(
    src TEXT NOT NULL,   -- src follows dst
    dst TEXT NOT NULL,
    PRIMARY KEY(src, dst)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
CREATE TRIGGER IF NOT EXISTS edges_deg AFTER INSERT ON edges
WHEN NOT EXISTS (SELECT 1 FROM edges WHERE src = NEW.dst AND dst = NEW.src)
BEGIN
    UPDATE users SET deg = deg + 1 WHERE pk IN (NEW.src, NEW.dst);
END;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

MIGRATIONS = {
    "priority": "ALTER TABLE users ADD COLUMN priority INTEGER NOT NULL DEFAULT 0",
    "fg_done": "ALTER TABLE users ADD COLUMN fg_done INTEGER NOT NULL DEFAULT 0",
    "fr_done": "ALTER TABLE users ADD COLUMN fr_done INTEGER NOT NULL DEFAULT 0",
}

CRAWLED = ("done", "partial")

# Auto-crawl candidates (BFS frontier). :frok = followers lists currently allowed.
_CLAIMABLE = """
FROM users
WHERE (status = 'seen' OR (status = 'partial' AND :frok = 1))
  AND depth < :maxd
  AND (:skipv = 0 OR is_verified = 0 OR depth = 0)
  AND (is_private = 0 OR pk = :me
       OR EXISTS (SELECT 1 FROM edges WHERE src = :me AND dst = users.pk))
"""


class GraphDB:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(users)")}
        for col, sql in MIGRATIONS.items():
            if col not in cols:
                self.conn.execute(sql)
                if col in ("fg_done", "fr_done"):  # accounts crawled before this column existed
                    self.conn.execute(f"UPDATE users SET {col} = 1 WHERE status = 'done'")
        self.conn.executescript(
            """CREATE INDEX IF NOT EXISTS idx_users_pick ON users(status, depth, deg);
               CREATE INDEX IF NOT EXISTS idx_users_prio ON users(priority) WHERE priority > 0;"""
        )

    # ---------------------------------------------------------------- writes
    def upsert_users(self, users: list[dict], depth: int) -> None:
        rows = [
            (u["pk"], u.get("username"), u.get("full_name") or "", u.get("is_private", 0),
             u.get("is_verified", 0), u.get("pic"), depth)
            for u in users if u.get("pk") and u.get("pk") != "None"
        ]
        with self.lock:
            self.conn.execute("BEGIN")
            self.conn.executemany(
                """INSERT INTO users(pk, username, full_name, is_private, is_verified, pic, depth)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(pk) DO UPDATE SET
                     username = COALESCE(excluded.username, users.username),
                     full_name = CASE WHEN excluded.full_name != '' THEN excluded.full_name ELSE users.full_name END,
                     is_private = excluded.is_private,
                     is_verified = excluded.is_verified,
                     pic = COALESCE(excluded.pic, users.pic),
                     depth = MIN(users.depth, excluded.depth)""",
                rows,
            )
            self.conn.execute("COMMIT")

    def add_edges(self, edges: list[tuple[str, str]]) -> None:
        with self.lock:
            self.conn.execute("BEGIN")
            self.conn.executemany("INSERT OR IGNORE INTO edges(src, dst) VALUES(?, ?)", edges)
            self.conn.execute("COMMIT")

    def claim_next(self, max_depth: int, me: str, skip_verified: bool, followers_ok: bool) -> sqlite3.Row | None:
        """Atomically pick the next user to crawl and mark it 'crawling'.

        Manually expanded users (priority > 0) always go first; then the BFS
        frontier: closest to the seed first, most-connected first.
        """
        params = {"maxd": max_depth, "me": me or "", "skipv": 1 if skip_verified else 0,
                  "frok": 1 if followers_ok else 0}
        cols = "pk, username, depth, fg_done, fr_done, priority"
        with self.lock:
            row = self.conn.execute(
                f"""SELECT {cols} FROM users
                    WHERE priority > 0 AND status IN ('seen', 'partial', 'error', 'skipped')
                    ORDER BY priority DESC LIMIT 1"""
            ).fetchone()
            if row is None and max_depth > 0:
                row = self.conn.execute(
                    f"SELECT {cols} {_CLAIMABLE} ORDER BY depth ASC, deg DESC LIMIT 1", params
                ).fetchone()
            if row:
                self.conn.execute("UPDATE users SET status = 'crawling', priority = 0 WHERE pk = ?", (row["pk"],))
            return row

    def queue_size(self, max_depth: int, me: str, skip_verified: bool, followers_ok: bool) -> int:
        params = {"maxd": max_depth, "me": me or "", "skipv": 1 if skip_verified else 0,
                  "frok": 1 if followers_ok else 0}
        with self.lock:
            prio = self.conn.execute("SELECT COUNT(*) FROM users WHERE priority > 0").fetchone()[0]
            auto = self.conn.execute(f"SELECT COUNT(*) {_CLAIMABLE}", params).fetchone()[0] if max_depth > 0 else 0
            return prio + auto

    def prioritize(self, pk: str) -> bool:
        """Queue a user for manual expansion. Returns False if already fully crawled / in progress."""
        with self.lock:
            row = self.conn.execute("SELECT status FROM users WHERE pk = ?", (pk,)).fetchone()
            if not row or row["status"] in ("done", "crawling"):
                return False
            top = self.conn.execute("SELECT COALESCE(MAX(priority), 0) FROM users").fetchone()[0]
            self.conn.execute("UPDATE users SET priority = ? WHERE pk = ?", (top + 1, pk))
            return True

    def has_priority(self) -> bool:
        with self.lock:
            return self.conn.execute("SELECT 1 FROM users WHERE priority > 0 LIMIT 1").fetchone() is not None

    def priority_usernames(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self.conn.execute(
                "SELECT username FROM users WHERE priority > 0 ORDER BY priority DESC LIMIT 20")]

    def mark(self, pk: str, status: str, note: str | None = None) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE users SET status = ?, note = ?, crawled_at = ? WHERE pk = ?",
                (status, note, time.time(), pk),
            )

    def mark_lists(self, pk: str, following: bool, followers: bool) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE users SET fg_done = MAX(fg_done, ?), fr_done = MAX(fr_done, ?) WHERE pk = ?",
                (int(following), int(followers), pk),
            )

    def set_depth_min(self, pk: str, depth: int) -> None:
        with self.lock:
            self.conn.execute("UPDATE users SET depth = MIN(depth, ?) WHERE pk = ?", (depth, pk))

    def reset_crawling(self) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE users SET status = CASE WHEN fg_done = 1 OR fr_done = 1 THEN 'partial' ELSE 'seen' END "
                "WHERE status = 'crawling'")

    def retry_errors(self) -> None:
        with self.lock:
            self.conn.execute("UPDATE users SET status = 'seen' WHERE status = 'error'")

    def clear(self) -> None:
        with self.lock:
            self.conn.executescript("DELETE FROM edges; DELETE FROM users; DELETE FROM meta;")
            self.conn.execute("VACUUM")

    def set_meta(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))

    # ----------------------------------------------------------------- reads
    def get_meta(self, key: str) -> str | None:
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None

    def get_user(self, pk: str) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute("SELECT * FROM users WHERE pk = ?", (pk,)).fetchone()

    def find_username(self, username: str) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM users WHERE lower(username) = lower(?) LIMIT 1", (username,)).fetchone()

    def counts(self) -> dict:
        with self.lock:
            users = self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            edges = self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            by_status = dict(self.conn.execute("SELECT status, COUNT(*) FROM users GROUP BY status").fetchall())
            by_depth = dict(self.conn.execute("SELECT depth, COUNT(*) FROM users GROUP BY depth").fetchall())
        return {"users": users, "edges": edges,
                "done": by_status.get("done", 0) + by_status.get("partial", 0),
                "partial": by_status.get("partial", 0),
                "skipped": by_status.get("skipped", 0), "errors": by_status.get("error", 0),
                "by_depth": {str(k): v for k, v in by_depth.items()}}

    def graph(self, min_degree: int, max_nodes: int, focus: str | None = None, extra: list[str] | None = None) -> dict:
        """The pruned set of nodes worth drawing and the edges between them.

        Always: you, your direct connections (depth <= 1), crawled accounts, any
        `extra` ids (e.g. a highlighted path) and all neighbours of `focus` (the
        selected person). Everyone else only if connected to >= min_degree
        crawled accounts.
        """
        params = {"mind": min_degree, "lim": max_nodes, "focus": focus or "",
                  "extra": json.dumps(extra or [])}
        with self.lock:
            nodes = self.conn.execute(
                """WITH fn(pk) AS (
                       SELECT dst FROM edges WHERE src = :focus
                       UNION SELECT src FROM edges WHERE dst = :focus),
                   ex(pk) AS (SELECT value FROM json_each(:extra))
                   SELECT pk, username, full_name, is_private, is_verified, depth, status, deg,
                          follower_count, following_count
                   FROM users
                   WHERE depth <= 1 OR status IN ('done', 'partial', 'crawling') OR deg >= :mind
                      OR pk IN (SELECT pk FROM ex) OR pk IN (SELECT pk FROM fn) OR pk = :focus
                   ORDER BY depth = 0 DESC,
                            pk IN (SELECT pk FROM ex) OR pk = :focus DESC,
                            status IN ('done', 'partial') DESC,
                            depth <= 1 DESC,
                            pk IN (SELECT pk FROM fn) DESC,
                            deg DESC
                   LIMIT :lim""",
                params,
            ).fetchall()
            self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS sel(pk TEXT PRIMARY KEY) WITHOUT ROWID")
            self.conn.execute("DELETE FROM sel")
            self.conn.executemany("INSERT INTO sel(pk) VALUES(?)", [(n["pk"],) for n in nodes])
            edges = self.conn.execute(
                """SELECT e.src, e.dst FROM sel a
                   JOIN edges e ON e.src = a.pk
                   WHERE e.dst IN (SELECT pk FROM sel)"""
            ).fetchall()
            version = self.conn.execute(
                "SELECT (SELECT COUNT(*) FROM edges) || ':' || "
                "(SELECT COUNT(*) FROM users WHERE status IN ('done','partial')) || ':' || "
                "(SELECT COUNT(*) FROM users WHERE status = 'crawling')"
            ).fetchone()[0]
            total = self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

        edge_set = {(s, d) for s, d in edges}
        links, seen = [], set()
        for s, d in edge_set:
            if (d, s) in edge_set:
                key = (s, d) if s < d else (d, s)
                if key in seen:
                    continue
                seen.add(key)
                links.append({"source": s, "target": d, "m": 1})
            else:
                links.append({"source": s, "target": d, "m": 0})

        return {
            "version": f"{version}:{min_degree}:{max_nodes}:{focus}:{len(extra or [])}",
            "total": total,
            "nodes": [
                {
                    "id": n["pk"], "u": n["username"], "n": n["full_name"], "d": n["depth"],
                    "deg": n["deg"], "c": 1 if n["status"] in CRAWLED else 0, "s": n["status"],
                    "p": n["is_private"], "v": n["is_verified"],
                }
                for n in nodes
            ],
            "links": links,
        }

    def shortest_path(self, src: str, dst: str) -> list[dict] | None:
        """Dijkstra over the follow graph, treating follows as connections either way.

        Mutual follows cost 1, one-way follows cost 1.5, so among paths with the
        same number of hops the one through real (mutual) friendships wins.
        Returns [{pk, rel}] from src to dst; rel describes the hop *into* that node.
        """
        with self.lock:
            rows = self.conn.execute("SELECT src, dst FROM edges").fetchall()
        out: dict[str, set[str]] = {}
        for s, d in rows:
            out.setdefault(s, set()).add(d)
        adj: dict[str, dict[str, str]] = {}
        for s, ds in out.items():
            for d in ds:
                mutual = s in out.get(d, ())
                adj.setdefault(s, {})[d] = "mutual" if mutual else "follows"      # s follows d
                adj.setdefault(d, {})[s] = "mutual" if mutual else "followed_by"  # d is followed by s
        if src not in adj or dst not in adj:
            return None
        dist = {src: 0.0}
        prev: dict[str, str] = {}
        pq = [(0.0, src)]
        while pq:
            dcur, u = heapq.heappop(pq)
            if u == dst:
                break
            if dcur > dist.get(u, float("inf")):
                continue
            for v, rel in adj[u].items():
                nd = dcur + (1.0 if rel == "mutual" else 1.5)
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        if dst not in dist:
            return None
        path = [dst]
        while path[-1] != src:
            path.append(prev[path[-1]])
        path.reverse()
        return [{"pk": pk, "rel": adj[path[i - 1]][pk] if i else None} for i, pk in enumerate(path)]

    def users_by_pk(self, pks: list[str]) -> dict[str, sqlite3.Row]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM users WHERE pk IN (SELECT value FROM json_each(?))", (json.dumps(pks),)).fetchall()
        return {r["pk"]: r for r in rows}

    def export(self) -> dict:
        with self.lock:
            users = [dict(r) for r in self.conn.execute(
                "SELECT pk, username, full_name, is_private, is_verified, depth, status, deg FROM users")]
            edges = [{"follower": s, "followee": d} for s, d in self.conn.execute("SELECT src, dst FROM edges")]
        return {"users": users, "follows": edges}
