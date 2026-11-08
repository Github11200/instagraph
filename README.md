# Instagraph

Map your Instagram network. Paste a link to your profile. Instagraph fetches your followers and following, then theirs, and so on. It draws everyone as a live, interactive 2D graph, like the Notion or Obsidian graph view.

Everything runs on your machine. The crawled graph is saved in `data/graph.db` (SQLite), and your login session is saved in `data/session.json`.

## Quick start

```bash
uv sync
cp .env.example .env      # optional: add IG_USERNAME / IG_PASSWORD or IG_SESSIONID
uv run instagraph         # opens http://127.0.0.1:8765
```

1. Click **Log in**, or just start a crawl and Instagraph logs in when it needs to.
   - With `IG_USERNAME`/`IG_PASSWORD` in `.env`, a browser window opens and fills in your details. If Instagram asks for 2FA or a "was this you?" check, finish it in that window.
   - With `IG_SESSIONID` in `.env`, no browser is needed. To get the value: in a browser where you're logged in, open DevTools → Application → Cookies → `instagram.com` and copy the `sessionid` value.
   - With neither set, the window opens and you log in by hand.
   - After you log in once, the session is reused until Instagram expires it.
2. Paste your profile link (`https://www.instagram.com/yourname/`) and click **Start crawl**.
3. The graph fills in as accounts are crawled. You can stop at any time. Starting again picks up where you left off.

The login window uses your installed Chromium, Chrome or Brave. If none is found, set `BROWSER_PATH` in `.env` or run `uv run playwright install chromium`.

## How it's fast

Instagraph doesn't click the Followers button and scroll the popup. It calls the same JSON endpoints that the popup uses. Each request returns about 100 accounts, and requests run in parallel over HTTP/2 using your logged-in session. Crawl order is breadth-first: accounts closest to you go first. Among those, accounts already linked to the most people in the graph go first, so the interesting overlaps show up early.

### Crawl settings

| Setting | Default | What it does |
|---|---|---|
| Depth | 2 | 1 = only you. 2 = you plus everyone you're connected to. 3 = one more hop. |
| Accounts to crawl | 200 | How many accounts to expand in this run |
| Max per list | 1000 | Cap on each followers or following list. Keeps big accounts from adding thousands of strangers. |
| Parallel requests | 4 | Number of requests in flight at once |
| Delay | 300 ms | Minimum gap between request starts, with random jitter |
| Skip verified | on | Don't expand celebrity or brand accounts |

Private accounts you don't follow are skipped, because Instagram doesn't let you see their lists.

### ⚠️ Rate limits

Instagram limits how many follower-list requests one account can make. If you push the settings too hard, you'll see **"Rate limited… pausing N min"**. Instagraph then waits, doubling the wait each time up to 30 minutes, and resumes on its own. Hammering past that can get your account temporarily restricted or sent to a security checkpoint.

Automated scraping is against Instagram's Terms of Use, so use this on your own account at your own risk. For a few hundred accounts, the defaults are a reasonable balance. If you hit limits often, lower **Parallel requests** or raise the delay.

## Exploring the graph

- **Node size**: how many people in the crawl that account is connected to.
- **Colors**: communities found automatically (label propagation). Switch to "Color: depth" to see hops from you instead.
- **Min. links**: hides people connected to fewer than N others. Start at 2 so you only see people who connect at least two accounts. Lower it to see everyone.
- **Mutuals only**: shows only follow relationships that go both ways.
- **Hover** a node to highlight its connections. **Click** it to see details: avatar, mutuals, who they follow in your graph, and a link to their profile.
- **Search** jumps to a username. The ⇩ button exports the full graph (every user plus every follow edge) as JSON.

## Project layout

```
instagraph/
  server.py      FastAPI app + endpoints
  crawler.py     concurrent BFS crawl
  ig_client.py   Instagram web-API client (pacing, retries, rate-limit backoff)
  auth.py        session loading / Playwright login
  db.py          SQLite graph storage
  static/        the web UI (force-graph, vendored, so it works offline)
```
