import json
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from hh_mcp_server.scraping.responses import (
    _collect_tab, _extract_visible_responses, ResponsePaginationError
)


def item(identifier: int, include_details: bool = True) -> str:
    extra = (
        f'<a href="/employer/{identifier}"><span data-qa="negotiations-item-company">'
        f'Employer {identifier}</span></a><span data-qa="negotiations-item-date">Today</span>'
    ) if include_details else ""
    return f"""<div data-qa="negotiations-item">
    <a href="/vacancy/{identifier}"><span data-qa="negotiations-item-vacancy">Role {identifier}</span></a>
    {extra}</div>"""


class IncrementalResponseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=True)
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def test_missing_employer_and_date_retains_exact_vacancy_ids(self):
        await self.page.set_content(
            f"{item(1)}{item(2, False)}{item(3, False)}")
        result = await _extract_visible_responses(self.page, deleted=False)
        self.assertEqual([x["vacancy_id"] for x in result], ["1", "2", "3"])
        self.assertIsNone(result[1]["employer"])
        self.assertIsNone(result[2]["applied_at"])

    async def test_number_page_anchor_is_supported(self):
        await self.page.set_content(f"""
          <div id="cards">{item(1)}{item(2)}</div>
          <a data-qa="number-pages-2" href="#page2">2</a>
          <script>
          document.querySelector('[data-qa="number-pages-2"]').onclick = (e) => {{
            e.preventDefault();
            document.getElementById('cards').innerHTML = {json.dumps(item(3))};
          }};
          </script>
        """)
        rows = await _collect_tab(
            self.page, tab_qa=None, deleted=False, expected_count=3
        )
        self.assertEqual([x["vacancy_id"] for x in rows], ["1", "2", "3"])

    async def test_explicit_pager_next_button_is_supported(self):
        await self.page.set_content(f"""
          <div id="cards">{item(1)}{item(2)}</div>
          <button data-qa="pager-next">Next</button>
          <script>
          document.querySelector('[data-qa="pager-next"]').onclick = () => {{
            document.getElementById('cards').innerHTML = {json.dumps(item(3))};
            document.querySelector('[data-qa="pager-next"]').remove();
          }};
          </script>
        """)
        rows = await _collect_tab(
            self.page, tab_qa=None, deleted=False, expected_count=3
        )
        self.assertEqual([x["vacancy_id"] for x in rows], ["1", "2", "3"])

    async def test_missing_pager_explains_dom_counts_without_applicant_details(self):
        await self.page.set_content("".join(item(i) for i in range(1, 21)))
        with self.assertRaises(ResponsePaginationError) as cm:
            await _collect_tab(
                self.page, tab_qa=None, deleted=False, expected_count=25
            )
        message = str(cm.exception)
        self.assertIn("20 of 25 expected", message)
        self.assertIn("dom_vacancy_nodes=20", message)
        self.assertNotIn("Employer 1", message)
        self.assertNotIn("Role 1", message)

    async def test_unparseable_vacancy_nodes_visible_in_diagnostics(self):
        await self.page.set_content(
            "".join(item(i) for i in range(1, 21)) +
            "".join('<span data-qa="negotiations-item-vacancy">No link</span>'
                    for _ in range(5))
        )
        with self.assertRaisesRegex(
            ResponsePaginationError, "dom_vacancy_nodes=25"
        ):
            await _collect_tab(
                self.page, tab_qa=None, deleted=False, expected_count=25
            )

    async def test_complete_25_records_do_not_require_pager(self):
        await self.page.set_content("".join(item(i) for i in range(1, 26)))
        rows = await _collect_tab(
            self.page, tab_qa=None, deleted=False, expected_count=25
        )
        self.assertEqual(len(rows), 25)

    async def test_tab_timeout_fails_closed_instead_of_name_error(self):
        await self.page.set_content(
            '<button data-qa="tab_filter_all" aria-selected="false">All</button>' +
            item(1)
        )
        with patch.object(
            self.page, "wait_for_selector",
            new=AsyncMock(side_effect=PlaywrightTimeoutError("stuck"))
        ):
            with self.assertRaisesRegex(
                ResponsePaginationError, "tab did not become selected"
            ):
                await _collect_tab(
                    self.page, tab_qa="tab_filter_all",
                    deleted=False, expected_count=1,
                )


if __name__ == "__main__":
    unittest.main()

