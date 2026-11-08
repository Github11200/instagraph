"""Thin async client for Instagram's private *web* API.

Instead of literally clicking the Followers / Following buttons and scrolling
the modal, we call the same JSON endpoints that modal calls. Same data, but
one HTTP request returns up to ~100-200 users, so it is orders of magnitude
faster than driving a browser.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from html import unescape
from collections import deque
from typing import AsyncIterator
from urllib.parse import unquote

import httpx

WEB_APP_ID = "936619743392459"
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)


class IGError(Exception):
    pass


class RateLimited(IGError):
    pass


class AuthError(IGError):
    pass


class NotAllowed(IGError):
    """Private account we don't follow, deleted user, etc. Skip and move on."""


class ListBlocked(IGError):
    """Instagram is temporarily refusing to serve a kind of list (redirects to the homepage)."""


class _Retry(Exception):
    pass


def slim_user(u: dict) -> dict:
    pk = u.get("pk") or u.get("id") or u.get("pk_id")
    return {
        "pk": str(pk) if pk is not None else None,
        "username": u.get("username"),
        "full_name": u.get("full_name") or "",
        "is_private": 1 if u.get("is_private") else 0,
        "is_verified": 1 if u.get("is_verified") else 0,
        "pic": u.get("profile_pic_url"),
    }


def user_id_from_cookies(cookies: dict) -> str | None:
    if cookies.get("ds_user_id"):
        return str(cookies["ds_user_id"])
    sid = unquote(cookies.get("sessionid", ""))
    return sid.split(":")[0] if ":" in sid else None


