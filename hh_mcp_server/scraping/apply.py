"""UI application submission with payload validation and positive result checks."""
import asyncio
from playwright.async_api import Page, TimeoutError as BrowserTimeout
from hh_mcp_server.constants import BASE_URL
from hh_mcp_server.scraping import selectors as S
from hh_mcp_server.submission_guard import SubmissionGuard
from hh_mcp_server.scraping.vacancy_detail import extract_vacancy_page
from hh_mcp_server.snapshots import normalized_label, normalized_text, vacancy_snapshot


async def visible(page, selectors):
    for selector in selectors:
        for candidate in await page.locator(selector).all():
            if await candidate.is_visible():
                return candidate
    return None


async def select_resume(page, resume_id, resume_title):
    for selector in ['select[name="resume_hash"]', 'select[name="resume_id"]', S.RESUME_SELECT]:
        control = await visible(page, [selector])
        if control is None:
            continue
        if await control.evaluate('(e) => e.tagName') == 'SELECT':
            if await control.locator(f'option[value="{resume_id}"]').count() == 1:
                await control.select_option(resume_id)
                return await control.input_value() == resume_id
        else:
            await control.click()
            option = page.get_by_role('option', name=resume_title, exact=True)
            if await option.count() == 1:
                await option.click()
                return True  # Exact resume ID is also enforced on the outgoing request.
    radio = page.locator(f'input[type="radio"][value="{resume_id}"]')
    if await radio.count() == 1:
        await radio.check()
        return await radio.is_checked()
    current = page.locator('input[name="resume_hash"], input[name="resume_id"], input[name="resumeId"]')
    values = [await item.input_value() for item in await current.all()]
    if values:
        return all(value == resume_id for value in values)
    # The current HH dialog keeps the selected ID in React state. Accept only
    # one visible exact title; SubmissionGuard still checks the outgoing ID.
    titles = [item for item in await page.locator(S.SELECTED_RESUME_TITLE).all()
              if await item.is_visible()]
    return (len(titles) == 1
            and normalized_text(await titles[0].inner_text()) == normalized_text(resume_title))


async def questions_and_fill(page, approved_answers):
    questions = []
    controls = await page.locator('textarea[name^="task_"], input[name^="task_"], select[name^="task_"]').all()
    for control in controls:
        name = await control.get_attribute('name')
        tag = await control.evaluate('(e) => e.tagName.toLowerCase()')
        kind = await control.get_attribute('type') or tag
        label = await control.evaluate("(e) => e.labels?.[0]?.innerText || e.closest('[data-qa=task-body]')?.innerText || e.getAttribute('aria-label') || e.name")
        questions.append({'name': name, 'label': label[:1000], 'type': kind})
        if name not in approved_answers or tag != 'textarea':
            continue
        await control.fill(approved_answers[name])
        if await control.input_value() != approved_answers[name]:
            raise ValueError('Approved questionnaire answer could not be verified.')
    incomplete = any(q['name'] not in approved_answers or q['type'] != 'textarea' for q in questions)
    extra = set(approved_answers) - {q['name'] for q in questions}
    return questions, incomplete or bool(extra)


async def fill_letter(page, letter):
    selectors = [S.COVER_LETTER_INPUT, 'textarea[name="letter"]', 'textarea[name="cover_letter"]']
    field = await visible(page, selectors)
    if field is None:
        toggle = await visible(page, [S.COVER_LETTER_TOGGLE])
        if toggle:
            await toggle.click()
            try:
                await page.wait_for_selector(', '.join(selectors), state='visible', timeout=5000)
            except BrowserTimeout:
                return False
            field = await visible(page, selectors)
    if field is None:
        return letter == ''  # A nonempty approved letter must never be dropped.
    await field.fill(letter)
    return await field.input_value() == letter


