import fcntl
import logging
import os
from typing import Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from hh_mcp_server.constants import PROFILE_DIR, STATE_FILE
from hh_mcp_server.private_files import private_directory, write_private_json

logger = logging.getLogger(__name__)
_playwright = None
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None
_headless = True
_lock_fd = None


def set_headless(value: bool) -> None:
    global _headless
    _headless = value


async def get_or_create_context() -> BrowserContext:
    global _playwright, _browser, _context, _lock_fd
    if _context is not None:
        return _context
    private_directory(PROFILE_DIR)
    lock_fd = os.open(PROFILE_DIR / "session.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        raise RuntimeError("HH session is in use by another local process; close that session first.") from None
    _lock_fd = lock_fd
    try:
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(headless=_headless)
        kwargs = {"viewport": {"width": 1440, "height": 960}, "locale": "ru-RU", "service_workers": "block"}
        if STATE_FILE.exists():
            STATE_FILE.chmod(0o600)
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
                os.close(_lock_fd)
                _lock_fd = None
