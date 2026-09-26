from typing import Any

from fastmcp import FastMCP

from hh_mcp_server.constants import TOOL_TIMEOUT_SECONDS
from hh_mcp_server.drivers.browser import get_page
from hh_mcp_server.resume_updates import editable_resume_summary, validate_resume_id
from hh_mcp_server.scraping.resume import get_my_resumes as _get_resumes, parse_resume_page
from hh_mcp_server.scraping.resume_update import read_owned_resume_states
from hh_mcp_server.utils.auth import ensure_authenticated


def register_resume_tools(mcp: FastMCP) -> None:

    @mcp.tool(timeout=TOOL_TIMEOUT_SECONDS, title="Get My Resumes")
    async def get_my_resumes() -> list[dict[str, Any]]:
        """Get list of user's resumes on hh.ru."""
        page = await get_page()
        await ensure_authenticated(page)
        return await _get_resumes(page)

    @mcp.tool(timeout=TOOL_TIMEOUT_SECONDS, title="Get Resume")
    async def get_resume(resume_id: str) -> dict[str, Any]:
        """Get full owned resume content plus stable editable field IDs.

        Args:
            resume_id: Resume ID (from get_my_resumes)
        """
        validate_resume_id(resume_id)
        page = await get_page()
        await ensure_authenticated(page)
        owned = [item for item in await _get_resumes(page) if item.get("id") == resume_id]
        if len(owned) != 1:
            raise ValueError(
                "The selected resume was not uniquely found in your own resumes"
            )
        raw = await parse_resume_page(page, resume_id)
        structured = await read_owned_resume_states(page, owned)
        if len(structured) != 1:
            raise ValueError("Structured owned resume state could not be verified")
        return {**raw, "editable": editable_resume_summary(structured[0])}
