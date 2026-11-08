"""Getting (and keeping) a logged-in Instagram session.

Order of preference:
  1. data/session.json saved from a previous login
  2. IG_SESSIONID from .env
  3. A real (visible) browser window via Playwright, auto-filled with
     IG_USERNAME / IG_PASSWORD, where you can complete 2FA if asked.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Callable

from . import config
from .ig_client import AuthError, IGClient, user_id_from_cookies


def load_saved() -> dict | None:
    try:
        sess = json.loads(config.SESSION_PATH.read_text())
        return sess if sess.get("cookies", {}).get("sessionid") else None
    except (OSError, ValueError):
        return None


def save_session(sess: dict) -> None:
    config.SESSION_PATH.write_text(json.dumps(sess, indent=2))
    try:
        config.SESSION_PATH.chmod(0o600)
    except OSError:
        pass


def session_from_env() -> dict | None:
    if not config.IG_SESSIONID:
        return None
    cookies = {"sessionid": config.IG_SESSIONID}
    uid = user_id_from_cookies(cookies)
    if uid:
        cookies["ds_user_id"] = uid
    return {"cookies": cookies, "user_agent": None}


async def _dismiss_cookie_banner(page) -> None:
    for label in ("Decline optional cookies", "Only allow essential cookies", "Refuse optional cookies"):
        try:
            btn = page.get_by_role("button", name=label)
            if await btn.count():
                await btn.first.click(timeout=2000)
                return
        except Exception:
            pass


async def browser_login(username: str, password: str, status: Callable[[str], None]) -> dict:
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("Playwright is not installed (uv sync)") from e

    async with async_playwright() as p:
        kwargs: dict = {"headless": False, "args": ["--disable-blink-features=AutomationControlled"]}
        if config.BROWSER_PATH:
            kwargs["executable_path"] = config.BROWSER_PATH
        try:
            browser = await p.chromium.launch(**kwargs)
        except Exception as e:
            raise RuntimeError(
                f"Couldn't start a browser for login ({e}). Set BROWSER_PATH in .env, "
                "run `uv run playwright install chromium`, or put IG_SESSIONID in .env instead."
            ) from e
        try:
            ctx = await browser.new_context(locale="en-US", viewport={"width": 460, "height": 820})
            page = await ctx.new_page()
            status("Opening Instagram login…")
            await page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded")
            await _dismiss_cookie_banner(page)

            if username and password:
                try:
                    user_in = page.locator('input[name="username"], input[name="email"]').first
                    await user_in.wait_for(timeout=20000)
                    await user_in.fill(username)
                    pw = page.locator('input[name="password"], input[name="pass"]').first
                    await pw.fill(password)
                    await pw.press("Enter")
                    status("Credentials submitted — finish any 2FA / security check in the browser window")
                except Exception:
                    status("Couldn't auto-fill the form — please log in manually in the browser window")
            else:
                status("Log in to Instagram in the browser window that just opened")

            deadline = time.monotonic() + 300
            cookies: dict = {}
            while time.monotonic() < deadline:
                if not browser.is_connected() or page.is_closed():
                    raise RuntimeError("Login window was closed before login finished")
                cookies = {c["name"]: c["value"] for c in await ctx.cookies("https://www.instagram.com")}
                if cookies.get("sessionid") and cookies.get("ds_user_id"):
                    break
                await asyncio.sleep(1)
            else:
                raise RuntimeError("Timed out waiting for login (5 minutes)")

            status("Logged in — saving session")
            await asyncio.sleep(2)  # let csrftoken & friends settle
            cookies = {c["name"]: c["value"] for c in await ctx.cookies("https://www.instagram.com")}
            ua = await page.evaluate("navigator.userAgent")
            return {"cookies": cookies, "user_agent": ua}
        finally:
            try:
                await browser.close()
            except Exception:
                pass


class SessionManager:
    def __init__(self) -> None:
        self.client: IGClient | None = None
        self.username: str | None = None
        self.user_pk: str | None = None
        self.logging_in = False
        self.status = ""
        self.error: str | None = None
        self._lock = asyncio.Lock()

    def info(self) -> dict:
        return {
            "logged_in": self.client is not None,
            "username": self.username,
            "user_pk": self.user_pk,
            "logging_in": self.logging_in,
            "status": self.status,
            "error": self.error,
            "has_credentials": bool(config.IG_USERNAME and config.IG_PASSWORD),
            "has_sessionid": bool(config.IG_SESSIONID),
        }

    def _set_status(self, s: str) -> None:
        self.status = s

    async def _activate(self, sess: dict) -> None:
        client = IGClient(sess["cookies"], sess.get("user_agent"))
        try:
            await asyncio.wait_for(client.verify(), timeout=30)
        except AuthError:
            await client.close()
            raise
        except Exception:
            # Couldn't confirm (rate limit, network…) — accept and let the crawl find out.
            pass
        client.message = ""
        if self.client:
            await self.client.close()
        self.client = client
        self.username = sess.get("username")
        self.user_pk = client.user_id
        self.error = None

    async def init(self) -> None:
        async with self._lock:
            if self.client:
                return
            for source, sess in (("saved session", load_saved()), ("IG_SESSIONID from .env", session_from_env())):
                if not sess:
                    continue
                try:
                    self.status = f"Checking {source}…"
                    await self._activate(sess)
                    self.status = f"Using {source}"
                    if source != "saved session":
                        save_session(sess)
                    return
                except AuthError as e:
                    self.error = f"{source} was rejected: {e}"
                    if source == "saved session":
                        config.SESSION_PATH.unlink(missing_ok=True)
            self.status = ""

    async def login(self) -> None:
        if self.logging_in:
            while self.logging_in:
                await asyncio.sleep(0.5)
            return
        self.logging_in = True
        self.error = None
        try:
            sess = await browser_login(config.IG_USERNAME, config.IG_PASSWORD, self._set_status)
            await self._activate(sess)
            save_session(sess)
            self.status = "Logged in"
        except Exception as e:
            self.error = str(e)
            self.status = ""
        finally:
            self.logging_in = False

    def start_login(self) -> None:
        if not self.logging_in:
            asyncio.create_task(self.login())

    async def ensure(self) -> IGClient:
        if self.client:
            return self.client
        await self.init()
        if not self.client:
            await self.login()
        if not self.client:
            raise AuthError(self.error or "Not logged in to Instagram")
        return self.client

    def invalidate(self, reason: str) -> None:
        client, self.client = self.client, None
        self.username = None
        self.error = reason
        config.SESSION_PATH.unlink(missing_ok=True)
        if client:
            asyncio.create_task(client.close())

    async def logout(self) -> None:
        self.invalidate("Logged out")
        self.error = None

    async def close(self) -> None:
        if self.client:
            await self.client.close()
