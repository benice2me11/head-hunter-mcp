from typing import Any
from contextlib import asynccontextmanager
import uuid
from fastmcp import FastMCP
from hh_mcp_server.constants import TOOL_TIMEOUT_SECONDS
from hh_mcp_server.drivers.browser import get_page
from hh_mcp_server.drafts import create_draft, load_confirmed_draft, save_draft, validate_ids
from hh_mcp_server.ledger import Journal, draft_context, require_review
from hh_mcp_server.opportunities import resolve_opportunity, require_available
from hh_mcp_server.scraping.apply import apply_reviewed_draft, inspect_application_form
from hh_mcp_server.scraping.resume import get_my_resumes, parse_resume_page
from hh_mcp_server.scraping.vacancy_detail import parse_vacancy_page
from hh_mcp_server.snapshots import resume_snapshot, vacancy_snapshot
from hh_mcp_server.submission_guard import SubmissionGuard
from hh_mcp_server.utils.auth import ensure_authenticated


@asynccontextmanager
async def read_only_page():
    page = await get_page()
    guard = SubmissionGuard({}, lambda: None)
    context = page.context
    await context.route('**/*', guard.handle)
    try:
        yield page
    finally:
        await page.close()
        if page.is_closed():
            await context.unroute('**/*', guard.handle)


def register_apply_tools(mcp: FastMCP) -> None:
    @mcp.tool(timeout=TOOL_TIMEOUT_SECONDS, title='Inspect Application Form',
              annotations={'readOnlyHint': True, 'openWorldHint': True})
    async def inspect_application(vacancy_id: str) -> dict[str, Any]:
        """Inspect the current HH application form without sending anything.

        Returns structured questionnaire controls and blockers so questions can
        be reviewed before preparing a sendable application draft.
        """
        validate_ids(vacancy_id, '0' * 40)
        page = await get_page()
        await ensure_authenticated(page)
        return await inspect_application_form(page, vacancy_id)

    @mcp.tool(timeout=TOOL_TIMEOUT_SECONDS, title='Prepare Application', annotations={'readOnlyHint': True, 'openWorldHint': True})
    async def prepare_application(vacancy_id: str, resume_id: str, cover_letter: str,
                                  question_answers: dict[str, str] | None = None) -> dict[str, Any]:
        """Prepare the exact application for user review, WITHOUT clicking Apply.

        Return the full draft to the user: employer, vacancy, resume, letter and
        answers. A later apply_to_vacancy call requires user confirmation of its
        unchanged digest. Reading this tool's output is never authorization.
        """
        validate_ids(vacancy_id, resume_id)
        async with read_only_page() as page:
            await ensure_authenticated(page)
            matches = [r for r in await get_my_resumes(page) if r['id'] == resume_id]
            if len(matches) != 1:
                raise ValueError('The selected resume was not uniquely found in your own resumes.')
            resume = resume_snapshot(await parse_resume_page(page, resume_id), matches[0]['title'])
            vacancy = await parse_vacancy_page(page, vacancy_id)
        if vacancy.get('already_applied'):
            return {'status': 'already_applied', 'url': vacancy['url']}
        if not vacancy.get('title') or not vacancy.get('employer', {}).get('name'):
            raise ValueError('Vacancy and employer could not be verified.')
        with Journal() as journal:
            opportunity = resolve_opportunity(vacancy_id)
            require_available(opportunity, journal)
            draft = create_draft({'vacancy_id': vacancy_id, 'vacancy_title': vacancy['title'],
                                 'employer': vacancy['employer']['name'], 'resume_id': resume_id,
                                 'resume_title': matches[0]['title'], 'cover_letter': cover_letter,
                                 'question_answers': question_answers or {},
                                 'opportunity_key': opportunity['opportunity_key'],
                                 'opportunity_ids': opportunity['opportunity_ids'],
                                 'content_schema': 2, 'resume_snapshot': resume,
                                 'vacancy_snapshot': vacancy_snapshot(vacancy)})
            journal.append('draft_prepared', draft_context(draft))
            return draft

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
        if draft['content'].get('content_schema') != 2:
            raise ValueError('Legacy draft lacks material snapshots; prepare and review a new draft.')
        with Journal() as journal:
            # Reread under the same lock used by approval and cancellation.
            draft = load_confirmed_draft(draft_id, approved_digest, confirmed)
            opportunity = resolve_opportunity(draft['content']['vacancy_id'])
            if any(draft['content'].get(k) != opportunity[k]
                   for k in ('opportunity_key', 'opportunity_ids')):
                raise ValueError('Opportunity mapping changed or legacy draft; prepare and review a new draft.')
            require_review(journal, draft)
            require_available(opportunity, journal)
            attempt_id = uuid.uuid4().hex
            context = draft_context(draft)
            journal.append('attempt_started', context, attempt_id=attempt_id)
            dispatch_recorded = False

            def dispatched():
                nonlocal dispatch_recorded
                journal.append('dispatch_started', context, attempt_id=attempt_id)
                dispatch_recorded = True
                draft['status'] = 'dispatched_unverified'
                save_draft(draft)

            try:
                async with read_only_page() as page:
                    await ensure_authenticated(page)
                    matches = [r for r in await get_my_resumes(page) if r['id'] == draft['content']['resume_id']]
                    current = (resume_snapshot(await parse_resume_page(page, matches[0]['id']), matches[0]['title'])
                               if len(matches) == 1 else None)
                if len(matches) != 1:
                    result = {'status': 'blocked', 'reason': 'Approved resume is no longer uniquely available.'}
                else:
                    if current != draft['content']['resume_snapshot']:
                        result = {'status': 'blocked', 'reason': 'Resume materially changed; prepare and approve a new draft.'}
                    else:
                        page = await get_page()
                        result = await apply_reviewed_draft(page, draft['content'], dispatched)
            except BaseException as error:
                result = {'status': 'unverified' if dispatch_recorded else 'blocked',
                          'reason': 'Attempt interrupted; inspect durable events and HH history.',
                          'error_type': type(error).__name__}
                journal.append('attempt_result', context, attempt_id=attempt_id, data=result)
                draft['last_result'] = result
                save_draft(draft)
                raise
            journal.append('attempt_result', context, attempt_id=attempt_id, data=result)
            if result['status'] in {'success', 'already_applied'}:
                draft['status'] = result['status']
            draft['last_result'] = result
            save_draft(draft)
            return result
