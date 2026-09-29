"""Safety regressions use synthetic pages and never connect to a real HH account."""
import copy
from datetime import datetime, timedelta, timezone
import json
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastmcp import Client
from fastmcp.exceptions import ToolError
from playwright.async_api import async_playwright

from hh_mcp_server import drafts
from hh_mcp_server.scraping.apply import apply_reviewed_draft
from hh_mcp_server.scraping.resume import get_my_resumes
from hh_mcp_server.server import create_mcp_server
from hh_mcp_server.submission_guard import SubmissionGuard, matches_application, parse_fields
from hh_mcp_server.utils.auth import ensure_authenticated
from hh_mcp_server.exceptions import AuthenticationError


CONTENT = {
    'vacancy_id': '123456', 'vacancy_title': 'Fixture Engineer',
    'employer': 'Fixture Employer', 'resume_id': 'a' * 40,
    'resume_title': 'Approved resume', 'cover_letter': 'Approved letter\nSecond line',
    'question_answers': {},
}


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name) / 'drafts'
        self.patcher = patch.object(drafts, 'DRAFT_DIR', self.folder)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_confirmation_required_before_file_access(self):
        with patch.object(drafts, 'draft_path', side_effect=AssertionError('file access')):
            with self.assertRaisesRegex(ValueError, 'confirmation'):
                drafts.load_confirmed_draft('invalid', '', False)

    def test_changed_content_is_rejected(self):
        draft = drafts.create_draft(copy.deepcopy(CONTENT))
        digest = draft['digest']
        draft['content']['resume_id'] = 'b' * 40
        drafts.save_draft(draft)
        with self.assertRaisesRegex(ValueError, 'changed'):
            drafts.load_confirmed_draft(draft['draft_id'], digest, True)

    def test_expired_draft_is_rejected(self):
        draft = drafts.create_draft(copy.deepcopy(CONTENT))
        draft['created_at'] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        drafts.save_draft(draft)
        with self.assertRaisesRegex(ValueError, 'expired'):
            drafts.load_confirmed_draft(draft['draft_id'], draft['digest'], True)

    def test_uncertain_dispatch_cannot_be_retried(self):
        draft = drafts.create_draft(copy.deepcopy(CONTENT))
        draft['status'] = 'dispatched_unverified'
        drafts.save_draft(draft)
        with self.assertRaisesRegex(ValueError, 'already been dispatched'):
            drafts.load_confirmed_draft(draft['draft_id'], draft['digest'], True)

    def test_private_permissions_and_valid_review(self):
        draft = drafts.create_draft(copy.deepcopy(CONTENT))
        self.assertEqual(self.folder.stat().st_mode & 0o777, 0o700)
        self.assertEqual(drafts.draft_path(draft['draft_id']).stat().st_mode & 0o777, 0o600)
        self.assertEqual(drafts.load_confirmed_draft(draft['draft_id'], draft['digest'], True), draft)

    def test_question_fields_cannot_override_destination(self):
        content = copy.deepcopy(CONTENT)
        content['question_answers'] = {'vacancy_id': '999999'}
        with self.assertRaises(ValueError):
            drafts.create_draft(content)


