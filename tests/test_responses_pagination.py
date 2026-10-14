"""Regression tests for HH response-history page transitions and completeness."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from hh_mcp_server.scraping.responses import (
    ResponsePaginationError,
    _collect_tab,
    get_my_responses,
)


def _card(number):
    return f"""
        <div class="response">
          <a href="/vacancy/{number}">
            <span data-qa="negotiations-item-vacancy">Role {number}</span>
          </a>
          <a href="/employer/{number}">
            <span data-qa="negotiations-item-company">Employer {number}</span>
          </a>
          <span data-qa="negotiations-item-date">Today</span>
        </div>
    """


class ResponseHistoryPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_next_page_accepted_when_cards_change_but_aria_current_does_not(self):
        await self.page.set_content(f"""
          <div id="cards">{_card(1)}{_card(2)}</div>
          <button data-qa="number-pages-1" aria-current="true">1</button>
          <button data-qa="number-pages-2 ">2</button>
          <script>
          document.querySelector('[data-qa~="number-pages-2"]').onclick = () => {{
            setTimeout(() => {{
              document.getElementById('cards').innerHTML = {json.dumps(_card(3))};
            }}, 40);
          }};
          </script>
        """)
        rows = await _collect_tab(
            self.page, tab_qa=None, deleted=False, expected_count=3
        )
        self.assertEqual([row["vacancy_id"] for row in rows], ["1", "2", "3"])

    async def test_missing_followup_page_fails_closed(self):
        await self.page.set_content(f"""
          <div>{_card(1)}</div>
          <button data-qa="number-pages-1">1</button>
        """)
        with self.assertRaisesRegex(ResponsePaginationError, "pager ended"):
            await _collect_tab(self.page, tab_qa=None, deleted=False, expected_count=2)

    async def test_repeated_page_never_becomes_successful_history(self):
        await self.page.set_content(f"""
          <div id="cards">{_card(1)}{_card(2)}</div>
          <button data-qa="number-pages-1" aria-current="true">1</button>
          <button data-qa="number-pages-2 ">2</button>
        """)
        # Simulate a frozen UI instead of spending ten real seconds in a timeout.
        with patch.object(
            self.page, "wait_for_function",
            new=AsyncMock(side_effect=PlaywrightTimeoutError("stale")),
        ):
            with self.assertRaisesRegex(ResponsePaginationError, "two attempts"):
                await _collect_tab(
                    self.page, tab_qa=None, deleted=False, expected_count=3
                )

    async def test_no_readable_ids_is_an_error_not_zero_results(self):
        await self.page.set_content("<button data-qa='number-pages-1'>1</button>")
        with self.assertRaisesRegex(ResponsePaginationError, "no readable vacancy IDs"):
            await _collect_tab(self.page, tab_qa=None, deleted=False, expected_count=2)

    async def test_bounded_limit_is_not_full_history(self):
        await self.page.set_content(f"""
          <button data-qa="tab_filter_all" aria-selected="true" aria-label="??? 2">All</button>
          <button data-qa="tab_filter_deleted" aria-selected="false" aria-label="????????? 10">Deleted</button>
          <div>{_card(1)}{_card(2)}</div>
        """)
        with patch(
            "hh_mcp_server.scraping.responses.navigate_and_wait", new=AsyncMock()
        ):
            data = await get_my_responses(self.page, limit=2, include_deleted=True)
        self.assertEqual(data["active_count"], 2)
        self.assertEqual(data["deleted_count"], 0)
        self.assertFalse(data["history_complete"])

    async def test_unknown_tab_counts_never_claim_full_history(self):
        await self.page.set_content(f"<div>{_card(1)}</div>")
        with patch(
            "hh_mcp_server.scraping.responses.navigate_and_wait", new=AsyncMock()
        ):
            data = await get_my_responses(self.page)
        self.assertEqual(data["active_count"], 1)
        self.assertFalse(data["history_complete"])

    async def test_missing_deleted_tab_with_known_count_fails_closed(self):
        await self.page.set_content(f"<div>{_card(1)}</div>")
        with self.assertRaisesRegex(ResponsePaginationError, "tab missing"):
            await _collect_tab(
                self.page, tab_qa="tab_filter_deleted",
                deleted=True, expected_count=10,
            )

    async def test_active_and_deleted_tabs_complete_after_content_change(self):
        await self.page.set_content(f"""
          <button data-qa="tab_filter_all" aria-selected="true" aria-label="??? 3">All</button>
          <button data-qa="tab_filter_deleted" aria-selected="false" aria-label="????????? 2">Deleted</button>
          <div id="cards">{_card(1)}{_card(2)}</div>
          <button data-qa="number-pages-1" aria-current="true">1</button>
          <button data-qa="number-pages-2 ">2</button>
          <script>
            const all = document.querySelector('[data-qa="tab_filter_all"]');
            const deleted = document.querySelector('[data-qa="tab_filter_deleted"]');
            const pageTwo = document.querySelector('[data-qa~="number-pages-2"]');
            pageTwo.onclick = () => {{
              setTimeout(() => {{
                document.getElementById('cards').innerHTML = {json.dumps(_card(3))};
              }}, 35);
            }};
            deleted.onclick = () => {{
              all.setAttribute('aria-selected', 'false');
              deleted.setAttribute('aria-selected', 'true');
              document.getElementById('cards').innerHTML =
                  {json.dumps(_card(10) + _card(11))};
              pageTwo.remove();
            }};
          </script>
        """)
        with patch(
            "hh_mcp_server.scraping.responses.navigate_and_wait", new=AsyncMock()
        ):
            data = await get_my_responses(self.page, limit=300, include_deleted=True)
        self.assertEqual(data["active_count"], 3)
        self.assertEqual(data["deleted_count"], 2)
        self.assertEqual(
            {(row["vacancy_id"], row["deleted"]) for row in data["responses"]},
            {("1", False), ("2", False), ("3", False), ("10", True), ("11", True)},
        )
        self.assertTrue(data["history_complete"])


if __name__ == "__main__":
    unittest.main()

