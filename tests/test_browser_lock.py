import unittest
from unittest.mock import patch

from hh_mcp_server.drivers import browser


class BrowserSessionLockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await browser.close_browser()

    async def test_busy_session_error_includes_wait_and_owner_metadata(self):
        with (
            patch.object(browser, "SESSION_LOCK_WAIT_SECONDS", 0.25),
            patch.object(browser, "acquire_exclusive_lock", side_effect=BlockingIOError("busy")),
            patch.object(browser, "_read_session_owner", return_value="pid=1234, acquired_at=2026-09-29T12:00:00+00:00"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"HH session stayed busy for 0\.25s\. Owner: pid=1234",
            ):
                await browser.get_or_create_context()
