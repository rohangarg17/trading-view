"""Persistent Playwright browser to fetch NSE option chains.

NSE's /api/option-chain-* endpoints return empty `{}` to any non-browser
client (including curl_cffi and raw headless Playwright). Only a stealth-
patched headless Chromium that has first loaded the option-chain page is
accepted. We keep a single browser + context alive and make fetch() calls
from page JS contexts, so NSE sees them as same-origin XHRs.

Concurrency model:
    Playwright's sync API can only be used from one thread. So we run the
    async API on a single dedicated event-loop thread, and any caller (e.g.
    the background refresh loop) submits work onto that loop.

    Two public APIs:
      - NSEBrowser.fetch_chain(sym, ...)        blocks until one symbol done.
      - NSEBrowser.fetch_chains_concurrent(tasks, concurrency=N)
            runs many symbol fetches concurrently, N in flight at a time,
            reusing a pool of N pages inside a single shared BrowserContext.

    The shared context is important — bot-detection cookies live on the
    context, so opening N pages inside it avoids re-solving the challenge
    per page.

    NSEPlaywright stays as a thin sync wrapper over NSEBrowser.fetch_chain
    for call sites that just want one symbol (stock_detail.py etc.).
"""

import asyncio
import threading
import time

from playwright.async_api import async_playwright
from playwright_stealth import Stealth


OPTION_CHAIN_URL = "https://www.nseindia.com/option-chain"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# A known-valid historical expiry that NSE's validator accepts so the probe
# response includes the real expiryDates list. If NSE ever rejects this,
# update to any trading Thursday/Friday in a recent month.
PROBE_EXPIRY = "24-Apr-2026"


# Two-stage JS payload:
#   1. Probe call learns the real expiryDates list.
#   2. All wanted-expiry fetches run concurrently via Promise.all, sharing
#      the same HTTP/2 connection (wall time ≈ single-fetch latency).
_FETCH_CHAIN_JS = """
async ({symbol, chainType, probeExpiry, maxExpiries}) => {
    const qs = (exp) => `/api/option-chain-v3?type=${chainType}&symbol=${encodeURIComponent(symbol)}&expiry=${encodeURIComponent(exp)}`;
    const r1 = await fetch(qs(probeExpiry));
    const j1 = await r1.json();
    const expiries = (j1 && j1.records && j1.records.expiryDates) || [];
    if (!expiries.length) return { error: 'no_expiries', snippet: JSON.stringify(j1).slice(0, 120) };
    const wanted = expiries.slice(0, maxExpiries);
    const results = await Promise.all(wanted.map(async (exp) => {
        const r = await fetch(qs(exp));
        const j = await r.json();
        return (j && j.records) || {};
    }));
    const allRows = [];
    let underlying = null;
    let timestamp = null;
    for (const recs of results) {
        if (recs.underlyingValue && !underlying) underlying = recs.underlyingValue;
        if (recs.timestamp && !timestamp) timestamp = recs.timestamp;
        for (const row of (recs.data || [])) allRows.push(row);
    }
    return {
        records: {
            data: allRows,
            expiryDates: wanted,
            underlyingValue: underlying,
            timestamp: timestamp,
        },
    };
}
"""


async def _warm_page(page):
    """Navigate a page to the option-chain URL and let cookies settle."""
    await page.goto(OPTION_CHAIN_URL, timeout=45000, wait_until="domcontentloaded")
    await asyncio.sleep(4)


async def _fetch_one(page, symbol, chain_type, max_expiries):
    """Run _FETCH_CHAIN_JS on a page, with one renav-and-retry on failure."""
    for attempt in range(2):
        try:
            result = await page.evaluate(
                _FETCH_CHAIN_JS,
                {
                    "symbol": symbol,
                    "chainType": chain_type,
                    "probeExpiry": PROBE_EXPIRY,
                    "maxExpiries": max_expiries,
                },
            )
            if result.get("error"):
                if attempt == 0:
                    try:
                        await _warm_page(page)
                    except Exception:
                        return None
                    continue
                return None
            return result["records"]
        except Exception:
            if attempt == 0:
                try:
                    await _warm_page(page)
                except Exception:
                    return None
                continue
            return None
    return None


# JS that fetches all strikes for a single expiry. Unlike _FETCH_CHAIN_JS it
# does NOT filter to ATM — we need the full chain for strike scanning.
_FETCH_EXPIRY_JS = """
async ({symbol, chainType, expiry}) => {
    const url = `/api/option-chain-v3?type=${chainType}&symbol=${encodeURIComponent(symbol)}&expiry=${encodeURIComponent(expiry)}`;
    const r = await fetch(url);
    const j = await r.json();
    const recs = (j && j.records) || {};
    if (!recs.data || !recs.data.length) return { error: 'empty', snippet: JSON.stringify(j).slice(0, 120) };
    return {
        records: {
            data: recs.data,
            expiryDates: recs.expiryDates || [expiry],
            underlyingValue: recs.underlyingValue,
            timestamp: recs.timestamp,
            strikePrices: recs.strikePrices,
        },
    };
}
"""


