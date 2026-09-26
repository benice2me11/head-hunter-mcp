"""MCP tools for reviewed, two-phase HH resume updates."""
from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any
import uuid

from fastmcp import FastMCP

from hh_mcp_server.constants import TOOL_TIMEOUT_SECONDS
from hh_mcp_server.drivers.browser import get_page
from hh_mcp_server.resume_update_guard import ReadOnlyGuard
from hh_mcp_server.resume_update_ledger import ResumeUpdateJournal, resume_update_context
from hh_mcp_server.resume_updates import (
    build_resume_update_plan,
    build_resume_update_snapshot,
    create_resume_update_draft,
    expected_prefix_snapshots,
    load_confirmed_resume_update_draft,
    save_resume_update_draft,
    validate_resume_id,
)
from hh_mcp_server.scraping.resume import get_my_resumes
from hh_mcp_server.scraping.resume_update import (
    apply_resume_update_operation,
    read_owned_resume_states,
)
from hh_mcp_server.utils.auth import ensure_authenticated


@asynccontextmanager
async def read_only_page():
    page = await get_page()
    guard = ReadOnlyGuard()
    context = page.context
    await context.route("**/*", guard.handle)
    try:
        yield page
    finally:
        await page.close()
        if page.is_closed():
            await context.unroute("**/*", guard.handle)


async def _read_all_owned_states() -> list[dict]:
    async with read_only_page() as page:
        await ensure_authenticated(page)
        owned = await get_my_resumes(page)
        return await read_owned_resume_states(page, owned)


def _require_owned(resume_id: str, states: list[dict]) -> None:
    matches = [item for item in states if item.get("id") == resume_id]
    if len(matches) != 1:
        raise ValueError(
            "The selected resume was not uniquely found in your own resumes"
        )


async def _current_snapshot(resume_id: str) -> dict:
    states = await _read_all_owned_states()
    _require_owned(resume_id, states)
    return build_resume_update_snapshot(states)


def _operation_key(operation: dict) -> str:
    if operation["kind"] == "experience_description":
        return f"experience:{operation['entry_id']}"
    if operation["kind"] == "skill_level":
        return f"skill_level:{operation['skill_id']}"
    return operation["kind"]


def _public_draft(draft: dict) -> dict:
    content = draft["content"]
    return {
        "status": "prepared",
        "draft_id": draft["draft_id"],
        "digest": draft["digest"],
        "resume_id": content["resume_id"],
        "snapshot_sha256": content["base_snapshot"]["sha256"],
        "preview": deepcopy(content["preview"]),
        "operations": deepcopy(content["operations"]),
        "expires_in_hours": 24,
    }


async def _reconcile(draft: dict, journal: ResumeUpdateJournal) -> dict:
    content = draft["content"]
    current = await _current_snapshot(content["resume_id"])
    prefixes = expected_prefix_snapshots(
        content["base_snapshot"], content["operations"]
    )
    matching = [index for index, snapshot in enumerate(prefixes) if snapshot == current]
    if len(matching) != 1:
        result = {
            "status": "unverified",
            "write_performed": False,
            "reason": (
                "Current HH state does not match any approved prefix; "
                "no request was retried"
            ),
            "observed_snapshot_sha256": current["sha256"],
        }
        draft["status"] = "unverified"
    else:
        count = matching[0]
        confirmed = [
            _operation_key(operation) for operation in content["operations"][:count]
        ]
        remaining = [
            _operation_key(operation) for operation in content["operations"][count:]
        ]
        result = {
            "status": "reconciled",
            "outcome": (
                "success"
                if count == len(content["operations"])
                else "partial"
                if count
                else "no_approved_change_observed"
            ),
            "confirmed_operations": confirmed,
            "remaining_operations": remaining,
            "write_performed": False,
        }
        if remaining:
            result["reason"] = (
                "No retry was sent; prepare a fresh draft for any remaining change"
            )
            draft["status"] = "reconciled"
        else:
            draft["status"] = "success"
        draft["completed_operations"] = confirmed
    draft["last_result"] = result
    save_resume_update_draft(draft)
    journal.append("reconciliation", resume_update_context(draft), data=result)
    return result


async def _uncertain_result(
    *,
    draft: dict,
    completed: list[str],
    operation_key: str,
    reason: str,
    error_type: str | None = None,
) -> dict:
    observation = None
    try:
        observation = await _current_snapshot(draft["content"]["resume_id"])
    except BaseException as error:
        observation_error = type(error).__name__
    else:
        observation_error = None
    status = "partial_success" if completed else "unverified"
    result = {
        "status": status,
        "reason": reason,
        "confirmed_operations": list(completed),
        "unverified_operations": [operation_key],
        "automatic_retry": False,
    }
    if error_type:
        result["error_type"] = error_type
    if observation is not None:
        result["observed_snapshot_sha256"] = observation["sha256"]
    if observation_error:
        result["readback_error_type"] = observation_error
    draft["status"] = status
    draft["completed_operations"] = list(completed)
    draft["last_result"] = result
    save_resume_update_draft(draft)
    return result


