import unittest

from playwright.async_api import async_playwright

from hh_mcp_server.scraping.apply import questions_and_fill
from hh_mcp_server.submission_guard import matches_application, parse_fields


class QuestionnaireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.page.close()
        await self.browser.close()
        await self.playwright.stop()

    async def test_groups_and_fills_supported_question_types(self):
        await self.page.set_content(
            """
            <div data-qa="task-body">
              <h3>Experience</h3>
              <label><input type="radio" name="task_exp" value="lt3">Less than 3 years</label>
              <label><input type="radio" name="task_exp" value="3to5">3–5 years</label>
            </div>
            <div data-qa="task-body">
              <h3>Backend</h3>
              <label><input type="radio" name="task_backend" value="go">Golang</label>
              <label><input type="radio" name="task_backend" value="python">Python</label>
            </div>
            <div data-qa="task-body">
              <h3>Tools</h3>
              <label><input type="checkbox" name="task_tools" value="codex">Codex</label>
              <label><input type="checkbox" name="task_tools" value="mcp">MCP</label>
            </div>
            <div data-qa="task-body">
              <h3>Level</h3>
              <select name="task_level"><option value="mid">Middle</option><option value="senior">Senior</option></select>
            </div>
            <div data-qa="task-body">
              <h3>City</h3>
              <input name="task_city" type="text">
            </div>
            <div data-qa="task-body">
              <h3>Workflow</h3>
              <textarea name="task_workflow"></textarea>
            </div>
            """
        )
        answers = {
            "task_exp": "3–5 years",
            "task_backend": "Golang",
            "task_tools": ["Codex", "MCP"],
            "task_level": "Senior",
            "task_city": "Moscow",
            "task_workflow": "Daily AI-assisted development",
        }

        questions, incomplete, payload = await questions_and_fill(self.page, answers)

        self.assertFalse(incomplete)
        by_name = {question["name"]: question for question in questions}
        self.assertEqual(by_name["task_exp"]["options"], ["Less than 3 years", "3–5 years"])
        self.assertEqual(by_name["task_backend"]["options"], ["Golang", "Python"])
        self.assertIsNone(by_name["task_backend"]["control_semantics_warning"])
        self.assertEqual(payload["task_exp"], "3to5")
        self.assertEqual(payload["task_backend"], "go")
        self.assertEqual(payload["task_tools"], ["codex", "mcp"])
        self.assertEqual(payload["task_level"], "senior")
        self.assertEqual(payload["task_city"], "Moscow")
        self.assertEqual(payload["task_workflow"], "Daily AI-assisted development")

    async def test_radio_multiple_wording_emits_semantics_warning(self):
        await self.page.set_content(
            """
            <div data-qa="task-body">
              <h3>С какими технологиями есть опыт? Можно выбрать несколько</h3>
              <label><input type="radio" name="task_stack" value="go">Golang</label>
              <label><input type="radio" name="task_stack" value="python">Python</label>
            </div>
            """
        )
        questions, incomplete, payload = await questions_and_fill(
            self.page, {"task_stack": "Golang"}
        )
        self.assertFalse(incomplete)
        self.assertEqual(payload["task_stack"], "go")
        self.assertIn("multiple selection", questions[0]["control_semantics_warning"])

    async def test_unknown_or_unmatched_answer_is_incomplete(self):
        await self.page.set_content(
            '<label><input type="radio" name="task_backend" value="go">Golang</label>'
        )
        _, incomplete, payload = await questions_and_fill(
            self.page, {"task_backend": "Rust", "task_unknown": "x"}
        )
        self.assertTrue(incomplete)
        self.assertEqual(payload, {})


class SubmissionQuestionPayloadTests(unittest.TestCase):
    def test_semantic_radio_answer_can_map_to_exact_payload_value(self):
        fields = {
            "vacancy_id": "123",
            "resume_hash": "a" * 40,
            "letter": "hello",
            "task_backend": "go",
        }
        expected = {
            "vacancy_id": "123",
            "resume_id": "a" * 40,
            "cover_letter": "hello",
            "question_answers": {"task_backend": "Golang"},
        }
        self.assertTrue(
            matches_application(fields, expected, question_payload_answers={"task_backend": "go"})
        )

    def test_repeated_checkbox_fields_are_preserved(self):
        parsed = parse_fields(
            b"vacancy_id=123&task_tools=codex&task_tools=mcp",
            "application/x-www-form-urlencoded",
        )
        self.assertEqual(parsed["task_tools"], ["codex", "mcp"])


if __name__ == "__main__":
    unittest.main()
