import logging
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware

from hh_mcp_server.constants import TOOL_TIMEOUT_SECONDS
from hh_mcp_server.drivers.browser import close_browser
from hh_mcp_server.tools.vacancy import register_vacancy_tools
from hh_mcp_server.tools.apply import register_apply_tools
from hh_mcp_server.tools.resume import register_resume_tools
from hh_mcp_server.tools.resume_update import register_resume_update_tools
from hh_mcp_server.tools.employer import register_employer_tools
from hh_mcp_server.tools.responses import register_response_tools

logger = logging.getLogger(__name__)


class SingleBrowserOperation(Middleware):
    def __init__(self):
        self.lock = asyncio.Lock()

    async def on_call_tool(self, context, call_next):
        async with self.lock:
            return await call_next(context)


@asynccontextmanager
async def server_lifespan(app: FastMCP) -> AsyncIterator[dict[str, Any]]:
    logger.info("HH MCP Server starting...")
    try:
        yield {}
    finally:
        logger.info("HH MCP Server shutting down...")
        await close_browser()


def create_mcp_server() -> FastMCP:
    mcp = FastMCP("hh_scraper", lifespan=server_lifespan)
    mcp.add_middleware(SingleBrowserOperation())

    register_vacancy_tools(mcp)
    register_apply_tools(mcp)
    register_resume_tools(mcp)
    register_resume_update_tools(mcp)
    register_employer_tools(mcp)
    register_response_tools(mcp)

    @mcp.tool(timeout=TOOL_TIMEOUT_SECONDS, title="Close Session")
    async def close_session() -> dict[str, Any]:
        """Close the browser session and save state."""
        await close_browser()
        return {"status": "success", "message": "Browser session closed and state saved."}

    return mcp