class IGClient:
    def __init__(self, cookies: dict, user_agent: str | None = None):
        self.cookies = dict(cookies)
        self.user_id = user_id_from_cookies(self.cookies)
        headers = {
            "User-Agent": user_agent or DEFAULT_UA,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "X-IG-App-ID": WEB_APP_ID,
            "X-ASBD-ID": "129477",
            "X-IG-WWW-Claim": "0",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.instagram.com/",
            "Origin": "https://www.instagram.com",
        }
        if self.cookies.get("csrftoken"):
            headers["X-CSRFToken"] = self.cookies["csrftoken"]
        self.http = httpx.AsyncClient(
            base_url="https://www.instagram.com",
            headers=headers,
            cookies=self.cookies,
            timeout=httpx.Timeout(25.0),
            http2=True,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=48, max_keepalive_connections=24),
        )
        # pacing / rate-limit state (shared by every crawler worker)
        self._sem = asyncio.Semaphore(4)
        self.delay = 0.3
        self._next_slot = 0.0
        self._slot_lock = asyncio.Lock()
        self.pause_until = 0.0
        self._backoff = 60.0
        self.requests = 0
        self._recent: deque[float] = deque(maxlen=500)
        self.message = ""
        self.followers_blocked_until = 0.0
        self._followers_backoff = 10 * 60.0

    def configure(self, concurrency: int, delay_ms: int) -> None:
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self.delay = max(0, delay_ms) / 1000.0

    async def close(self) -> None:
        await self.http.aclose()

    # ---------------------------------------------------------------- stats
    def rate(self, window: float = 10.0) -> float:
        now = time.monotonic()
        return sum(1 for t in self._recent if now - t <= window) / window

    def paused_for(self) -> float:
        return max(0.0, self.pause_until - time.monotonic())

    def followers_blocked_for(self) -> float:
        return max(0.0, self.followers_blocked_until - time.monotonic())

    def followers_ok(self) -> bool:
        return self.followers_blocked_for() == 0

    def _block_followers(self) -> None:
        now = time.monotonic()
        if now < self.followers_blocked_until:
            return
        self.followers_blocked_until = now + self._followers_backoff
        self._followers_backoff = min(self._followers_backoff * 2, 3 * 3600)

    # -------------------------------------------------------------- pacing
    async def _wait_turn(self) -> None:
        while True:
            wait = self.pause_until - time.monotonic()
            if wait > 0:
                await asyncio.sleep(min(wait, 1.0))
                continue
            if self.message.startswith("Rate limited"):
                self.message = "Resuming after rate-limit pause…"
            break
        if self.delay <= 0:
            return
        async with self._slot_lock:
            wait = self._next_slot - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            # jitter so we don't look like a metronome
            self._next_slot = time.monotonic() + self.delay * random.uniform(0.6, 1.4)

    def _hit_rate_limit(self, why: str) -> None:
        now = time.monotonic()
        if now < self.pause_until:
            return  # another worker already triggered this pause
        self.pause_until = now + self._backoff
        mins = self._backoff / 60
        self.message = (
            f"Rate limited by Instagram ({why or 'please wait'}) — pausing "
            f"{mins:.0f} min, then resuming automatically"
        )
        self._backoff = min(self._backoff * 2, 30 * 60)

    # ------------------------------------------------------------- requests
    async def _get(self, path: str, params: dict | None = None, html: bool = False):
        attempt = 0
        while True:
            await self._wait_turn()
            async with self._sem:
                try:
                    r = await self.http.get(path, params=params, headers=self._HTML_HEADERS if html else None)
                except httpx.TransportError as e:
                    attempt += 1
                    if attempt > 4:
                        raise IGError(f"Network error: {e}") from e
                    await asyncio.sleep(2 * attempt)
                    continue
            self.requests += 1
            self._recent.append(time.monotonic())
            try:
                data = self._check_html(r) if html else self._check(r)
            except RateLimited as e:
                self._hit_rate_limit(str(e))
                continue
            except _Retry:
                attempt += 1
                if attempt > 4:
                    raise IGError(f"Instagram kept failing (HTTP {r.status_code}) for {path}")
                await asyncio.sleep(3 * attempt)
                continue
            if self.paused_for() == 0:
                self._backoff = 60.0
                if self.message.startswith("Resuming"):
                    self.message = ""
            return data

    _HTML_HEADERS = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    }

    @staticmethod
    def _check_html(r: httpx.Response) -> str:
        if r.is_redirect:
            loc = r.headers.get("location", "")
            if "login" in loc or "challenge" in loc or "checkpoint" in loc:
                raise AuthError("Instagram session is not logged in (redirected to login) — log in again")
            raise _Retry()
        if r.status_code == 429:
            raise RateLimited("HTTP 429")
        if r.status_code >= 500:
            raise _Retry()
        if r.status_code == 404:
            raise NotAllowed("Profile not found")
        if r.status_code >= 400:
            raise NotAllowed(f"HTTP {r.status_code}")
        if "not-logged-in" in r.text[:2000]:
            raise AuthError("Instagram session expired — log in again")
        return r.text

    @staticmethod
    def _check(r: httpx.Response) -> dict:
        if r.is_redirect:
            loc = r.headers.get("location", "")
            if "login" in loc or "challenge" in loc or "checkpoint" in loc:
                raise AuthError("Instagram session is not logged in (redirected to login) — log in again")
            if loc.rstrip("/") in ("https://www.instagram.com", ""):
                # API call bounced to the homepage: Instagram's soft block on this kind of request
                raise ListBlocked("redirected to homepage")
            raise _Retry()
        if r.status_code == 429:
            raise RateLimited("HTTP 429")
        if r.status_code >= 500:
            raise _Retry()
        try:
            data = r.json()
        except ValueError:
            if r.status_code == 404:
                raise NotAllowed("Instagram endpoint or user not found (HTTP 404)")
            if r.status_code == 200:
                raise AuthError("Instagram returned a web page instead of data — session probably expired")
            if r.status_code in (401, 403):
                raise AuthError(f"HTTP {r.status_code} — session rejected")
            raise NotAllowed(f"HTTP {r.status_code}")
        if not isinstance(data, dict):
            raise NotAllowed("Unexpected response")

        msg = str(data.get("message") or "")
        low = msg.lower()
        if data.get("require_login") or "login_required" in low:
            raise AuthError("Instagram session expired — log in again")
        if data.get("checkpoint_url") or "checkpoint" in low or "challenge_required" in low:
            raise AuthError(
                "Instagram wants a security check. Open instagram.com in your normal browser, "
                "confirm it was you, then log in again here."
            )
        if data.get("spam") or "wait a few minutes" in low or "try again later" in low or "rate limit" in low:
            raise RateLimited(msg[:80])
        if r.status_code == 401:
            raise AuthError(msg or "HTTP 401 — session rejected")
        if r.status_code >= 400 or data.get("status") == "fail":
            raise NotAllowed(msg or f"HTTP {r.status_code}")
        return data

    # ------------------------------------------------------------ endpoints
    async def verify(self) -> None:
        """Cheap check that the session works: fetch one page of our own following list."""
        if not self.user_id:
            raise AuthError("Session cookie has no user id")
        await self._get(f"/api/v1/friendships/{self.user_id}/following/", {"count": 1})

    async def profile(self, username: str) -> dict:
        """Resolve a username to its user id.

        The old JSON endpoint (users/web_profile_info) now 404s, so read the id
        from the profile page itself ("profilePage_<id>"). Counts aren't
        reliably in the page any more, so they're left empty.
        """
        html = await self._get(f"/{username}/", html=True)
        m = re.search(r'"profilePage_(\d+)"', html)
        if not m:
            raise NotAllowed(f"@{username} not found (or Instagram changed its profile page)")
        name = ""
        t = re.search(r"<title>\s*(.*?)\s*\(&#064;", html, re.S)
        if t:
            name = unescape(t.group(1)).strip()
        return {
            "pk": m.group(1), "username": username, "full_name": name,
            "is_private": 0, "is_verified": 0, "pic": None,
            "follower_count": None, "following_count": None,
        }

    async def iter_connections(self, pk: str, kind: str, limit: int, page_size: int = 100) -> AsyncIterator[list[dict]]:
        """Yield pages of followers/following (kind = 'followers' | 'following')."""
        assert kind in ("followers", "following")
        path = f"/api/v1/friendships/{pk}/{kind}/"
        max_id: str | None = None
        got = 0
        while got < limit:
            params: dict = {"count": page_size}
            if kind == "followers":
                params["search_surface"] = "follow_list_page"
            if max_id:
                params["max_id"] = max_id
            try:
                data = await self._get(path, params)
            except ListBlocked:
                if kind == "followers":
                    self._block_followers()
                    raise
                # following lists bouncing too: treat as a normal rate limit and retry
                self._hit_rate_limit("requests redirected")
                continue
            if kind == "followers" and self.followers_blocked_until:
                self._followers_backoff = 10 * 60.0
                self.followers_blocked_until = 0.0
            users = [slim_user(u) for u in data.get("users") or []]
            users = [u for u in users if u["pk"]][: limit - got]
            if not users:
                break
            got += len(users)
            yield users
            nxt = data.get("next_max_id")
            if not nxt or nxt == max_id:
                break
            max_id = str(nxt)
