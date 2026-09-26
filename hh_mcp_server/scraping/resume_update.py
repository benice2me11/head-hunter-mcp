"""Structured resume reads and guarded UI writes using researched HH controls."""
from __future__ import annotations

import asyncio

from playwright.async_api import Page

from hh_mcp_server.constants import BASE_URL
from hh_mcp_server.resume_update_guard import (
    ResumeMutationCaptureGuard,
    ResumeUpdateGuard,
    build_captured_experience_mutation,
    build_expected_mutation,
)
from hh_mcp_server.resume_updates import SKILL_LEVELS, validate_resume_id
from hh_mcp_server.utils.auth import ensure_authenticated


async def read_owned_resume_states(page: Page, owned: list[dict]) -> list[dict]:
    """Read structured editor state for an already verified list of owned resumes."""
    result = []
    for item in owned:
        resume_id = item.get("id")
        validate_resume_id(resume_id)
        response = await page.context.request.get(
            f"{BASE_URL}/applicant/resume",
            params={"resume": resume_id},
        )
        if response.status != 200:
            raise ValueError(
                f"HH structured resume read failed with HTTP {response.status}"
            )
        data = await response.json()
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("resume"), dict)
            or not data["resume"]
        ):
            raise ValueError("HH structured resume response has an unsupported shape")
        result.append(
            {
                "id": resume_id,
                "title": item.get("title", ""),
                "resume": data["resume"],
            }
        )
    return result


def operation_ui(resume_id: str, operation: dict) -> dict:
    validate_resume_id(resume_id)
    kind = operation.get("kind")
    if kind == "headline":
        return {
            "url": f"{BASE_URL}/resume/edit/{resume_id}/position",
            "field": "[data-qa='resume-edit-title-suggest']",
            "save": "[data-qa='resume-partial-edit-save']",
        }
    if kind == "about":
        return {
            "url": f"{BASE_URL}/resume/edit/{resume_id}/about",
            "field": "[data-qa='resume-editor-about']",
            "save": "[data-qa='resume-partial-edit-save']",
        }
    if kind == "experience_description":
        entry_id = operation.get("entry_id")
        if not isinstance(entry_id, int) or entry_id <= 0:
            raise ValueError("Invalid experience entry ID")
        return {
            "url": (
                f"{BASE_URL}/profile/edit/experience/{entry_id}"
                f"?resumeFrom={resume_id}&hhtmFrom=resume"
            ),
            "field": "[data-qa='resume-editor-experience-description-input']",
            "save": "[data-qa='profile-layout-save-button']",
        }
    if kind == "skills":
        return {
            "url": f"{BASE_URL}/resume/edit/{resume_id}/keySkills",
            "field": "[data-qa='resume-editor-skills-input']",
            "save": "[data-qa='resume-partial-edit-save']",
        }
    if kind == "skill_level":
        return {
            "url": f"{BASE_URL}/resume/edit/{resume_id}/skillsLevels",
            "field": "[data-qa='skill']",
            "save": "[data-qa='resume-partial-edit-save']",
        }
    raise ValueError(f"Unsupported resume update operation: {kind}")


async def _require_visible(page: Page, selector: str):
    locator = page.locator(selector).first
    if not await locator.is_visible():
        raise ValueError(f"Required HH resume editor control is unavailable: {selector}")
    return locator


async def _prepare_text_operation(page: Page, spec: dict, operation: dict) -> None:
    field = await _require_visible(page, spec["field"])
    current = await field.input_value()
    if current != operation["before"]:
        raise ValueError("Resume field changed before the approved update was dispatched")
    await field.fill(operation["after"])


async def _current_skill_names(page: Page) -> list[str]:
    root = await _require_visible(page, "[data-qa='resume-editor-skills-input']")
    chips = root.locator("[data-qa^='chips-trigger-chip-']")
    names = []
    for index in range(await chips.count()):
        qa = await chips.nth(index).get_attribute("data-qa") or ""
        prefix = "chips-trigger-chip-"
        if not qa.startswith(prefix) or len(qa) == len(prefix):
            raise ValueError("HH skill chip is missing a stable data-qa name")
        names.append(qa[len(prefix):])
    return names


async def _prepare_skills(page: Page, operation: dict) -> None:
    before = await _current_skill_names(page)
    if before != operation["before"]:
        raise ValueError("Resume skills changed before dispatch")

    root = await _require_visible(page, "[data-qa='resume-editor-skills-input']")
    while True:
        delete = root.locator("[data-qa='chip-delete-action']").first
        if not await delete.count():
            break
        if not await delete.is_visible():
            raise ValueError("HH skill delete control is unavailable")
        await delete.click()
        await page.wait_for_timeout(50)

    input_box = root.locator("[data-qa='chips-trigger-input']").first
    if not await input_box.is_visible():
        raise ValueError("HH skill input control is unavailable")
    for name in operation["after"]:
        await input_box.fill(name)
        await input_box.press("Enter")
        await page.wait_for_timeout(100)

    after = await _current_skill_names(page)
    if after != operation["after"]:
        raise ValueError("HH skill editor did not produce the exact approved skill list")


async def _prepare_skill_level(page: Page, operation: dict) -> None:
    cards = page.locator("[data-qa='skill']")
    matches = []
    for index in range(await cards.count()):
        card = cards.nth(index)
        name = card.locator("[data-qa='skillName']").first
        if await name.count() and (await name.inner_text()).strip() == operation["name"]:
            matches.append(card)
    if len(matches) != 1:
        raise ValueError("Approved skill level entry is missing or ambiguous in HH UI")
    rank = SKILL_LEVELS[operation["after"]]["level_rank"]
    level = matches[0].locator(f"[data-qa='skill-level-{rank}']").first
    if not await level.is_visible():
        raise ValueError("Approved skill level control is unavailable")
    await level.click()