class EncodingTests(unittest.TestCase):
    def test_duplicate_fields_rejected(self):
        for body, kind in [(b'{"resume_id":"a","resume_id":"b"}', 'application/json'),
                           (b'resume_id=a&resume_id=b', 'application/x-www-form-urlencoded')]:
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                parse_fields(body, kind)

    def test_nested_json_rejected(self):
        with self.assertRaises(ValueError):
            parse_fields(b'{"answers":{"unreviewed":"value"}}', 'application/json')

    def test_letter_and_answers_must_match_exactly(self):
        fields = {'vacancy_id': CONTENT['vacancy_id'], 'resume_hash': CONTENT['resume_id'],
                  'letter': CONTENT['cover_letter']}
        self.assertTrue(matches_application(fields, CONTENT))
        for change in [{'letter': ''}, {'task_unknown': 'unreviewed'}, {'resume_id': 'b' * 40}]:
            with self.subTest(change=change):
                self.assertFalse(matches_application(fields | change, CONTENT))

    def test_form_newlines_preserve_reviewed_letter_and_answer_text(self):
        expected = CONTENT | {'question_answers': {'task_1': 'First\nSecond'}}
        fields = {'vacancy_id': CONTENT['vacancy_id'], 'resume_hash': CONTENT['resume_id'],
                  'letter': CONTENT['cover_letter'].replace('\n', '\r\n'), 'task_1': 'First\r\nSecond'}
        self.assertTrue(matches_application(fields, expected))
        for changed in [fields['letter'] + ' ', fields['letter'] + '\r\n', fields['letter'].replace('\r\n', ' '),
                        fields['letter'].replace('\r\n', '\r')]:
            with self.subTest(changed=changed):
                self.assertFalse(matches_application(fields | {'letter': changed}, expected))
        self.assertFalse(matches_application(fields | {'task_1': 'First\r\nChanged'}, expected))


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_confirmation_default_blocks_before_browser(self):
        with patch('hh_mcp_server.tools.apply.get_page', side_effect=AssertionError('browser access')):
            async with Client(create_mcp_server()) as client:
                tools = {tool.name: tool for tool in await client.list_tools()}
                self.assertIn('prepare_application', tools)
                self.assertFalse(tools['apply_to_vacancy'].inputSchema['properties']['confirmed']['default'])
                with self.assertRaisesRegex(ToolError, 'confirmation'):
                    await client.call_tool('apply_to_vacancy', {'draft_id': 'invalid', 'approved_digest': ''})


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_account_link_does_not_prove_login(self):
        from unittest.mock import AsyncMock, Mock
        page = Mock(url='https://hh.ru/vacancy/123456')
        page.locator.return_value.first.is_visible = AsyncMock(return_value=True)
        page.wait_for_selector = AsyncMock()
        async def redirect_to_login(*args, **kwargs):
            page.url = 'https://hh.ru/account/login'
        page.goto = AsyncMock(side_effect=redirect_to_login)
        with self.assertRaises(AuthenticationError):
            await ensure_authenticated(page)
        page.goto.assert_awaited_once()

    async def test_private_page_with_account_marker_is_accepted(self):
        from unittest.mock import AsyncMock, Mock
        page = Mock(url='https://hh.ru/applicant/resumes')
        page.locator.return_value.first.is_visible = AsyncMock(return_value=True)
        page.wait_for_selector = AsyncMock()
        await ensure_authenticated(page)
        self.assertFalse(page.goto.called)

    async def test_current_profile_redirect_is_accepted(self):
        from unittest.mock import AsyncMock, Mock
        page = Mock(url='https://hh.ru/vacancy/123456')
        page.locator.return_value.first.is_visible = AsyncMock(return_value=True)
        page.wait_for_selector = AsyncMock()
        async def redirect_to_profile(*args, **kwargs):
            page.url = 'https://hh.ru/applicant/profile/me'
        page.goto = AsyncMock(side_effect=redirect_to_profile)
        await ensure_authenticated(page)
        page.goto.assert_awaited_once()


class BrowserSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context(service_workers='block')
        self.page = await self.context.new_page()
        self.page.set_default_timeout(2000)
        self.posts = []
        self.dispatched = 0
        self.scenario = 'success'
        self.content = copy.deepcopy(CONTENT)
        # Every request is intercepted; this fixture never opens a network socket.
        await self.context.route('**/*', self.serve_fixture)

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()

    def on_dispatch(self):
        self.dispatched += 1

    def fixture_html(self):
        if self.posts and self.scenario != 'no_marker':
            return '<a data-qa="vacancy-response-link-view-topic">Application recorded</a>'
        employer = 'Changed Employer' if self.scenario == 'employer_changed' else CONTENT['employer']
        letter = '' if self.scenario == 'no_letter' else '<textarea name="letter"></textarea>'
        question = '<label>Why this role?<textarea name="task_42_text"></textarea></label>' if self.scenario in {'questions', 'approved_question'} else ''
        html = '''<!doctype html><html><body>
        <h1 data-qa="vacancy-title">Fixture Engineer</h1>
        <span data-qa="vacancy-company-name">EMPLOYER</span>
        <button id="open" data-qa="vacancy-response-link-top">Apply</button>
        <form id="application" hidden>
          <input name="vacancy_id" value="123456" type="hidden">
          <select name="resume_hash">
            <option value="WRONG_ID">Default unapproved resume</option>
            <option value="APPROVED_ID">Approved resume</option>
          </select>
          LETTER
          QUESTION
          <button data-qa="vacancy-response-submit-popup" type="submit">Send</button>
        </form>
        <script>
          const form = document.querySelector('#application');
          document.querySelector('#open').onclick = () => { OPEN_ACTION };
          form.onsubmit = async (event) => {
            event.preventDefault();
            const body = new URLSearchParams(new FormData(form));
            MUTATE_PAYLOAD
            try { await fetch('/applicant/vacancy_response/popup', {method:'POST',body}); } catch (_) {}
          };
        </script></body></html>'''
        open_action = "form.hidden=false;"
        if self.scenario == 'one_click':
            open_action = "fetch('/applicant/vacancy_response/popup', {method:'POST',body:new URLSearchParams(new FormData(form))}).catch(()=>{});"
        mutate = "body.set('resume_hash','" + 'b' * 40 + "');" if self.scenario == 'wrong_payload' else ''
        html = (html.replace('EMPLOYER', employer).replace('WRONG_ID', 'b' * 40)
                .replace('APPROVED_ID', CONTENT['resume_id']).replace('LETTER', letter)
                .replace('QUESTION', question).replace('OPEN_ACTION', open_action).replace('MUTATE_PAYLOAD', mutate))
        if self.scenario.startswith('modern_'):
            title = 'Other resume' if self.scenario == 'modern_wrong_title' else CONTENT['resume_title']
            warning_style = '' if self.scenario == 'modern_visible_warning' else 'max-height:0;overflow:hidden'
            selected = (f'<div role="dialog"><div data-qa="resume-title">{title}</div></div>'
                        f'<div data-qa="hidden-resume-warning" style="{warning_style}">Change resume visibility</div>')
            html = re.sub(r'<select name="resume_hash">.*?</select>', selected, html, flags=re.S)
            html = html.replace('<textarea name="letter"></textarea>',
                '<button type="button" data-qa="add-cover-letter" onclick="setTimeout(() => document.querySelector(\'textarea\').hidden=false, 50)">Add letter</button>'
                '<textarea data-qa="vacancy-response-popup-form-letter-input" hidden></textarea>')
            outgoing_id = 'b' * 40 if self.scenario == 'modern_wrong_payload' else CONTENT['resume_id']
            html = html.replace('const body = new URLSearchParams(new FormData(form));',
                'const body = new URLSearchParams(new FormData(form));'
                f'body.set("resume_hash", "{outgoing_id}");'
                'body.set("letter", document.querySelector("textarea").value);')
            if self.scenario == 'modern_multipart':
                html = html.replace('new URLSearchParams(new FormData(form))', 'new FormData(form)')
        return html

    async def serve_fixture(self, route):
        request = route.request
        parsed = urlparse(request.url)
        if parsed.hostname != 'hh.ru':
            await route.abort()
        elif request.method == 'GET' and parsed.path == '/vacancy/123456':
            await route.fulfill(status=200, content_type='text/html', body=self.fixture_html())
        elif request.method == 'POST' and parsed.path == '/applicant/vacancy_response/popup':
            if 'multipart/form-data' in request.headers.get('content-type', ''):
                fields = parse_fields(request.post_data_buffer, request.headers['content-type'])
                self.posts.append({k: [v] for k, v in fields.items()})
            else:
                self.posts.append(parse_qs(request.post_data, keep_blank_values=True))
            await route.fulfill(status=200, content_type='application/json', body='{"success":true}')
        else:
            await route.abort()

    async def run_application(self, scenario):
        self.scenario = scenario
        result = await apply_reviewed_draft(self.page, self.content, self.on_dispatch)
        self.assertTrue(self.page.is_closed(), 'Page scripts must stop before guard is removed')
        return result

    async def test_success_selects_approved_resume_and_exact_letter(self):
        result = await self.run_application('success')
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(self.dispatched, 1)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]['resume_hash'], [CONTENT['resume_id']])
        self.assertEqual(self.posts[0]['letter'], [CONTENT['cover_letter']])

    async def test_missing_letter_sends_nothing(self):
        result = await self.run_application('no_letter')
        self.assertEqual(result['status'], 'blocked', result)
        self.assertEqual(self.dispatched, 0)
        self.assertEqual(self.posts, [])

    async def test_payload_resume_tampering_sends_nothing(self):
        result = await self.run_application('wrong_payload')
        self.assertEqual(result['status'], 'blocked', result)
        self.assertEqual(self.posts, [])
        self.assertEqual(self.dispatched, 0)

    async def test_modern_selected_resume_and_delayed_letter_are_verified(self):
        result = await self.run_application('modern_success')
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]['resume_hash'], [CONTENT['resume_id']])
        self.assertEqual(self.posts[0]['letter'], [CONTENT['cover_letter']])

    async def test_modern_title_cannot_override_wrong_outgoing_resume_id(self):
        result = await self.run_application('modern_wrong_payload')
        self.assertEqual(result['status'], 'blocked', result)
        self.assertEqual(self.posts, [])
        self.assertEqual(self.dispatched, 0)

    async def test_browser_multipart_newline_serialization_sends_exact_text_once(self):
        result = await self.run_application('modern_multipart')
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(self.dispatched, 1)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]['letter'], [CONTENT['cover_letter'].replace('\n', '\r\n')])

    async def test_modern_unapproved_selected_title_sends_nothing(self):
        result = await self.run_application('modern_wrong_title')
        self.assertEqual(result['status'], 'blocked', result)
        self.assertEqual(self.posts, [])

    async def test_visible_privacy_warning_stops_before_dispatch(self):
        result = await self.run_application('modern_visible_warning')
        self.assertEqual(result['blocker'], 'resume_visibility_review_required', result)
        self.assertEqual(self.posts, [])
        self.assertEqual(self.dispatched, 0)

    async def test_initial_one_click_post_is_blocked(self):
        result = await self.run_application('one_click')
        self.assertEqual(result['status'], 'blocked', result)
        self.assertEqual(self.posts, [])
        self.assertEqual(self.dispatched, 0)

    async def test_unanswered_questions_return_for_review(self):
        result = await self.run_application('questions')
        self.assertEqual(result['status'], 'questions_required', result)
        self.assertEqual(result['questions'][0]['name'], 'task_42_text')
        self.assertEqual(self.posts, [])

    async def test_approved_question_answer_is_sent_exactly(self):
        self.content['question_answers'] = {'task_42_text': 'Approved answer'}
        result = await self.run_application('approved_question')
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(self.posts[0]['task_42_text'], ['Approved answer'])

    async def test_http_success_without_page_confirmation_is_unverified(self):
        result = await self.run_application('no_marker')
        self.assertEqual(result['status'], 'unverified', result)
        self.assertEqual(self.dispatched, 1)
        self.assertEqual(len(self.posts), 1)

    async def test_changed_employer_sends_nothing(self):
        result = await self.run_application('employer_changed')
        self.assertEqual(result['status'], 'blocked', result)
        self.assertEqual(self.posts, [])

    async def test_current_resume_titles_exclude_salary_and_keep_legacy_fallback(self):
        modern = '<div data-qa="resume"><a data-qa="resume-card-link-fixture" href="/resume/' + 'a' * 40 + '"><span>Permanent work</span><h3 data-qa="resume-title">Fixture Fullstack</h3><span>Salary and schedule</span></a></div>'
        legacy = '<div data-qa="resume"><a data-qa="resume-title-link" href="/resume/' + 'b' * 40 + '">Fixture Golang</a></div>'
        async def fixture(route):
            await route.fulfill(status=200, content_type='text/html', body=modern + legacy + modern)
        await self.context.route('**/*', fixture)
        resumes = await get_my_resumes(self.page)
        self.assertEqual([r['title'] for r in resumes], ['Fixture Fullstack', 'Fixture Golang'])
        self.assertEqual(len(resumes), 2)


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_durable_dispatch_record_prevents_request(self):
        from unittest.mock import AsyncMock, Mock
        guard = SubmissionGuard(CONTENT, Mock(side_effect=OSError('fixture disk full')))
        guard.commit = True
        body = json.dumps({'vacancy_id': CONTENT['vacancy_id'], 'resume_hash': CONTENT['resume_id'],
                           'letter': CONTENT['cover_letter']}).encode()
        route = Mock(request=Mock(url='https://hh.ru/applicant/vacancy_response/popup', method='POST',
                                 post_data_buffer=body, headers={'content-type': 'application/json'}),
                     abort=AsyncMock(), fallback=AsyncMock())
        await guard.handle(route)
        route.fallback.assert_not_awaited()
        route.abort.assert_awaited_once()
        self.assertFalse(guard.dispatched)

    async def test_unrelated_domain_http_and_duplicate_dispatch_are_blocked(self):
        from unittest.mock import AsyncMock, Mock
        guard = SubmissionGuard(CONTENT, Mock())
        guard.commit = True
        body = json.dumps({'vacancy_id': CONTENT['vacancy_id'], 'resume_hash': CONTENT['resume_id'],
                           'letter': CONTENT['cover_letter']}).encode()
        def route(url, method='POST'):
            return Mock(request=Mock(url=url, method=method, post_data_buffer=body,
                                     headers={'content-type': 'application/json'}),
                        abort=AsyncMock(), fallback=AsyncMock())
        for url in ['https://hh.ru.attacker.invalid/collect', 'http://hh.ru/applicant/vacancy_response/popup']:
            blocked = route(url)
            await guard.handle(blocked)
            blocked.abort.assert_awaited_once()
            blocked.fallback.assert_not_awaited()
        allowed = route('https://hh.ru/applicant/vacancy_response/popup')
        await guard.handle(allowed)
        allowed.fallback.assert_awaited_once()
        duplicate = route('https://hh.ru/applicant/vacancy_response/popup')
        await guard.handle(duplicate)
        duplicate.abort.assert_awaited_once()
        guard.on_dispatch.assert_called_once()


if __name__ == '__main__':
    unittest.main()
