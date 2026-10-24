"""Playwright browser session used by the booking adapters.

Two constraints shape this module:

* Playwright's sync API refuses to run inside a running asyncio event
  loop, and FastAPI serves every request from one. So the browser is
  driven from a dedicated worker thread that has an event loop of its own
  (none, in fact), and the caller waits for the result.
* Playwright's sync API is not thread safe. The worker thread is a
  single-thread executor, so exactly one browser operation runs at a time
  and a page is never touched from two threads.

The session owns one Chromium browser and one browser context. Every
operation gets a fresh page, so state never leaks between a search and a
checkout.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from ..providers import ProviderError


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(float(str(raw).strip()))
    except ValueError:
        return default


# Playwright's default headless user agent is advertised as "HeadlessChrome"
# and several booking sites answer it with a stripped-down page. Sending the
# same user agent a normal Chrome sends is what makes the pages render.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


# Selectors that indicate we reached a payment step. The adapters stop as
# soon as one of these is visible: no card data is ever typed by the bot.
PAYMENT_SELECTORS = (
    "input[name*='cardnumber' i]",
    "input[name*='card_number' i]",
    "input[autocomplete*='cc-number' i]",
    "input[id*='cardNumber' i]",
    "iframe[title*='payment' i]",
    "iframe[title*='card' i]",
    "iframe[src*='payment' i]",
    "[data-testid*='payment' i]",
    "#payment-method",
    ".payment-method",
    # The last step of a checkout: the button that would place the
    # booking. Seeing it means the payment step is on screen.
    "button:has-text('Complete booking')",
    "button:has-text('Pay now')",
)

# Interstitials that mean the provider is challenging the automation.
# We abort with a clear message instead of trying to work around them.
BLOCK_SELECTORS = (
    "#captcha-form",
    "form[action*='recaptcha' i]",
    "#recaptcha",
    "div[title*='unusual traffic' i]",
    "[data-testid*='captcha' i]",
    "text=/unusual traffic/i",
    "text=/are you a robot/i",
    "text=/verify you are human/i",
)


class BrowserSession:
    """A reusable headless-by-default Chromium session."""

    def __init__(
        self,
        headless: bool | None = None,
        timeout_ms: int | None = None,
        slow_mo_ms: int | None = None,
        user_data_dir: str | None = None,
        storage_state: str | None = None,
        proxy: str | None = None,
    ):
        self.headless = _env_flag("BROWSER_HEADLESS", True) if headless is None else headless
        self.timeout_ms = (
            _env_int("BROWSER_TIMEOUT_MS", 45000)
            if timeout_ms is None
            else int(timeout_ms)
        )
        self.slow_mo_ms = (
            _env_int("BROWSER_SLOW_MO_MS", 0) if slow_mo_ms is None else int(slow_mo_ms)
        )
        self.user_data_dir = (
            os.getenv("BROWSER_USER_DATA_DIR", "").strip()
            if user_data_dir is None
            else (user_data_dir or "").strip()
        )
        self.storage_state = (
            os.getenv("BROWSER_STORAGE_STATE", "").strip()
            if storage_state is None
            else (storage_state or "").strip()
        )
        self.proxy = (
            os.getenv("BROWSER_PROXY", "").strip()
            if proxy is None
            else (proxy or "").strip()
        )
        self.user_agent = (
            os.getenv("BROWSER_USER_AGENT", "").strip()
            or DEFAULT_USER_AGENT
        )
        self.locale = os.getenv("BROWSER_LOCALE", "en-US").strip() or "en-US"

        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="tripmate-browser"
        )
        self._playwright = None
        self._browser = None
        self._context = None
        self._closed = False

    # ------------------------------------------------------------------
    # lifecycle (all of it runs on the browser thread)
    # ------------------------------------------------------------------

    def _ensure_started(self) -> None:
        """Launch the browser. Called on the browser thread only."""
        if self._context is not None:
            return
        self._resolve_browsers_path()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:  # pragma: no cover - dependency guard
            raise ProviderError(
                "Playwright is not installed. Run: pip install playwright "
                "&& python -m playwright install chromium"
            ) from error

        launch_args = {
            "headless": self.headless,
            "slow_mo": self.slow_mo_ms,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }
        if self.proxy:
            launch_args["proxy"] = {"server": self.proxy}

        try:
            self._playwright = sync_playwright().start()
            if self.user_data_dir:
                Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)
                self._context = self._playwright.chromium.launch_persistent_context(
                    self.user_data_dir, **launch_args
                )
            else:
                self._browser = self._playwright.chromium.launch(**launch_args)
                self._context = self._browser.new_context(
                    storage_state=self.storage_state or None,
                    user_agent=self.user_agent,
                    locale=self.locale,
                    viewport={"width": 1440, "height": 900},
                    extra_http_headers={"Accept-Language": f"{self.locale},en;q=0.9"},
                )
            self._context.set_default_timeout(self.timeout_ms)
            self._context.set_default_navigation_timeout(self.timeout_ms)
        except ProviderError:
            raise
        except Exception as error:  # pragma: no cover - launch failures
            self._teardown()
            raise ProviderError(
                f"Could not start the browser: {error}", transient=True
            ) from error

    def _resolve_browsers_path(self) -> None:
        """Point Playwright at the browsers it was installed with.

        Playwright defaults to ``$XDG_CACHE_HOME/ms-playwright``, which is
        not where ``playwright install`` puts them when XDG_CACHE_HOME is
        redirected (a VS Code sandbox, for instance). If the standard
        ``~/.cache/ms-playwright`` folder exists we use it.
        """
        if os.getenv("PLAYWRIGHT_BROWSERS_PATH"):
            return
        fallback = Path.home() / ".cache" / "ms-playwright"
        if fallback.is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(fallback)

    def _teardown(self) -> None:
        for attr in ("_context", "_browser"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:  # pragma: no cover - best effort teardown
                    pass
                setattr(self, attr, None)
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # pragma: no cover
                pass
            self._playwright = None

    def close(self) -> None:
        """Close the browser and stop the worker thread."""
        if self._closed:
            return
        self._closed = True
        try:
            self._executor.submit(self._teardown).result(timeout=30)
        except Exception:  # pragma: no cover - teardown must never raise
            pass
        self._executor.shutdown(wait=False)

    def __enter__(self) -> "BrowserSession":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------------
    # running browser work
    # ------------------------------------------------------------------

    def run(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        """Run ``fn(page, *args, **kwargs)`` on the browser thread.

        The callable receives a fresh page and must use it only there.
        """
        if self._closed:
            raise ProviderError("The browser session is closed.", transient=False)

        def task():
            self._ensure_started()
            page = self._context.new_page()
            try:
                page.set_default_timeout(self.timeout_ms)
                page.set_default_navigation_timeout(self.timeout_ms)
                return fn(page, *args, **kwargs)
            finally:
                try:
                    page.close()
                except Exception:  # pragma: no cover
                    pass

        future = self._executor.submit(task)
        try:
            return future.result()
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError(
                f"Browser operation failed: {error}", transient=True
            ) from error

    # ------------------------------------------------------------------
    # guards
    # ------------------------------------------------------------------

    @staticmethod
    def check_blocked(page) -> None:
        """Abort when the provider shows a bot challenge.

        TripMate does not attempt to solve or bypass CAPTCHAs: the run stops
        and the user is told to complete that step in a normal browser.
        """
        try:
            for selector in BLOCK_SELECTORS:
                if page.locator(selector).count() > 0:
                    raise ProviderError(
                        "The provider asked for a human verification step. "
                        "Open the site in a normal browser and finish there.",
                        transient=False,
                        payload={"selector": selector},
                    )
        except ProviderError:
            raise
        except Exception:  # pragma: no cover - a broken selector must not fail the run
            return

    @staticmethod
    def check_payment_step(page) -> bool:
        """True when the page has reached a payment form."""
        try:
            for selector in PAYMENT_SELECTORS:
                if page.locator(selector).count() > 0:
                    return True
        except Exception:  # pragma: no cover
            return False
        return False

    def screenshot(self, page, name: str) -> str:
        """Save a screenshot next to BROWSER_ARTIFACT_DIR and return its path."""
        artifact_dir = os.getenv("BROWSER_ARTIFACT_DIR", "").strip()
        if not artifact_dir:
            return ""
        try:
            directory = Path(artifact_dir)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{name}.png"
            page.screenshot(path=str(path), full_page=False)
            return str(path)
        except Exception:  # pragma: no cover - screenshots are a nice-to-have
            return ""