async def _apply_captured_experience_operation(
    page: Page,
    resume_id: str,
    operation: dict,
    base_snapshot: dict,
    on_dispatch,
) -> dict:
    baseline_operation = {**operation, "after": operation["before"]}
    baseline = build_expected_mutation(
        resume_id, baseline_operation, base_snapshot
    )
    guard = ResumeMutationCaptureGuard(baseline)
    context = page.context
    await context.route("**/*", guard.handle)
    page.on("response", guard.observe_response)
    try:
        await ensure_authenticated(page)
        spec = operation_ui(resume_id, operation)
        await page.goto(spec["url"], wait_until="domcontentloaded")
        await page.wait_for_selector(spec["field"])
        field = await _require_visible(page, spec["field"])
        if await field.input_value() != operation["before"]:
            return {
                "status": "blocked",
                "reason": "Resume field changed before the approved update was dispatched",
                "dispatched": False,
            }

        save = await _require_visible(page, spec["save"])
        try:
            await save.click(timeout=5000)
        except BaseException:
            # An aborted capture request can make the UI save promise reject.
            # The capture event, not the click result, decides whether it is safe.
            pass
        try:
            await asyncio.wait_for(guard.capture_event.wait(), timeout=5)
        except TimeoutError:
            return {
                "status": "blocked",
                "reason": "HH did not produce the researched experience save request",
                "dispatched": False,
            }
        if guard.payload is None:
            return {
                "status": "blocked",
                "reason": guard.error or "HH experience baseline payload could not be verified",
                "dispatched": False,
            }

        expected = build_captured_experience_mutation(
            resume_id, operation, guard.payload
        )
        commit_guard = guard.arm(expected, on_dispatch)
        try:
            status = await page.evaluate(
                """async ({url, method, payload}) => {
                    const response = await fetch(url, {
                        method,
                        credentials: 'same-origin',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-Requested-With': 'XMLHttpRequest',
                        },
                        body: JSON.stringify(payload),
                    });
                    return response.status;
                }""",
                {
                    "url": f"https://{expected.host}{expected.path}",
                    "method": expected.method,
                    "payload": expected.payload,
                },
            )
        except BaseException:
            if not commit_guard.dispatched:
                return {
                    "status": "blocked",
                    "reason": commit_guard.error or "Exact approved experience request was blocked",
                    "dispatched": False,
                }
            raise

        if not commit_guard.dispatched:
            return {
                "status": "blocked",
                "reason": commit_guard.error or "Exact approved experience request was not dispatched",
                "dispatched": False,
            }
        if status >= 400:
            return {
                "status": "failed",
                "reason": f"HH returned HTTP {status}",
                "dispatched": True,
                "http_status": status,
            }
        return {
            "status": "dispatched",
            "dispatched": True,
            "http_status": status,
        }
    finally:
        await page.close()
        if page.is_closed():
            await context.unroute("**/*", guard.handle)


async def apply_resume_update_operation(
    page: Page,
    resume_id: str,
    operation: dict,
    base_snapshot: dict,
    on_dispatch,
) -> dict:
    """Generate one UI save and let only its exact approved network mutation through."""
    if operation.get("kind") == "experience_description":
        return await _apply_captured_experience_operation(
            page, resume_id, operation, base_snapshot, on_dispatch
        )

    expected = build_expected_mutation(resume_id, operation, base_snapshot)
    guard = ResumeUpdateGuard(expected, on_dispatch)
    context = page.context
    await context.route("**/*", guard.handle)
    page.on("response", guard.observe_response)
    try:
        await ensure_authenticated(page)
        spec = operation_ui(resume_id, operation)
        await page.goto(spec["url"], wait_until="domcontentloaded")
        await page.wait_for_selector(spec["field"])

        kind = operation["kind"]
        if kind in {"headline", "about", "experience_description"}:
            await _prepare_text_operation(page, spec, operation)
        elif kind == "skills":
            await _prepare_skills(page, operation)
        elif kind == "skill_level":
            await _prepare_skill_level(page, operation)
        else:
            raise ValueError(f"Unsupported resume update operation: {kind}")

        save = await _require_visible(page, spec["save"])
        try:
            await save.click(timeout=5000)
        except BaseException:
            if not guard.dispatched:
                return {
                    "status": "blocked",
                    "reason": guard.error or "HH did not produce an approved save request",
                    "dispatched": False,
                }
            raise

        try:
            await asyncio.wait_for(guard.dispatch_event.wait(), timeout=5)
        except TimeoutError:
            return {
                "status": "blocked",
                "reason": guard.error or "No exact approved HH mutation was produced",
                "dispatched": False,
            }

        try:
            await asyncio.wait_for(guard.response_event.wait(), timeout=8)
        except TimeoutError:
            return {
                "status": "unverified",
                "reason": "Approved mutation was dispatched but no response was observed",
                "dispatched": True,
            }

        if guard.response_status is not None and guard.response_status >= 400:
            return {
                "status": "failed",
                "reason": f"HH returned HTTP {guard.response_status}",
                "dispatched": True,
                "http_status": guard.response_status,
            }
        return {
            "status": "dispatched",
            "dispatched": True,
            "http_status": guard.response_status,
        }
    finally:
        await page.close()
        if page.is_closed():
            await context.unroute("**/*", guard.handle)
