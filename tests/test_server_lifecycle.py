import unittest
from unittest.mock import AsyncMock, patch

from hh_mcp_server.server import SingleBrowserOperation


class BrowserOperationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_is_closed_after_successful_tool_call(self):
        middleware = SingleBrowserOperation()
        call_next = AsyncMock(return_value={"ok": True})

        with patch("hh_mcp_server.server.close_browser", new=AsyncMock()) as close_browser:
            result = await middleware.on_call_tool(object(), call_next)

        self.assertEqual(result, {"ok": True})
        close_browser.assert_awaited_once()

    async def test_browser_is_closed_after_failed_tool_call(self):
        middleware = SingleBrowserOperation()
        call_next = AsyncMock(side_effect=RuntimeError("tool failed"))

        with patch("hh_mcp_server.server.close_browser", new=AsyncMock()) as close_browser:
            with self.assertRaisesRegex(RuntimeError, "tool failed"):
                await middleware.on_call_tool(object(), call_next)

        close_browser.assert_awaited_once()
