"""UI application submission with payload validation and positive result checks."""
import asyncio
from playwright.async_api import Page, TimeoutError as BrowserTimeout
from hh_mcp_server.constants import BASE_URL
from hh_mcp_server.scraping import selectors as S
from hh_mcp_server.scraping.responses import get_my_responses
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
    picker = page.locator('[role="button"]:has([data-qa="resume-title"])')
    if await picker.count() == 1 and await picker.is_visible():
        await picker.click(force=True)
        option = page.locator(f'[data-qa="magritte-select-option-{resume_id}"]')
        if await option.count() == 1:
            await option.click(force=True)
            selected = page.locator('[role="button"]:has([data-qa="resume-title"]) [data-qa="resume-title"]')
            if await selected.count() == 1:
                return normalized_text(await selected.inner_text()) == normalized_text(resume_title)
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
    groups = {}
    payload_answers = {}
    inactive_answer_names = set()
    controls = await page.locator('textarea[name^="task_"], input[name^="task_"], select[name^="task_"]').all()
    for control in controls:
        name = await control.get_attribute('name')
        if not name:
            continue
        tag = await control.evaluate('(e) => e.tagName.toLowerCase()')
        kind = await control.get_attribute('type') or tag
        if kind == 'hidden':
            continue
        active = await control.is_visible()
        option_label = await control.evaluate(
            "(e) => e.labels?.[0]?.innerText || e.getAttribute('aria-label') || e.value || e.name"
        )
        question_label = await control.evaluate(
            "(e) => e.closest('[data-qa=task-body]')?.querySelector('legend, h1, h2, h3, h4, [data-qa*=title]')?.innerText || "
            "e.closest('[data-qa=task-body]')?.innerText || e.getAttribute('aria-label') || e.name"
        )
        group = groups.setdefault(
            name,
            {
                'name': name,
                'type': kind,
                'question': (question_label or name)[:1000],
                'options': [],
                'control_semantics_warning': None,
                'active': active,
            },
        )
        question_text = (question_label or '').casefold()
        if kind == 'radio' and any(
            marker in question_text
            for marker in ('можно выбрать несколько', 'выберите несколько', 'несколько вариантов')
        ):
            group['control_semantics_warning'] = (
                'Question text suggests multiple selection, but HH currently renders '
                'same-name radio controls; only one option can be selected in this DOM.'
            )
        if kind in {'radio', 'checkbox'}:
            label = (option_label or '').strip()
            if label and label not in group['options']:
                group['options'].append(label[:500])

        if name not in approved_answers:
            continue
        if not active and kind not in {'radio', 'checkbox'}:
            # HH uses hidden textareas for conditional "custom answer" fields.
            # Never force-fill an inactive control: selecting its controller
            # must make it visible first, otherwise the approved payload is
            # internally inconsistent with the current form state.
            if name in approved_answers:
                inactive_answer_names.add(name)
            continue
        answer = approved_answers[name]
        if kind == 'radio':
            if normalized_text(option_label or '') == normalized_text(str(answer)):
                await control.check(force=True)
                if not await control.is_checked():
                    raise ValueError('Approved radio answer could not be verified.')
                payload_answers[name] = await control.input_value()
        elif kind == 'checkbox':
            wanted = answer if isinstance(answer, list) else [answer]
            should_check = any(
                normalized_text(option_label or '') == normalized_text(str(item))
                for item in wanted
            )
            if should_check:
                await control.check(force=True)
                if not await control.is_checked():
                    raise ValueError('Approved checkbox answer could not be verified.')
                payload_answers.setdefault(name, []).append(await control.input_value())
        elif tag == 'select':
            try:
                await control.select_option(label=str(answer))
            except Exception:
                await control.select_option(str(answer))
            selected = await control.locator('option:checked').inner_text()
            if normalized_text(selected) != normalized_text(str(answer)):
                raise ValueError('Approved select answer could not be verified.')
            payload_answers[name] = await control.input_value()
        else:
            await control.fill(str(answer), force=True)
            if await control.input_value() != str(answer):
                raise ValueError('Approved questionnaire answer could not be verified.')
            payload_answers[name] = str(answer)

    questions = list(groups.values())
    present_names = set(groups)
    incomplete = False
    for question in questions:
        name = question['name']
        if name not in approved_answers:
            incomplete = True
            continue
        if question['type'] not in {'radio', 'checkbox'} and not question.get('active', True):
            # Conditional controls (for example HH's custom-answer textarea)
            # are present in the DOM even when their controller keeps them
            # inactive. They must not block a draft that deliberately selected
            # another option, and they must not be included in the payload.
            continue
        if question['type'] == 'radio':
            if await page.locator(f'input[name="{name}"]:checked').count() != 1:
                incomplete = True
        elif question['type'] == 'checkbox':
            if await page.locator(f'input[name="{name}"]:checked').count() == 0:
                incomplete = True
    extra = set(approved_answers) - present_names
    return questions, incomplete or bool(extra), payload_answers, inactive_answer_names


