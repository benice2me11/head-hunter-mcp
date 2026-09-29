import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from typing import Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from hh_mcp_server.constants import PROFILE_DIR, SESSION_LOCK_WAIT_SECONDS, STATE_FILE
from hh_mcp_server.platform_io import acquire_exclusive_lock, set_private_path_mode
from hh_mcp_server.private_files import private_directory, write_private_json

logger = logging.getLogger(__name__)
_playwright = None
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None
_headless = True
_lock_fd = None
_SESSION_OWNER_FILE = PROFILE_DIR / "session.owner.json"


def set_headless(value: bool) -> None:
    global _headless
    _headless = value


def _read_session_owner() -> str | None:
    try:
        value = json.loads(_SESSION_OWNER_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    pid = value.get("pid")
    acquired_at = value.get("acquired_at")
    if pid is None:
        return None
    return f"pid={pid}, acquired_at={acquired_at}" if acquired_at else f"pid={pid}"


async def get_or_create_context() -> BrowserContext:
    global _playwright, _browser, _context, _lock_fd
    if _context is not None:
        return _context
    private_directory(PROFILE_DIR)
    try:
        lock_fd = await asyncio.to_thread(
            acquire_exclusive_lock,
            PROFILE_DIR / "session.lock",
            wait_seconds=SESSION_LOCK_WAIT_SECONDS,
        )
    except BlockingIOError:
        owner = _read_session_owner()
        owner_suffix = f" Owner: {owner}." if owner else ""
        raise RuntimeError(
            f"HH session stayed busy for {SESSION_LOCK_WAIT_SECONDS:g}s.{owner_suffix}"
        ) from None
    _lock_fd = lock_fd
    try:
        write_private_json(
            _SESSION_OWNER_FILE,
            {
                "pid": os.getpid(),
                "acquired_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(headless=_headless)
        kwargs = {"viewport": {"width": 1440, "height": 960}, "locale": "ru-RU", "service_workers": "block"}
        if STATE_FILE.exists():
            set_private_path_mode(STATE_FILE, 0o600)
            kwargs["storage_state"] = str(STATE_FILE)
        _context = await _browser.new_context(**kwargs)
        return _context
    except BaseException:
        await close_browser()
        raise


async def get_page() -> Page:
    context = await get_or_create_context()
    return context.pages[0] if context.pages else await context.new_page()


async def save_state() -> None:
    if _context is not None:
        write_private_json(STATE_FILE, await _context.storage_state())


async def close_browser() -> None:
    global _playwright, _browser, _context, _lock_fd
    try:
        if _context is not None:
            try:
                await save_state()
            finally:
                await _context.close()
    finally:
        _context = None
        try:
            if _browser is not None:
                await _browser.close()
            if _playwright is not None:
                await _playwright.stop()
        finally:
            _browser = None
            _playwright = None
            if _lock_fd is not None:
                try:
                    _SESSION_OWNER_FILE.unlink(missing_ok=True)
                finally:
                    os.close(_lock_fd)
                    _lock_fd = None
