"""Crawl of the follow graph with N concurrent workers.

Two ways accounts get crawled:
  * auto  — breadth-first from the seed up to `depth` (depth 1 = just you)
  * expand — the user clicks someone in the graph; they jump the queue

Workers pull the next account from SQLite, download its following and
followers lists page by page, and write users + edges as they arrive so the UI
can draw the graph growing live.
"""

from __future__ import annotations

import asyncio
import time
import traceback
from collections import deque
from dataclasses import asdict, dataclass, replace

from .auth import SessionManager
from .db import UNKNOWN_DEPTH, GraphDB
from .ig_client import AuthError, IGClient, ListBlocked, NotAllowed


@dataclass
class CrawlOptions:
    depth: int = 1                 # 1 = only the seed's own lists; 2 = + each of their lists; ...
    max_accounts: int = 200        # accounts to crawl in this run
    max_per_list: int = 1000       # cap per followers / following list
    concurrency: int = 4
    delay_ms: int = 300            # min gap between request starts (global)
    include_followers: bool = True
    include_following: bool = True
    skip_verified: bool = True     # don't auto-expand celebrities / brands


class Crawler:
    def __init__(self, db: GraphDB, sessions: SessionManager):
        self.db = db
        self.sessions = sessions
        self.opts = CrawlOptions()
        self.task: asyncio.Task | None = None
        self.running = False
        self.mode = "idle"             # auto | expand
        self.seed: str | None = None
        self.crawled_run = 0
        self.inflight: dict[str, str] = {}
        self.error: str | None = None
        self.phase = "idle"
        self.started_at: float | None = None
        self.log: deque[dict] = deque(maxlen=150)
        self._client: IGClient | None = None
        self._me = ""
        self._single_lock = asyncio.Lock()

    def _log(self, msg: str) -> None:
        self.log.append({"t": time.time(), "msg": msg})

    async def client(self) -> IGClient:
        c = await self.sessions.ensure()
        if c is not self._client:
            c.configure(self.opts.concurrency, self.opts.delay_ms)
            self._client = c
        self._me = c.user_id or ""
        return c

    # ------------------------------------------------------------- control
    async def start(self, seed: str | None, opts: CrawlOptions) -> None:
        if self.running:
            raise RuntimeError("A crawl is already running")
        self.opts = opts
        self.mode = "auto" if seed else "expand"
        self.seed = seed or self.seed
        self.error = None
        self.crawled_run = 0
        self.inflight.clear()
        self.running = True
        self.started_at = time.time()
        self.phase = "starting"
        self.task = asyncio.create_task(self._run(seed))

    async def expand(self, pk: str) -> bool:
        """Queue one person for crawling, starting an expand-only run if idle."""
        queued = self.db.prioritize(pk)
        if queued and not self.running:
            await self.start(None, replace(self.opts, depth=0, max_accounts=10**9))
        return queued

    async def stop(self) -> None:
        task = self.task
        if task and not task.done():
            self.phase = "stopping"
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def status(self) -> dict:
        c = self._client
        me = self._me or (self.sessions.user_pk or "")
        o = self.opts
        followers_ok = c.followers_ok() if c else True
        try:
            queued = self.db.queue_size(o.depth if self.running else 0, me, o.skip_verified, followers_ok)
        except Exception:
            queued = None
        message = c.message if (c and self.running) else ""
        if c and c.followers_blocked_for() > 0 and not message:
            message = (f"Instagram is temporarily blocking followers lists — continuing with following "
                       f"lists only, retrying followers in {c.followers_blocked_for() / 60:.0f} min")
        return {
            "running": self.running,
            "mode": self.mode,
            "phase": self.phase,
            "seed": self.seed or self.db.get_meta("seed_username"),
            "seed_pk": self.db.get_meta("seed"),
            "opts": asdict(o),
            "crawled_run": self.crawled_run,
            "inflight": list(self.inflight.values()),
            "inflight_pks": list(self.inflight.keys()),
            "expanding": self.db.priority_usernames(),
            "queued": queued,
            "requests": c.requests if c else 0,
            "rps": round(c.rate(), 2) if c else 0,
            "paused_for": round(c.paused_for()) if c else 0,
            "message": message,
            "error": self.error,
            "log": list(self.log)[-40:],
            **self.db.counts(),
        }

    # ---------------------------------------------------------------- run
    async def _run(self, seed_username: str | None) -> None:
        workers: list[asyncio.Task] = []
        try:
            self.phase = "logging in"
            client = await self.client()
            client.configure(self.opts.concurrency, self.opts.delay_ms)
            self.db.reset_crawling()

            if seed_username:
                self.phase = "looking up profile"
                seed = await client.profile(seed_username)
                self.db.upsert_users([seed], depth=0)
                self.db.set_depth_min(seed["pk"], 0)
                self.db.set_meta("seed", seed["pk"])
                self.db.set_meta("seed_username", seed["username"])
                self._log(f"Seed @{seed['username']} (id {seed['pk']})")
                if seed["pk"] == self._me and not self.sessions.username:
                    self.sessions.username = seed["username"]

            self.phase = "crawling"
            while True:
                workers = [asyncio.create_task(self._worker(client)) for _ in range(self.opts.concurrency)]
                await asyncio.gather(*workers)
                workers = []
                # something may have been queued for expansion just as the workers ran dry
                if not self.db.has_priority() or self.crawled_run >= self.opts.max_accounts:
                    break
            if self.mode == "auto":
                if self.crawled_run == 0:
                    self._log("Nothing left to crawl at this depth — click people to expand them, or raise Depth")
                self._log(f"Done — crawled {self.crawled_run} accounts")
            self.phase = "finished"
        except asyncio.CancelledError:
            self._log(f"Stopped after {self.crawled_run} accounts")
            self.phase = "stopped"
        except AuthError as e:
            self.error = str(e)
            self.phase = "error"
            self._log(f"Auth problem: {e}")
            self.sessions.invalidate(str(e))
        except NotAllowed as e:
            self.error = str(e)
            self.phase = "error"
            self._log(str(e))
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            self.phase = "error"
            self._log(traceback.format_exc(limit=3))
        finally:
            for w in workers:
                w.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            self.db.reset_crawling()
            self.inflight.clear()
            self.running = False

    async def _worker(self, client: IGClient) -> None:
        o = self.opts
        while True:
            if self.crawled_run + len(self.inflight) >= o.max_accounts and not self.db.has_priority():
                return
            row = self.db.claim_next(o.depth, self._me, o.skip_verified, client.followers_ok())
            if row is None:
                if not self.inflight:
                    return  # queue empty and nobody can add more
                await asyncio.sleep(0.3)
                continue

            pk, username = row["pk"], row["username"]
            self.inflight[pk] = username
            try:
                status, n_fg, n_fr = await self._crawl_one(client, row)
                self.db.mark(pk, status)
                self.crawled_run += 1
                extra = " (followers pending)" if status == "partial" else ""
                self._log(f"@{username} · {n_fg} following · {n_fr} followers{extra}")
            except NotAllowed as e:
                self.db.mark(pk, "skipped", str(e))
                self._log(f"skip @{username}: {e}")
            except (AuthError, asyncio.CancelledError):
                self.db.mark(pk, "seen")
                raise
            except Exception as e:
                self.db.mark(pk, "error", str(e))
                self._log(f"error @{username}: {e}")
            finally:
                self.inflight.pop(pk, None)

    async def _crawl_one(self, client: IGClient, row) -> tuple[str, int, int]:
        """Fetch whichever of the user's lists are still missing. Returns (status, #following, #followers)."""
        o = self.opts
        pk, depth = row["pk"], row["depth"]
        child_depth = min(depth + 1, UNKNOWN_DEPTH)
        want_fg = o.include_following and not row["fg_done"]
        want_fr = o.include_followers and not row["fr_done"]

        async def fetch(kind: str) -> int:
            n = 0
            async for batch in client.iter_connections(pk, kind, o.max_per_list):
                self.db.upsert_users(batch, child_depth)
                if kind == "following":
                    self.db.add_edges([(pk, u["pk"]) for u in batch])
                else:
                    self.db.add_edges([(u["pk"], pk) for u in batch])
                n += len(batch)
            self.db.mark_lists(pk, kind == "following", kind == "followers")
            return n

        n_fg = n_fr = 0
        fr_missing = False
        jobs: dict[str, asyncio.Task] = {}
        if want_fg:
            jobs["following"] = asyncio.ensure_future(fetch("following"))
        if want_fr:
            if client.followers_ok():
                jobs["followers"] = asyncio.ensure_future(fetch("followers"))
            else:
                fr_missing = True
        if jobs:
            results = await asyncio.gather(*jobs.values(), return_exceptions=True)
            res = dict(zip(jobs.keys(), results))
            for kind, r in res.items():
                if isinstance(r, ListBlocked):
                    fr_missing = True
                elif isinstance(r, BaseException):
                    raise r
            n_fg = res.get("following", 0) if not isinstance(res.get("following"), BaseException) else 0
            n_fr = res.get("followers", 0) if not isinstance(res.get("followers"), BaseException) else 0
        # 'partial' accounts are picked up again once followers lists are allowed again
        return ("partial" if fr_missing else "done"), n_fg, n_fr

    async def crawl_single(self, pk: str) -> None:
        """Crawl one account right now (used by path search for people outside the graph)."""
        async with self._single_lock:
            client = await self.client()
            row = self.db.get_user(pk)
            if not row or row["status"] in ("done", "crawling"):
                return
            self.db.mark(pk, "crawling")
            self.inflight[pk] = row["username"]
            try:
                status, n_fg, n_fr = await self._crawl_one(client, row)
                self.db.mark(pk, status)
                self._log(f"@{row['username']} · {n_fg} following · {n_fr} followers (path search)")
            except NotAllowed as e:
                self.db.mark(pk, "skipped", str(e))
                raise
            except BaseException:
                self.db.mark(pk, row["status"])
                raise
            finally:
                self.inflight.pop(pk, None)
