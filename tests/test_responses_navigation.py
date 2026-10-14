import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from hh_mcp_server.exceptions import ScrapingError
from hh_mcp_server.scraping.responses import get_my_responses


class FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    async def count(self):
        if self.selector == '[data-qa="negotiations-item-vacancy"]':
            return len(self.page.visible)
        for tab in ("all", "deleted"):
            if f'tab_filter_{tab}' in self.selector:
                return 1 if tab in self.page.data else 0
        for i in range(1, 9):
            if f'number-pages-{i}' in self.selector:
                return int(i > self.page.index + 1 and i <= len(self.page.data[self.page.tab]))
        return 0

    async def get_attribute(self, key):
        if key == "aria-label":
            if "tab_filter_all" in self.selector:
                return f"Все {self.page.count_overrides.get('all', sum(len(p) for p in self.page.data['all']))}"
            if "tab_filter_deleted" in self.selector:
                return f"Удалённые {self.page.count_overrides.get('deleted', sum(len(p) for p in self.page.data['deleted']))}"
        return None

    async def click(self):
        if "tab_filter_all" in self.selector:
            self.page.tab, self.page.index = "all", 0
        elif "tab_filter_deleted" in self.selector:
            self.page.tab, self.page.index = "deleted", 0
        else:
            for i in range(1, 9):
                if f"number-pages-{i}" in self.selector:
                    if not self.page.stuck:
                        self.page.index = i-1
                    break

    async def evaluate_all(self, expression, *args):
        if not args:
            return list(self.page.visible)
        limit = args[0]
        ids = self.page.visible[:limit] if limit is not None else self.page.visible
        return [{
            "vacancy_href": f"/vacancy/{id}",
            "title": f"Role {id}",
            "company_name": f"Company {id}",
            "company_href": "/employer/555",
            "applied_at": "сегодня",
            "status_text": "Собеседование",
            "status_qa": "negotiations-item-interview",
        } for id in ids]


class FakePage:
    def __init__(self, active, deleted, stuck=False, count_overrides=None):
        self.data = {"all": active, "deleted": deleted}
        self.tab, self.index = "all", 0
        self.stuck = stuck
        self.count_overrides = count_overrides or {}

    @property
    def visible(self):
        return self.data[self.tab][self.index]

    def locator(self, selector):
        return FakeLocator(self, selector)

    async def wait_for_selector(self, *args, **kwargs):
        return None

    async def wait_for_timeout(self, *args, **kwargs):
        return None

    async def inner_text(self, *args, **kwargs):
        return ""

    async def wait_for_function(self, *args, **kwargs):
        previous = tuple(kwargs.get("arg") or [])
        new = tuple(self.visible)
        if not new or previous == new:
            raise PlaywrightTimeoutError("Waited and content did not change")
        return True


class PageHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_active_and_two_deleted_pages_without_aria_current(self):
        page = FakePage([["1"], ["2"]], [["3"], ["4"]])
        with patch("hh_mcp_server.scraping.responses.navigate_and_wait", new=AsyncMock()):
            result = await get_my_responses(page, limit=50, include_deleted=True)
        self.assertEqual(
            [(x["vacancy_id"], x["deleted"]) for x in result["responses"]],
            [("1",False),("2",False),("3",True),("4",True)],
        )
        self.assertTrue(result["history_complete"])
        self.assertFalse(result["history_partial"])

    async def test_bounded_limit_declares_partial(self):
        page = FakePage([["1", "2"],["5"]], [["3"],["4"]])
        with patch("hh_mcp_server.scraping.responses.navigate_and_wait", new=AsyncMock()):
            result = await get_my_responses(page, limit=2, include_deleted=True)
        self.assertEqual(result["active_count"], 2)
        self.assertTrue(result["history_partial"])
        self.assertEqual(result["deleted_count"], 0)

    async def test_repeated_page_is_failed_closed(self):
        page = FakePage([["1"],["2"]], [["3"]], stuck=True)
        with patch("hh_mcp_server.scraping.responses.navigate_and_wait", new=AsyncMock()):
            with self.assertRaisesRegex(ScrapingError, "did not load distinct"):
                await get_my_responses(page, limit=50, include_deleted=True)

    async def test_zero_cards_not_silent_success(self):
        page = FakePage([[]], [["4"]], count_overrides={"all": 1})
        with patch("hh_mcp_server.scraping.responses.navigate_and_wait", new=AsyncMock()):
            with self.assertRaisesRegex(ScrapingError, "no vacancy cards"):
                await get_my_responses(page, limit=50, include_deleted=True)


if __name__ == "__main__":
    unittest.main()
