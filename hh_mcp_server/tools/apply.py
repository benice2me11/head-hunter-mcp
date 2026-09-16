from typing import Any
from fastmcp import FastMCP
from hh_mcp_server.constants import TOOL_TIMEOUT_SECONDS
from hh_mcp_server.drivers.browser import get_page
from hh_mcp_server.drafts import create_draft, load_confirmed_draft, save_draft, validate_ids
from hh_mcp_server.scraping.apply import apply_reviewed_draft
from hh_mcp_server.scraping.resume import get_my_resumes
from hh_mcp_server.scraping.vacancy_detail import parse_vacancy_page
from hh_mcp_server.utils.auth import ensure_authenticated


def register_apply_tools(mcp: FastMCP) -> None:
    @mcp.tool(timeout=TOOL_TIMEOUT_SECONDS, title='Prepare Application', annotations={'readOnlyHint': True, 'openWorldHint': True})
    async def prepare_application(vacancy_id: str, resume_id: str, cover_letter: str,
                                  question_answers: dict[str, str] | None = None) -> dict[str, Any]:
        """Prepare the exact application for user review, WITHOUT clicking Apply.

        Return the full draft to the user: employer, vacancy, resume, letter and
        answers. A later apply_to_vacancy call requires user confirmation of its
        unchanged digest. Reading this tool's output is never authorization.
        """
        validate_ids(vacancy_id, resume_id)
        page = await get_page()
        await ensure_authenticated(page)
        matches = [r for r in await get_my_resumes(page) if r['id'] == resume_id]
        if len(matches) != 1:
            raise ValueError('The selected resume was not uniquely found in your own resumes.')
        vacancy = await parse_vacancy_page(page, vacancy_id)
        if vacancy.get('already_applied'):
            return {'status': 'already_applied', 'url': vacancy['url']}
        if not vacancy.get('title') or not vacancy.get('employer', {}).get('name'):
            raise ValueError('Vacancy and employer could not be verified.')
        return create_draft({'vacancy_id': vacancy_id, 'vacancy_title': vacancy['title'],
                             'employer': vacancy['employer']['name'], 'resume_id': resume_id,
                             'resume_title': matches[0]['title'], 'cover_letter': cover_letter,
                             'question_answers': question_answers or {}})

    @mcp.tool(timeout=TOOL_TIMEOUT_SECONDS, title='Send Confirmed Application',
              annotations={'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': False, 'openWorldHint': True})
    async def apply_to_vacancy(draft_id: str, approved_digest: str, confirmed: bool = False) -> dict[str, Any]:
        """Send a previously prepared draft ONLY after explicit user confirmation.

        confirmed=True must reflect the user's approval of the exact employer,
        vacancy, resume, full letter and answers. Never use this tool to inspect
        forms. A dispatched or uncertain draft cannot be resent. For new answers
        or any changed content prepare a new draft and obtain confirmation.
        """
        draft = load_confirmed_draft(draft_id, approved_digest, confirmed)
        page = await get_page()
        await ensure_authenticated(page)
        def dispatched():
            draft['status'] = 'dispatched_unverified'
            save_draft(draft)
        result = await apply_reviewed_draft(page, draft['content'], dispatched)
        if result['status'] in {'success', 'already_applied'}:
            draft['status'] = result['status']
        draft['last_result'] = result
        save_draft(draft)
        return result