async def inspect_application_form(page: Page, vacancy_id: str) -> dict:
    """Open the response form under a network guard and describe it without dispatching."""
    guard = SubmissionGuard({}, lambda: None)
    context = page.context
    await context.route('**/*', guard.handle)
    url = f"{BASE_URL}/vacancy/{vacancy_id}"
    try:
        await page.goto(url, wait_until='domcontentloaded')
        if await page.locator(S.ALREADY_APPLIED).count():
            return {'status': 'already_applied', 'url': url}
        button = await visible(page, [S.APPLY_BUTTON])
        if button is None:
            return {'status': 'blocked', 'reason': 'Application control not found.', 'url': url}
        await button.click()
        try:
            await page.wait_for_selector(S.SUBMIT_BUTTON, timeout=5000)
        except BrowserTimeout:
            return {'status': 'blocked', 'reason': guard.error or 'No verifiable application form.', 'url': url}
        questions, _, _, _ = await questions_and_fill(page, {})
        resume_candidates = []
        for candidate in await page.locator('[data-qa*="resume"]').all():
            if not await candidate.is_visible():
                continue
            resume_candidates.append({
                'data_qa': await candidate.get_attribute('data-qa'),
                'tag': await candidate.evaluate('(e) => e.tagName.toLowerCase()'),
                'text': (await candidate.inner_text())[:1000],
            })
        return {
            'status': 'form_inspected',
            'url': url,
            'questions': questions,
            'question_count': len(questions),
            'requires_questions': bool(questions),
            'resume_visibility_warning': bool(await visible(page, [S.RESUME_VISIBILITY_WARNING])),
            'relocation_warning': bool(await visible(page, ['[data-qa="relocation-warning-confirm"]'])),
            'cover_letter_available': bool(await visible(page, [S.COVER_LETTER_INPUT, S.COVER_LETTER_TOGGLE])),
            'resume_candidates': resume_candidates,
            'dispatch_attempted': guard.dispatched,
        }
    finally:
        await page.close()
        if page.is_closed():
            await context.unroute('**/*', guard.handle)


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
    stage = 'open_vacancy'
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
        stage = 'open_application_form'
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
        stage = 'inspect_blockers'
        warning = await visible(page, [S.RESUME_VISIBILITY_WARNING])
        if warning:
            return {'status': 'blocked', 'reason': 'HH requires a resume visibility change; obtain separate privacy approval.',
                    'blocker': 'resume_visibility_review_required', 'details': await warning.inner_text()}
        if await visible(page, ['[data-qa="relocation-warning-confirm"]']):
            return {'status': 'blocked', 'reason': 'Relocation warning requires separate user review.'}
        stage = 'fill_questions'
        questions, incomplete, payload_answers, inactive_answer_names = await questions_and_fill(
            page, content.get('question_answers') or {}
        )
        if incomplete:
            return {'status': 'questions_required', 'questions': questions,
                    'reason': 'Review the questions and prepare a new draft with approved answers.'}
        stage = 'select_resume'
        if not await select_resume(page, content['resume_id'], content['resume_title']):
            return {'status': 'blocked', 'reason': 'The approved resume could not be selected and verified.'}
        stage = 'fill_cover_letter'
        if not await fill_letter(page, content['cover_letter']):
            return {'status': 'blocked', 'reason': 'The approved cover letter could not be filled and verified.'}
        submit = await visible(page, [S.SUBMIT_BUTTON])
        if submit is None or not await submit.is_enabled():
            return {'status': 'blocked', 'reason': 'Submission is unavailable or the form is incomplete.'}
        stage = 'pre_dispatch_snapshot'
        if 'vacancy_snapshot' in content and '/applicant/vacancy_response' not in page.url:
            current = vacancy_snapshot(await extract_vacancy_page(page, content['vacancy_id']))
            if current != content['vacancy_snapshot']:
                return {'status': 'blocked', 'reason': 'Vacancy terms changed while filling the form; review a new draft.'}
        stage = 'submit'
        guard.question_payload_answers = payload_answers
        guard.optional_empty_question_fields = inactive_answer_names
        guard.commit = True
        await submit.click()
        for _ in range(50):
            if guard.error or guard.response_status is not None:
                break
            await asyncio.sleep(0.1)
        stage = 'verify_dispatch'
        if not guard.dispatched:
            # HH may insert another questionnaire/confirmation step instead of
            # dispatching immediately. Surface the new state instead of
            # collapsing it into an ambiguous generic blocker.
            next_questions, _, _, _ = await questions_and_fill(
                page, content.get('question_answers') or {}
            )
            question_shape = lambda values: [
                (item['name'], item['type'], tuple(item.get('options') or []))
                for item in values
            ]
            if next_questions and question_shape(next_questions) != question_shape(questions):
                return {
                    'status': 'additional_step_required',
                    'questions': next_questions,
                    'reason': 'HH showed another application step; review it before dispatch.',
                }
            return {'status': 'blocked', 'reason': guard.error or 'No approved application request was sent.'}
        if guard.response_status is None or not 200 <= guard.response_status < 300:
            return {'status': 'unverified', 'reason': 'Request was dispatched but success is unverified. Check history before any retry.'}
        # A successful HTTP response alone is insufficient: reload the vacancy.
        stage = 'verify_marker'
        await page.goto(url, wait_until='domcontentloaded')
        try:
            await page.wait_for_selector(S.ALREADY_APPLIED, timeout=8000)
        except BrowserTimeout:
            return {'status': 'unverified', 'reason': 'HH has not visibly confirmed the application. Check history; do not retry automatically.'}
        # Cross-check the canonical response history too. The vacancy marker is
        # already strong evidence, so a lagging negotiations list is reported
        # explicitly rather than turning a verified submission into uncertainty.
        history_match = None
        history_error = None
        try:
            history = await get_my_responses(page)
            history_match = next(
                (item for item in history.get('responses', []) if item.get('vacancy_id') == content['vacancy_id']),
                None,
            )
        except Exception as error:
            history_error = type(error).__name__
        verified_by = ['exact_outgoing_payload', 'application_marker_after_reload']
        if history_match:
            verified_by.append('responses_exact_vacancy_id')
        return {
            'status': 'success',
            'url': url,
            'verified_by': verified_by,
            'history_verification': 'confirmed' if history_match else 'pending_sync',
            'response': history_match,
            'history_error': history_error,
        }
    except Exception as error:
        return {'status': 'unverified' if guard.dispatched else 'blocked',
                'reason': 'Application interrupted; check history before retrying.' if guard.dispatched else 'Form interaction failed; no approved request was dispatched.',
                'error_type': type(error).__name__, 'error_detail': str(error), 'stage': stage}
    finally:
        page.remove_listener('response', guard.observe_response)
        # Stop delayed page scripts before removing the request guard. If closing
        # fails (including cancellation), retain the guard for this context.
        await page.close()
        if page.is_closed():
            await context.unroute('**/*', guard.handle)