def register_resume_update_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Prepare Resume Update",
        annotations={"readOnlyHint": True, "openWorldHint": True},
    )
    async def prepare_resume_update(
        resume_id: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """Prepare an exact typed resume update without any HH mutation."""
        validate_resume_id(resume_id)
        states = await _read_all_owned_states()
        _require_owned(resume_id, states)
        plan = build_resume_update_plan(resume_id, changes, states)
        if plan["status"] == "no_changes":
            return {
                "status": "no_changes",
                "resume_id": resume_id,
                "snapshot_sha256": plan["base_snapshot"]["sha256"],
                "preview": {},
                "operations": [],
            }

        draft = create_resume_update_draft(plan)
        with ResumeUpdateJournal() as journal:
            journal.append(
                "draft_prepared",
                resume_update_context(draft),
                data={
                    "snapshot_sha256": draft["content"]["base_snapshot"]["sha256"],
                    "operations": [
                        _operation_key(item) for item in draft["content"]["operations"]
                    ],
                },
            )
        return _public_draft(draft)

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Apply Confirmed Resume Update",
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    )
    async def apply_resume_update(
        draft_id: str,
        approved_digest: str,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Apply only an unchanged reviewed draft; uncertain writes are never retried."""
        draft = load_confirmed_resume_update_draft(
            draft_id, approved_digest, confirmed, allow_reconciliation=True
        )
        with ResumeUpdateJournal() as journal:
            draft = load_confirmed_resume_update_draft(
                draft_id, approved_digest, confirmed, allow_reconciliation=True
            )
            if draft["status"] != "prepared":
                return await _reconcile(draft, journal)

            content = draft["content"]
            context = resume_update_context(draft)
            try:
                current = await _current_snapshot(content["resume_id"])
            except BaseException as error:
                result = {
                    "status": "blocked",
                    "reason": "Current owned resume state could not be verified",
                    "error_type": type(error).__name__,
                }
                draft["status"] = "blocked"
                draft["last_result"] = result
                save_resume_update_draft(draft)
                journal.append("attempt_result", context, data=result)
                return result
            if current != content["base_snapshot"]:
                result = {
                    "status": "blocked",
                    "reason": (
                        "Resume changed after preview; prepare and approve a new draft"
                    ),
                }
                draft["status"] = "stale"
                draft["last_result"] = result
                save_resume_update_draft(draft)
                journal.append("attempt_result", context, data=result)
                return result

            attempt_id = uuid.uuid4().hex
            journal.append("attempt_started", context, attempt_id=attempt_id)
            operations = content["operations"]
            prefixes = expected_prefix_snapshots(
                content["base_snapshot"], operations
            )
            completed: list[str] = []

            for index, operation in enumerate(operations):
                key = _operation_key(operation)
                operation_id = f"{index + 1}:{key}"

                try:
                    before = await _current_snapshot(content["resume_id"])
                except BaseException as error:
                    result = {
                        "status": "partial_success" if completed else "blocked",
                        "reason": "Resume state could not be re-verified before the next write",
                        "confirmed_operations": list(completed),
                        "error_type": type(error).__name__,
                    }
                    draft["status"] = result["status"]
                    draft["last_result"] = result
                    save_resume_update_draft(draft)
                    journal.append(
                        "attempt_result", context, attempt_id=attempt_id, data=result
                    )
                    return result
                if before != prefixes[index]:
                    result = {
                        "status": "partial_success" if completed else "blocked",
                        "reason": "Resume changed between approved sub-operations",
                        "confirmed_operations": list(completed),
                    }
                    draft["status"] = result["status"]
                    draft["last_result"] = result
                    save_resume_update_draft(draft)
                    journal.append(
                        "attempt_result", context, attempt_id=attempt_id, data=result
                    )
                    return result

                journal.append(
                    "suboperation_started",
                    context,
                    attempt_id=attempt_id,
                    operation_id=operation_id,
                    data={"operation": key},
                )
                dispatch_recorded = False

                def dispatched() -> None:
                    nonlocal dispatch_recorded
                    journal.append(
                        "dispatch_started",
                        context,
                        attempt_id=attempt_id,
                        operation_id=operation_id,
                        data={"operation": key},
                    )
                    draft["status"] = "dispatched_unverified"
                    draft["active_operation"] = operation_id
                    save_resume_update_draft(draft)
                    dispatch_recorded = True

                try:
                    page = await get_page()
                    outcome = await apply_resume_update_operation(
                        page,
                        content["resume_id"],
                        deepcopy(operation),
                        prefixes[index],
                        dispatched,
                    )
                except BaseException as error:
                    if dispatch_recorded:
                        result = await _uncertain_result(
                            draft=draft,
                            completed=completed,
                            operation_key=key,
                            reason=(
                                "Resume write may have been dispatched; "
                                "the approved request was not retried"
                            ),
                            error_type=type(error).__name__,
                        )
                    else:
                        result = {
                            "status": "partial_success" if completed else "blocked",
                            "reason": (
                                "Resume update stopped before the next approved "
                                "mutation was dispatched"
                            ),
                            "confirmed_operations": list(completed),
                            "error_type": type(error).__name__,
                        }
                        draft["status"] = result["status"]
                        draft["last_result"] = result
                        save_resume_update_draft(draft)
                    journal.append(
                        "suboperation_result",
                        context,
                        attempt_id=attempt_id,
                        operation_id=operation_id,
                        data=result,
                    )
                    journal.append(
                        "attempt_result", context, attempt_id=attempt_id, data=result
                    )
                    return result

                if outcome.get("status") != "dispatched":
                    if dispatch_recorded or outcome.get("dispatched"):
                        if outcome.get("status") == "failed":
                            result = {
                                "status": "partial_success" if completed else "failed",
                                "reason": outcome.get(
                                    "reason", "HH rejected the approved update"
                                ),
                                "confirmed_operations": list(completed),
                            }
                            draft["status"] = result["status"]
                            draft["last_result"] = result
                            save_resume_update_draft(draft)
                        else:
                            result = await _uncertain_result(
                                draft=draft,
                                completed=completed,
                                operation_key=key,
                                reason=outcome.get(
                                    "reason",
                                    "Approved write was dispatched but could not be verified",
                                ),
                            )
                    else:
                        result = {
                            "status": "partial_success" if completed else "blocked",
                            "reason": outcome.get(
                                "reason", "No approved mutation was dispatched"
                            ),
                            "confirmed_operations": list(completed),
                        }
                        draft["status"] = result["status"]
                        draft["last_result"] = result
                        save_resume_update_draft(draft)
                    journal.append(
                        "suboperation_result",
                        context,
                        attempt_id=attempt_id,
                        operation_id=operation_id,
                        data=result,
                    )
                    journal.append(
                        "attempt_result", context, attempt_id=attempt_id, data=result
                    )
                    return result

                if not dispatch_recorded:
                    result = {
                        "status": "blocked",
                        "reason": "HH mutation was not preceded by a durable dispatch record",
                        "confirmed_operations": list(completed),
                    }
                    draft["status"] = "blocked"
                    draft["last_result"] = result
                    save_resume_update_draft(draft)
                    journal.append(
                        "attempt_result", context, attempt_id=attempt_id, data=result
                    )
                    return result

                try:
                    observed = await _current_snapshot(content["resume_id"])
                except BaseException as error:
                    result = await _uncertain_result(
                        draft=draft,
                        completed=completed,
                        operation_key=key,
                        reason="Approved write was dispatched but read-back failed",
                        error_type=type(error).__name__,
                    )
                    journal.append(
                        "suboperation_result",
                        context,
                        attempt_id=attempt_id,
                        operation_id=operation_id,
                        data=result,
                    )
                    journal.append(
                        "attempt_result", context, attempt_id=attempt_id, data=result
                    )
                    return result

                expected = prefixes[index + 1]
                if observed != expected:
                    if observed == prefixes[index]:
                        result = {
                            "status": "partial_success" if completed else "failed",
                            "reason": (
                                "HH responded to the approved request but the approved "
                                "change was not present on read-back"
                            ),
                            "confirmed_operations": list(completed),
                        }
                        draft["status"] = result["status"]
                        draft["last_result"] = result
                        save_resume_update_draft(draft)
                    else:
                        result = await _uncertain_result(
                            draft=draft,
                            completed=completed,
                            operation_key=key,
                            reason=(
                                "Read-back differs from both the prior and exact "
                                "approved material state"
                            ),
                        )
                    journal.append(
                        "suboperation_result",
                        context,
                        attempt_id=attempt_id,
                        operation_id=operation_id,
                        data=result,
                    )
                    journal.append(
                        "attempt_result", context, attempt_id=attempt_id, data=result
                    )
                    return result

                completed.append(key)
                draft["completed_operations"] = list(completed)
                draft["status"] = "in_progress"
                draft.pop("active_operation", None)
                save_resume_update_draft(draft)
                journal.append(
                    "suboperation_result",
                    context,
                    attempt_id=attempt_id,
                    operation_id=operation_id,
                    data={"status": "success", "operation": key},
                )

            result = {
                "status": "success",
                "confirmed_operations": list(completed),
                "snapshot_sha256": prefixes[-1]["sha256"],
                "verified_by": "fresh structured HH read-back of all owned material resumes",
            }
            draft["status"] = "success"
            draft["last_result"] = result
            save_resume_update_draft(draft)
            journal.append("attempt_result", context, attempt_id=attempt_id, data=result)
            return result
