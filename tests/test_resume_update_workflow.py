from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import inspect
import tempfile
from unittest.mock import AsyncMock, patch
import unittest

from playwright.async_api import async_playwright

from hh_mcp_server import resume_update_ledger, resume_updates
from hh_mcp_server.resume_update_ledger import ResumeUpdateJournal
from hh_mcp_server.tools.resume_update import register_resume_update_tools
from tests.test_resume_updates import RID1, RID2, owned


class ToolRegistry:
    def __init__(self):
        self.methods = {}

    def tool(self, *args, **kwargs):
        def register(function):
            self.methods[function.__name__] = function
            return function
        return register


def operation_key(operation):
    if operation["kind"] == "experience_description":
        return f"experience:{operation['entry_id']}"
    if operation["kind"] == "skill_level":
        return f"skill_level:{operation['skill_id']}"
    return operation["kind"]


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.states = owned()
        self.registry = ToolRegistry()
        self.writer_calls = []
        self.writer_mode = "success"
        self.read_fail = False

        async def read_states():
            if self.read_fail:
                raise TimeoutError("read failed")
            return deepcopy(self.states)

        async def writer(page, resume_id, operation, base_snapshot, on_dispatch):
            self.writer_calls.append(deepcopy(operation))
            if self.writer_mode == "timeout_before":
                raise TimeoutError("before dispatch")
            on_dispatch()
            if self.writer_mode == "timeout_after":
                raise TimeoutError("after dispatch")
            if self.writer_mode == "validation":
                return {
                    "status": "failed",
                    "reason": "validation_error",
                    "dispatched": True,
                }
            if self.writer_mode == "no_readback_change":
                return {"status": "dispatched", "dispatched": True, "http_status": 200}
            self._apply_to_live_state(operation)
            return {"status": "dispatched", "dispatched": True, "http_status": 200}

        self.patchers = [
            patch.object(
                resume_updates, "RESUME_UPDATE_DRAFT_DIR", self.root / "drafts"
            ),
            patch.object(resume_update_ledger, "PROFILE_DIR", self.root),
            patch(
                "hh_mcp_server.tools.resume_update._read_all_owned_states",
                side_effect=read_states,
            ),
            patch(
                "hh_mcp_server.tools.resume_update.apply_resume_update_operation",
                side_effect=writer,
            ),
            patch(
                "hh_mcp_server.tools.resume_update.get_page",
                new=AsyncMock(return_value=object()),
            ),
        ]
        for item in self.patchers:
            item.start()
        register_resume_update_tools(self.registry)

    async def asyncTearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.directory.cleanup()

    def _apply_to_live_state(self, operation):
        kind = operation["kind"]
        if kind == "headline":
            target = next(item for item in self.states if item["id"] == RID1)
            target["resume"]["title"] = [{"string": operation["after"]}]
        elif kind == "about":
            target = next(item for item in self.states if item["id"] == RID1)
            target["resume"]["skills"] = (
                [{"string": operation["after"]}] if operation["after"] else []
            )
        elif kind == "experience_description":
            for state in self.states:
                for row in state["resume"]["experience"]:
                    if row["id"] == operation["entry_id"]:
                        row["description"] = operation["after"]
        elif kind == "skills":
            target = next(item for item in self.states if item["id"] == RID1)
            target["resume"]["keySkills"] = [
                {"string": name} for name in operation["after"]
            ]
        elif kind == "skill_level":
            for state in self.states:
                for row in state["resume"]["resumeApplicantSkills"]:
                    if row["id"] == operation["skill_id"]:
                        row["level"] = {
                            "id": operation["after_level_id"],
                            "internalId": operation["after"],
                            "name": operation["after"],
                            "rank": {"base": 1, "middle": 2, "advanced": 3}[
                                operation["after"]
                            ],
                        }
        else:
            raise AssertionError(operation)

    async def prepare(self, changes=None):
        return await self.registry.methods["prepare_resume_update"](
            RID1, changes or {"about": "New about"}
        )

    async def apply(self, draft, *, digest=None, confirmed=True):
        return await self.registry.methods["apply_resume_update"](
            draft["draft_id"], digest or draft["digest"], confirmed
        )

    async def test_prepare_own_resume_unknown_and_noop(self):
        draft = await self.prepare()
        self.assertEqual(draft["status"], "prepared")
        self.assertEqual(draft["preview"]["about"]["before"], "about")
        self.assertEqual(draft["preview"]["about"]["after"], "New about")

        with self.assertRaisesRegex(ValueError, "own resumes"):
            await self.registry.methods["prepare_resume_update"](
                "c" * 40, {"about": "x"}
            )

        draft_count = len(list((self.root / "drafts").glob("*.json")))
        no_op = await self.prepare({"headline": "Resume One"})
        self.assertEqual(no_op["status"], "no_changes")
        self.assertEqual(
            len(list((self.root / "drafts").glob("*.json"))),
            draft_count,
        )

    async def test_confirmation_false_wrong_digest_cancelled_and_expired_rejected(self):
        draft = await self.prepare()
        with self.assertRaisesRegex(ValueError, "confirmation"):
            await self.apply(draft, confirmed=False)
        with self.assertRaisesRegex(ValueError, "digest"):
            await self.apply(draft, digest="0" * 64)

        stored = resume_updates.load_resume_update_draft(draft["draft_id"])
        stored["status"] = "cancelled"
        resume_updates.save_resume_update_draft(stored)
        with self.assertRaisesRegex(ValueError, "cancelled"):
            await self.apply(draft)

        fresh = await self.prepare({"about": "Another"})
        stored = resume_updates.load_resume_update_draft(fresh["draft_id"])
        stored["created_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=25)
        ).isoformat()
        resume_updates.save_resume_update_draft(stored)
        with self.assertRaisesRegex(ValueError, "expired"):
            await self.apply(fresh)

    async def test_stale_resume_blocks_before_writer(self):
        draft = await self.prepare()
        self.states[0]["resume"]["salary"][0]["amount"] += 1
        result = await self.apply(draft)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("changed after preview", result["reason"])
        self.assertEqual(self.writer_calls, [])

    async def test_only_approved_operations_are_sent_and_success_needs_readback(self):
        draft = await self.prepare(
            {
                "headline": "Golang Developer",
                "experience": [{"entry_id": 101, "description": "Approved text"}],
                "about": "Approved about",
            }
        )
        result = await self.apply(draft)
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(
            [operation_key(item) for item in self.writer_calls],
            ["headline", "experience:101", "about"],
        )

        draft = await self.prepare({"about": "Must be observed"})
        self.writer_mode = "no_readback_change"
        result = await self.apply(draft)
        self.assertNotEqual(result["status"], "success")

    async def test_dispatch_started_is_durable_before_suboperation_result(self):
        draft = await self.prepare()
        result = await self.apply(draft)
        self.assertEqual(result["status"], "success")
        with ResumeUpdateJournal() as journal:
            kinds = [event["kind"] for event in journal.events]
        self.assertLess(kinds.index("dispatch_started"), kinds.index("suboperation_result"))

    async def test_validation_error_never_becomes_success(self):
        draft = await self.prepare()
        self.writer_mode = "validation"
        result = await self.apply(draft)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.states[0]["resume"]["skills"], [{"string": "about"}])

    async def test_timeout_before_dispatch_is_blocked(self):
        draft = await self.prepare()
        self.writer_mode = "timeout_before"
        result = await self.apply(draft)
        self.assertEqual(result["status"], "blocked")
        with ResumeUpdateJournal() as journal:
            self.assertFalse(any(e["kind"] == "dispatch_started" for e in journal.events))

    async def test_timeout_after_dispatch_is_unverified_and_retry_is_read_only(self):
        draft = await self.prepare()
        self.writer_mode = "timeout_after"
        first = await self.apply(draft)
        self.assertEqual(first["status"], "unverified")
        calls = len(self.writer_calls)

        self.writer_mode = "success"
        second = await self.apply(draft)
        self.assertEqual(second["status"], "reconciled")
        self.assertEqual(len(self.writer_calls), calls)

    async def test_second_operation_uncertain_returns_partial_success_and_retry_reconciles(self):
        draft = await self.prepare(
            {"headline": "Golang Developer", "about": "Second change"}
        )
        calls = 0

        async def mixed_writer(page, resume_id, operation, base_snapshot, on_dispatch):
            nonlocal calls
            calls += 1
            self.writer_calls.append(deepcopy(operation))
            on_dispatch()
            if calls == 2:
                raise TimeoutError("uncertain second write")
            self._apply_to_live_state(operation)
            return {"status": "dispatched", "dispatched": True, "http_status": 200}

        with patch(
            "hh_mcp_server.tools.resume_update.apply_resume_update_operation",
            side_effect=mixed_writer,
        ):
            first = await self.apply(draft)
        self.assertEqual(first["status"], "partial_success", first)
        self.assertEqual(first["confirmed_operations"], ["headline"])
        self.assertEqual(first["unverified_operations"], ["about"])

        writer_count = len(self.writer_calls)
        second = await self.apply(draft)
        self.assertEqual(second["status"], "reconciled")
        self.assertEqual(len(self.writer_calls), writer_count)

    async def test_in_progress_draft_after_crash_reconciles_without_retry(self):
        draft = await self.prepare(
            {"headline": "Golang Developer", "about": "Second change"}
        )
        first = draft["operations"][0]
        self._apply_to_live_state(first)
        stored = resume_updates.load_resume_update_draft(draft["draft_id"])
        stored["status"] = "in_progress"
        stored["completed_operations"] = ["headline"]
        resume_updates.save_resume_update_draft(stored)

        result = await self.apply(draft)
        self.assertEqual(result["status"], "reconciled")
        self.assertEqual(result["confirmed_operations"], ["headline"])
        self.assertEqual(self.writer_calls, [])


class PrepareReadOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_blocks_page_initiated_mutation_and_confirm_defaults_false(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(service_workers="block")
        page = await context.new_page()
        page.set_default_timeout(2000)
        mutations = []

        async def fixture(route):
            request = route.request
            path = request.url.split("?", 1)[0]
            if request.method == "GET" and path.endswith("/applicant/resumes"):
                await route.fulfill(
                    status=200,
                    content_type="text/html",
                    body="""<!doctype html><script>
                    fetch('/applicant/resume/edit', {
                      method: 'POST',
                      headers: {'Content-Type':'application/json'},
                      body: JSON.stringify({unapproved:true})
                    }).catch(() => {});
                    </script>""",
                )
            elif request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                mutations.append((request.method, path))
                await route.fulfill(status=200, body="{}")
            else:
                await route.abort()

        await context.route("**/*", fixture)

        async def get_resumes(active_page):
            await active_page.goto(
                "https://hh.ru/applicant/resumes", wait_until="domcontentloaded"
            )
            await active_page.wait_for_timeout(100)
            return [
                {"id": RID1, "title": "Resume One"},
                {"id": RID2, "title": "Resume Two"},
            ]

        registry = ToolRegistry()
        register_resume_update_tools(registry)
        self.assertIs(
            inspect.signature(registry.methods["apply_resume_update"])
            .parameters["confirmed"]
            .default,
            False,
        )

        try:
            with (
                patch.object(
                    resume_updates, "RESUME_UPDATE_DRAFT_DIR", root / "drafts"
                ),
                patch.object(resume_update_ledger, "PROFILE_DIR", root),
                patch(
                    "hh_mcp_server.tools.resume_update.get_page",
                    new=AsyncMock(return_value=page),
                ),
                patch(
                    "hh_mcp_server.tools.resume_update.ensure_authenticated",
                    new=AsyncMock(),
                ),
                patch(
                    "hh_mcp_server.tools.resume_update.get_my_resumes",
                    side_effect=get_resumes,
                ),
                patch(
                    "hh_mcp_server.tools.resume_update.read_owned_resume_states",
                    new=AsyncMock(return_value=owned()),
                ),
            ):
                result = await registry.methods["prepare_resume_update"](
                    RID1, {"about": "New about"}
                )
            self.assertEqual(result["status"], "prepared")
            self.assertEqual(mutations, [])
        finally:
            await context.close()
            await browser.close()
            await playwright.stop()
            directory.cleanup()


if __name__ == "__main__":
    unittest.main()
