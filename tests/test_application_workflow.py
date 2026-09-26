"""End-to-end tool functions on an intercepted browser; no real HH requests."""
from pathlib import Path
import os
import tempfile
from unittest.mock import AsyncMock, patch
import unittest

from hh_mcp_server import drafts, ledger
from hh_mcp_server.ledger import Journal, draft_context
from hh_mcp_server.tools.apply import register_apply_tools
import test_safety as safety
from test_snapshots import RESUME

CONTENT = safety.CONTENT


class ToolRegistry:
    def __init__(self):
        self.methods = {}

    def tool(self, *args, **kwargs):
        def register(function):
            self.methods[function.__name__] = function
            return function
        return register


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    serve_fixture = safety.BrowserSafetyTests.serve_fixture

    async def asyncSetUp(self):
        await safety.BrowserSafetyTests.asyncSetUp(self)
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.salary = '100 RUB net'
        self.resume = {'raw_text': RESUME['raw_text'].replace('Fixture Resume', CONTENT['resume_title'])}
        self.registry = ToolRegistry()
        register_apply_tools(self.registry)

        async def get_page():
            if self.page.is_closed():
                self.page = await self.context.new_page()
                self.page.set_default_timeout(2000)
            return self.page

        async def read_resume(*args):
            return self.resume

        self.patchers = [patch.object(drafts, 'DRAFT_DIR', self.root / 'drafts'),
                         patch.object(ledger, 'PROFILE_DIR', self.root),
                         patch.dict(os.environ, {'HH_MONITORING_FILE': ''}),
                         patch('hh_mcp_server.tools.apply.get_page', side_effect=get_page),
                         patch('hh_mcp_server.tools.apply.ensure_authenticated', new=AsyncMock()),
                         patch('hh_mcp_server.tools.apply.get_my_resumes', new=AsyncMock(return_value=[
                             {'id': CONTENT['resume_id'], 'title': CONTENT['resume_title']} ])),
                         patch('hh_mcp_server.tools.apply.parse_resume_page', side_effect=read_resume)]
        for p in self.patchers:
            p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.directory.cleanup()
        await safety.BrowserSafetyTests.asyncTearDown(self)

    def fixture_html(self):
        html = safety.BrowserSafetyTests.fixture_html(self)
        extra = ('<div data-qa="vacancy-description"><p>Build useful software.</p></div>'
                 f'<span data-qa="vacancy-salary">{self.salary}</span>'
                 '<span data-qa="work-formats-text">Remote</span>')
        if self.scenario == 'auto_apply':
            extra += "<script>fetch('/applicant/vacancy_response/popup',{method:'POST',body:'unapproved=true'}).catch(()=>{});</script>"
        return html.replace('<button id="open"', extra + '<button id="open"')

    async def prepare(self):
        return await self.registry.methods['prepare_application'](
            CONTENT['vacancy_id'], CONTENT['resume_id'], CONTENT['cover_letter'])

    def approve(self, draft):
        with Journal() as journal:
            journal.append('approved', draft_context(draft), evidence_ref='fixture:user-consent')
            journal.append('history_checked', draft_context(draft), evidence_ref='fixture:verified-history',
                           data={'result': 'no_prior_response', 'checked_ids': [CONTENT['vacancy_id']]})

    async def send(self, draft):
        return await self.registry.methods['apply_to_vacancy'](draft['draft_id'], draft['digest'], True)

    async def test_prepare_blocks_even_a_page_initiated_post(self):
        self.scenario = 'auto_apply'
        draft = await self.prepare()
        self.assertEqual(draft['content']['content_schema'], 2)
        self.assertEqual(self.posts, [])
        self.assertTrue(self.page.is_closed())
        with Journal() as journal:
            self.assertEqual([e['kind'] for e in journal.events], ['draft_prepared'])

    async def test_boolean_confirmation_without_durable_consent_cannot_send(self):
        draft = await self.prepare()
        with self.assertRaisesRegex(ValueError, 'approval'):
            await self.send(draft)
        self.assertEqual(self.posts, [])

    async def test_full_reviewed_cycle_records_exact_dispatch_and_blocks_second_draft(self):
        first = await self.prepare()
        second = await self.prepare()
        self.approve(first)
        self.approve(second)
        result = await self.send(first)
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(len(self.posts), 1)
        with Journal() as journal:
            kinds = [e['kind'] for e in journal.events]
            self.assertLess(kinds.index('dispatch_started'), kinds.index('attempt_result'))
            self.assertEqual(journal.events[-1]['data'], result)
        with self.assertRaisesRegex(ValueError, 'dispatched'):
            await self.send(second)
        self.assertEqual(len(self.posts), 1)

    async def test_changed_resume_blocks_before_application_form(self):
        draft = await self.prepare()
        self.approve(draft)
        self.resume['raw_text'] = self.resume['raw_text'].replace('Go\nReact', 'Python\nReact')
        result = await self.send(draft)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('Resume materially changed', result['reason'])
        self.assertEqual(self.posts, [])

    async def test_changed_salary_blocks_before_application_form(self):
        draft = await self.prepare()
        self.approve(draft)
        self.salary = '90 RUB gross'
        result = await self.send(draft)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('terms materially changed', result['reason'])
        self.assertEqual(self.posts, [])

    async def test_cancelled_draft_cannot_send(self):
        draft = await self.prepare()
        self.approve(draft)
        with Journal() as journal:
            journal.append('cancelled', draft_context(draft), evidence_ref='fixture:user-cancel')
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            await self.send(draft)
        self.assertEqual(self.posts, [])

    async def test_uncertain_result_cannot_be_bypassed_with_second_draft(self):
        first = await self.prepare()
        second = await self.prepare()
        self.approve(first)
        self.approve(second)
        self.scenario = 'no_marker'
        result = await self.send(first)
        self.assertEqual(result['status'], 'unverified')
        with self.assertRaisesRegex(ValueError, 'dispatched'):
            await self.send(second)
        self.assertEqual(len(self.posts), 1)
