import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

from hh_mcp_server.scraping.responses import get_my_responses


class ResponseScrapingTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_my_responses_returns_structured_vacancy_ids(self):
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(
            """
            <html><body>
              <div>
                <input data-qa="negotiations-item-checkbox" type="checkbox">
                <a href="/vacancy/137763639?hhtmFrom=negotiation_list">
                  <span data-qa="negotiations-item-vacancy">Разработчик Golang</span>
                </a>
                <a href="/employer/5451960?hhtmFrom=negotiation_list">
                  <div data-qa="negotiations-item-company">ВФМ Технолоджи</div>
                </a>
                <div data-qa="negotiations-item-date">24 сентября</div>
                <div data-qa="negotiations-tag negotiations-item-discard">Отказ</div>
              </div>
              <div>
                <input data-qa="negotiations-item-checkbox" type="checkbox">
                <a href="/vacancy/136885519?hhtmFrom=negotiation_list">
                  <span data-qa="negotiations-item-vacancy">Senior/middle+ Golang-разработчик</span>
                </a>
                <a href="/employer/9323846?hhtmFrom=negotiation_list">
                  <div data-qa="negotiations-item-company">SpaceAI</div>
                </a>
                <div data-qa="negotiations-item-date">12 сентября</div>
                <div data-qa="negotiations-tag negotiations-item-interview">Собеседование</div>
              </div>
            </body></html>
            """
        )
        try:
            with patch(
                "hh_mcp_server.scraping.responses.navigate_and_wait",
                new=AsyncMock(),
            ):
                result = await get_my_responses(page)
        finally:
            await browser.close()
            await playwright.stop()

        self.assertEqual(len(result["responses"]), 2)
        first = result["responses"][0]
        self.assertEqual(first["vacancy_id"], "137763639")
        self.assertEqual(first["vacancy_url"], "https://hh.ru/vacancy/137763639")
        self.assertEqual(first["employer"], "ВФМ Технолоджи")
        self.assertEqual(first["employer_id"], "5451960")
        self.assertEqual(first["status"], "Отказ")
        self.assertEqual(first["status_code"], "discard")
        self.assertEqual(first["applied_at"], "24 сентября")
        self.assertFalse(first["deleted"])

        second = result["responses"][1]
        self.assertEqual(second["vacancy_id"], "136885519")
        self.assertEqual(second["status"], "Собеседование")
        self.assertEqual(second["status_code"], "interview")


if __name__ == "__main__":
    unittest.main()