async def apply_reviewed_draft(page: Page, content: dict, on_dispatch) -> dict:
    guard = SubmissionGuard(content, on_dispatch)
    context = page.context
    await context.route('**/*', guard.handle)
    page.on('response', guard.observe_response)
    url = f"{BASE_URL}/vacancy/{content['vacancy_id']}"
    try:
        await page.goto(url, wait_until='domcontentloaded')
        if await page.locator(S.ALREADY_APPLIED).count():
            return {'status': 'already_applied', 'url': url}
        if await page.locator(S.DETAIL_TITLE).count() != 1:
            return {'status': 'blocked', 'reason': 'Vacancy is unavailable or page structure changed.'}
        if normalized_label(await page.locator(S.DETAIL_TITLE).inner_text()) != normalized_label(content['vacancy_title']):
            return {'status': 'blocked', 'reason': 'Vacancy title changed since review; prepare a new draft.'}
        employer = page.locator(S.DETAIL_EMPLOYER)
        if await employer.count() != 1 or normalized_label(await employer.inner_text()) != normalized_label(content['employer']):
            return {'status': 'blocked', 'reason': 'Employer changed or could not be verified; prepare a new draft.'}
        if 'vacancy_snapshot' in content:
            current = vacancy_snapshot(await extract_vacancy_page(page, content['vacancy_id']))
            if current != content['vacancy_snapshot']:
                return {'status': 'blocked', 'reason': 'Vacancy terms materially changed; prepare and approve a new draft.'}
        cookie = await visible(page, ['[data-qa="cookies-policy-informer-accept"]'])
        if cookie:
            await cookie.click()
        button = await visible(page, [S.APPLY_BUTTON])
        if button is None:
            return {'status': 'blocked', 'reason': 'Application control not found.'}
        # Read-only network phase: a one-click application POST is blocked here.
        await button.click()
        try:
            await page.wait_for_selector(S.SUBMIT_BUTTON, timeout=5000)
        except BrowserTimeout:
            return {'status': 'blocked', 'reason': guard.error or 'No verifiable application form. Manual review required.'}
        if guard.error:
            return {'status': 'blocked', 'reason': guard.error}
        warning = await visible(page, [S.RESUME_VISIBILITY_WARNING])
        if warning:
            return {'status': 'blocked', 'reason': 'HH requires a resume visibility change; obtain separate privacy approval.',
                    'blocker': 'resume_visibility_review_required', 'details': await warning.inner_text()}
        if await visible(page, ['[data-qa="relocation-warning-confirm"]']):
            return {'status': 'blocked', 'reason': 'Relocation warning requires separate user review.'}
        questions, incomplete = await questions_and_fill(page, content.get('question_answers') or {})
        if incomplete:
            return {'status': 'questions_required', 'questions': questions,
                    'reason': 'Review the questions and prepare a new draft with approved answers.'}
        if not await select_resume(page, content['resume_id'], content['resume_title']):
            return {'status': 'blocked', 'reason': 'The approved resume could not be selected and verified.'}
        if not await fill_letter(page, content['cover_letter']):
            return {'status': 'blocked', 'reason': 'The approved cover letter could not be filled and verified.'}
        submit = await visible(page, [S.SUBMIT_BUTTON])
        if submit is None or not await submit.is_enabled():
            return {'status': 'blocked', 'reason': 'Submission is unavailable or the form is incomplete.'}
        if 'vacancy_snapshot' in content:
            current = vacancy_snapshot(await extract_vacancy_page(page, content['vacancy_id']))
            if current != content['vacancy_snapshot']:
                return {'status': 'blocked', 'reason': 'Vacancy terms changed while filling the form; review a new draft.'}
        guard.commit = True
        await submit.click()
        for _ in range(50):
            if guard.error or guard.response_status is not None:
                break
            await asyncio.sleep(0.1)
        if not guard.dispatched:
            return {'status': 'blocked', 'reason': guard.error or 'No approved application request was sent.'}
        if guard.response_status is None or not 200 <= guard.response_status < 300:
            return {'status': 'unverified', 'reason': 'Request was dispatched but success is unverified. Check history before any retry.'}
        # A successful HTTP response alone is insufficient: reload the vacancy.
        await page.goto(url, wait_until='domcontentloaded')
        try:
            await page.wait_for_selector(S.ALREADY_APPLIED, timeout=8000)
        except BrowserTimeout:
            return {'status': 'unverified', 'reason': 'HH has not visibly confirmed the application. Check history; do not retry automatically.'}
        return {'status': 'success', 'url': url, 'verified_by': 'exact outgoing payload and application marker after reload'}
    except Exception as error:
        return {'status': 'unverified' if guard.dispatched else 'blocked',
                'reason': 'Application interrupted; check history before retrying.' if guard.dispatched else 'Form interaction failed; no approved request was dispatched.',
                'error_type': type(error).__name__}
    finally:
        page.remove_listener('response', guard.observe_response)
        # Stop delayed page scripts before removing the request guard. If closing
        # fails (including cancellation), retain the guard for this context.
        await page.close()
        if page.is_closed():
            await context.unroute('**/*', guard.handle)