async def _fetch_one_expiry(page, symbol, chain_type, expiry):
    """Fetch all strikes for one specific expiry. Used by the collar analyzer."""
    for attempt in range(2):
        try:
            result = await page.evaluate(
                _FETCH_EXPIRY_JS,
                {"symbol": symbol, "chainType": chain_type, "expiry": expiry},
            )
            if result.get("error"):
                if attempt == 0:
                    try:
                        await _warm_page(page)
                    except Exception:
                        return None
                    continue
                return None
            return result["records"]
        except Exception:
            if attempt == 0:
                try:
                    await _warm_page(page)
                except Exception:
                    return None
                continue
            return None
    return None


class NSEBrowser:
    """Singleton owning a Chromium process, a shared BrowserContext, and its
    own asyncio loop on a dedicated thread.

    Callers submit coroutines via run_coro() (sync, blocks). For batch work,
    use fetch_chains_concurrent() which dispatches N in-flight fetches
    across N pages.
    """

    def __init__(self, headless=True):
        self._headless = headless
        self._loop = None
        self._loop_thread = None
        self._ready = threading.Event()
        self._stealth_cm = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._start_lock = threading.Lock()

    # --- Lifecycle -------------------------------------------------------

    def start(self):
        with self._start_lock:
            if self._loop_thread is not None:
                return
            self._loop_thread = threading.Thread(
                target=self._run_loop, name="nse-pw-loop", daemon=True,
            )
            self._loop_thread.start()
            self._ready.wait()
            # Now schedule the async browser launch and block until done.
            fut = asyncio.run_coroutine_threadsafe(self._async_start(), self._loop)
            fut.result()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    async def _async_start(self):
        self._stealth_cm = Stealth().use_async(async_playwright())
        self._playwright = await self._stealth_cm.__aenter__()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = await self._browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
        )

    def close(self):
        if self._loop is None:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._async_close(), self._loop)
            fut.result(timeout=10)
        except Exception:
            pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass

    async def _async_close(self):
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._stealth_cm is not None:
                await self._stealth_cm.__aexit__(None, None, None)
        except Exception:
            pass

    # --- Public API ------------------------------------------------------

    def fetch_chain(self, symbol, chain_type="Equity", max_expiries=4):
        """Blocking single-symbol fetch. Opens & closes a throwaway page."""
        fut = asyncio.run_coroutine_threadsafe(
            self._async_fetch_single(symbol, chain_type, max_expiries), self._loop,
        )
        return fut.result()

    async def _async_fetch_single(self, symbol, chain_type, max_expiries):
        page = await self._context.new_page()
        try:
            await _warm_page(page)
            return await _fetch_one(page, symbol, chain_type, max_expiries)
        finally:
            try:
                await page.close()
            except Exception:
                pass

    def fetch_full_chain(self, symbol, expiry, chain_type="Equity"):
        """Blocking fetch of every strike for one specific expiry.

        Returns the raw NSE `records` dict, or None. The Collar Analyzer uses
        this to scan the full strike ladder rather than just ATM.
        """
        fut = asyncio.run_coroutine_threadsafe(
            self._async_fetch_full_chain(symbol, chain_type, expiry), self._loop,
        )
        return fut.result()

    async def _async_fetch_full_chain(self, symbol, chain_type, expiry):
        page = await self._context.new_page()
        try:
            await _warm_page(page)
            return await _fetch_one_expiry(page, symbol, chain_type, expiry)
        finally:
            try:
                await page.close()
            except Exception:
                pass

    def fetch_chains_concurrent(self, tasks, concurrency=4):
        """Fetch many symbols in parallel using a pool of N pages.

        `tasks` is a list of (symbol, chain_type, max_expiries, key) tuples;
        `key` is whatever you want returned alongside the result (e.g. your
        row-building context). Returns a list of (key, records_or_None).
        """
        fut = asyncio.run_coroutine_threadsafe(
            self._async_fetch_batch(tasks, concurrency), self._loop,
        )
        return fut.result()

    async def _async_fetch_batch(self, tasks, concurrency):
        # One warmed page per worker, all in the same BrowserContext.
        pages = []
        try:
            for _ in range(concurrency):
                p = await self._context.new_page()
                pages.append(p)
            # Warm them in parallel too.
            await asyncio.gather(*[_warm_page(p) for p in pages], return_exceptions=True)

            page_queue = asyncio.Queue()
            for p in pages:
                page_queue.put_nowait(p)

            async def worker(task):
                symbol, chain_type, max_expiries, key = task
                page = await page_queue.get()
                try:
                    records = await _fetch_one(page, symbol, chain_type, max_expiries)
                finally:
                    page_queue.put_nowait(page)
                return key, records

            return await asyncio.gather(*[worker(t) for t in tasks])
        finally:
            for p in pages:
                try:
                    await p.close()
                except Exception:
                    pass


# --- Backwards-compatible sync shim ---------------------------------------

class NSEPlaywright:
    """Sync facade for call sites that just want one symbol at a time."""

    def __init__(self, headless=True):
        self._browser = NSEBrowser(headless=headless)

    def start(self):
        self._browser.start()

    def close(self):
        self._browser.close()

    def fetch_chain(self, symbol, chain_type="Equity", max_expiries=4):
        return self._browser.fetch_chain(symbol, chain_type, max_expiries)

    def fetch_equity_chain(self, symbol):
        return self.fetch_chain(symbol, chain_type="Equity", max_expiries=4)
