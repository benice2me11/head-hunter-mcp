import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import async_playwright

from hh_mcp_server.scraping.resume_update import (
    apply_resume_update_operation,
    operation_ui,
    read_owned_resume_states,
)
from hh_mcp_server.resume_update_guard import build_expected_mutation
from hh_mcp_server.resume_updates import build_resume_update_plan
from tests.test_resume_updates import RID1, RID2, owned, resume


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def json(self):
        return self._payload


class ScrapingTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_reader_uses_only_get_and_preserves_owned_metadata(self):
        first = resume("Resume One")
        second = resume("Resume Two")
        request = Mock()
        request.get = AsyncMock(
            side_effect=[
                FakeResponse({"resume": first}),
                FakeResponse({"resume": second}),
            ]
        )
        page = Mock(context=Mock(request=request))
        owned = [
            {"id": RID1, "title": "Resume One"},
            {"id": RID2, "title": "Resume Two"},
        ]

        result = await read_owned_resume_states(page, owned)
        self.assertEqual(
            result,
            [
                {"id": RID1, "title": "Resume One", "resume": first},
                {"id": RID2, "title": "Resume Two", "resume": second},
            ],
        )
        self.assertEqual(request.get.await_count, 2)
        for call, resume_id in zip(request.get.await_args_list, (RID1, RID2)):
            self.assertTrue(call.args[0].endswith("/applicant/resume"))
            self.assertEqual(call.kwargs["params"], {"resume": resume_id})

    async def test_structured_reader_fails_closed_on_non_200_or_bad_shape(self):
        for response in (
            FakeResponse({"resume": {}}, status=403),
            FakeResponse({"unexpected": {}}, status=200),
        ):
            with self.subTest(status=response.status):
                request = Mock(get=AsyncMock(return_value=response))
                page = Mock(context=Mock(request=request))
                with self.assertRaises(ValueError):
                    await read_owned_resume_states(
                        page, [{"id": RID1, "title": "Resume One"}]
                    )

    def test_operation_ui_uses_only_researched_routes_and_selectors(self):
        cases = [
            (
                {"kind": "headline", "after": "x"},
                f"/resume/edit/{RID1}/position",
                "resume-edit-title-suggest",
                "resume-partial-edit-save",
            ),
            (
                {"kind": "about", "after": "x"},
                f"/resume/edit/{RID1}/about",
                "resume-editor-about",
                "resume-partial-edit-save",
            ),
            (
                {
                    "kind": "experience_description",
                    "entry_id": 123,
                    "after": "x",
                },
                "/profile/edit/experience/123",
                "resume-editor-experience-description-input",
                "profile-layout-save-button",
            ),
            (
                {"kind": "skills", "after": ["Go"]},
                f"/resume/edit/{RID1}/keySkills",
                "resume-editor-skills-input",
                "resume-partial-edit-save",
            ),
            (
                {
                    "kind": "skill_level",
                    "name": "Go",
                    "after": "advanced",
                    "after_level_id": 10,
                },
                f"/resume/edit/{RID1}/skillsLevels",
                "skill",
                "resume-partial-edit-save",
            ),
        ]
        for operation, path, field_qa, save_qa in cases:
            with self.subTest(kind=operation["kind"]):
                spec = operation_ui(RID1, operation)
                self.assertIn(path, spec["url"])
                self.assertIn(field_qa, spec["field"])
                self.assertIn(save_qa, spec["save"])

    async def test_experience_write_captures_noop_then_dispatches_only_patched_payload(self):
        plan = build_resume_update_plan(
            RID1,
            {"experience": [{"entry_id": 101, "description": "approved"}]},
            owned(),
        )
        operation = plan["operations"][0]
        baseline_operation = {**operation, "after": operation["before"]}
        baseline = build_expected_mutation(
            RID1, baseline_operation, plan["base_snapshot"]
        )

        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(service_workers="block")
        page = await context.new_page()
        page.set_default_timeout(2000)
        writes = []

        async def fixture(route):
            request = route.request
            path = request.url.split("?", 1)[0]
            if request.method == "GET" and "/profile/edit/experience/101" in path:
                body = f"""<!doctype html><html><body>
                <textarea data-qa="resume-editor-experience-description-input">{operation["before"]}</textarea>
                <button data-qa="profile-layout-save-button">Save</button>
                <script>
                document.querySelector('[data-qa="profile-layout-save-button"]').onclick = async () => {{
                  try {{
                    await fetch('https://resume-profile-front.hh.ru/profile/shards/profile/update', {{
                      method: 'POST',
                      credentials: 'same-origin',
                      headers: {{
                        'Content-Type': 'application/json',
                        'X-Requested-With': 'XMLHttpRequest'
                      }},
                      body: JSON.stringify({json.dumps(baseline.payload)})
                    }});
                  }} catch (_) {{}}
                }};
                </script>
                </body></html>"""
                await route.fulfill(status=200, content_type="text/html", body=body)
            elif (
                request.method == "POST"
                and request.url.startswith("https://resume-profile-front.hh.ru/")
                and path.endswith("/profile/shards/profile/update")
            ):
                writes.append(json.loads(request.post_data))
                await route.fulfill(
                    status=200, content_type="application/json", body='{"ok":true}'
                )
            else:
                await route.abort()

        await context.route("**/*", fixture)
        dispatched = Mock()
        try:
            with patch(
                "hh_mcp_server.scraping.resume_update.ensure_authenticated",
                new=AsyncMock(),
            ):
                result = await apply_resume_update_operation(
                    page,
                    RID1,
                    operation,
                    plan["base_snapshot"],
                    dispatched,
                )
            self.assertEqual(result["status"], "dispatched", result)
            self.assertEqual(len(writes), 1)
            rows = writes[0]["profile"]["experience"]
            target = [row for row in rows if row["id"] == 101]
            self.assertEqual(len(target), 1)
            self.assertEqual(target[0]["description"], "approved")
            baseline_other = [
                row for row in baseline.payload["profile"]["experience"]
                if row["id"] != 101
            ]
            written_other = [row for row in rows if row["id"] != 101]
            self.assertEqual(written_other, baseline_other)
            dispatched.assert_called_once()
        finally:
            await context.close()
            await browser.close()
            await playwright.stop()


if __name__ == "__main__":
    unittest.main()
