from typing import Any

from fastmcp import FastMCP

from hh_mcp_server.constants import TOOL_TIMEOUT_SECONDS
from hh_mcp_server.drivers.browser import get_page
from hh_mcp_server.scraping.responses import get_my_responses
from hh_mcp_server.utils.auth import ensure_authenticated


def register_response_tools(mcp: FastMCP) -> None:

    @mcp.tool(timeout=TOOL_TIMEOUT_SECONDS, title="Get My Responses")
    async def get_responses(
        limit: int | None = None, include_deleted: bool = True
    ) -> dict[str, Any]:
        """Get user applications. Use a small limit for recent responses; include deleted for full dedup history."""
        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1")
        page = await get_page()
        await ensure_authenticated(page)
        return await get_my_responses(
            page, limit=limit, include_deleted=include_deleted
        )
