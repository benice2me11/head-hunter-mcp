"""Regression tests for current hh.ru vacancy search markup."""
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright

from hh_mcp_server.scraping.vacancy_search import build_search_url, search_vacancies


class VacancySearchMarkupTests(unittest.IsolatedAsyncioTestCase):
    def test_search_url_defaults_to_newest_first(self):
        query = parse_qs(urlparse(build_search_url("Golang backend")).query)
        self.assertEqual(query["order_by"], ["publication_time"])

    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        await self.page.set_content(
            """<!doctype html><html><body>
            <h1 data-qa="vacancies-search-header">Найдено 45 вакансий «Codex»</h1>
            <div data-qa="serp-item">
              <a data-qa="serp-item__title"
                 href="https://hh.ru/vacancy/137050747?query=Codex">
                Fullstack-разработчик
              </a>
              <div class="compensation-labels--current-hh">
                <span>до&nbsp;<data value="480000">480 000</data>&nbsp;<data value="RUR">₽</data>&nbsp;за месяц, на руки</span>
                <span data-qa="vacancy-serp__vacancy-work-experience-moreThan6">Опыт более 6 лет</span>
              </div>
              <a data-qa="vacancy-serp__vacancy-employer">ООО Диджитал Лайн</a>
              <span data-qa="vacancy-serp__vacancy-address">Москва</span>
              <span data-qa="vacancy-serp__vacancy_snippet_responsibility">Backend и frontend.</span>
              <span data-qa="vacancy-serp__vacancy_snippet_requirement">Go и Codex.</span>
            </div>
            </body></html>"""
        )

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_current_serp_extracts_salary_and_result_count(self):
        with patch(
            "hh_mcp_server.scraping.vacancy_search.navigate_and_wait",
            new=AsyncMock(),
        ):
            result = await search_vacancies(
                self.page, text="Codex", salary_from=200000, max_pages=1
            )

        self.assertEqual(result["total_found"], "Найдено 45 вакансий «Codex»")
        self.assertEqual(len(result["vacancies"]), 1)
        self.assertEqual(
            result["vacancies"][0]["salary"],
            "до\xa0480 000\xa0₽\xa0за месяц, на руки",
        )
        self.assertEqual(result["vacancies"][0]["id"], "137050747")
        self.assertEqual(result["pages_loaded"], 1)

    async def test_multi_page_search_does_not_depend_on_pager_next(self):
        async def fake_navigate(page, url, wait_selector=None, timeout=15000):
            page_num = int(parse_qs(urlparse(url).query)["page"][0])
            if page_num >= 2:
                await page.set_content("<!doctype html><html><body></body></html>")
                return

            vacancy_id = "137050747" if page_num == 0 else "137050748"
            await page.set_content(
                f"""<!doctype html><html><body>
                <h1 data-qa="vacancies-search-header">Найдено 2 вакансии «Codex»</h1>
                <div data-qa="serp-item">
                  <a data-qa="serp-item__title"
                     href="https://hh.ru/vacancy/{vacancy_id}?query=Codex">
                    Fullstack-разработчик {page_num}
                  </a>
                </div>
                </body></html>"""
            )

        with patch(
            "hh_mcp_server.scraping.vacancy_search.navigate_and_wait",
            new=AsyncMock(side_effect=fake_navigate),
        ) as navigate:
            result = await search_vacancies(self.page, text="Codex", max_pages=3)

        self.assertEqual(result["vacancy_ids"], ["137050747", "137050748"])
        self.assertEqual(result["pages_loaded"], 2)
        self.assertEqual(navigate.await_count, 3)

    async def test_repeated_page_stops_pagination_without_duplicates(self):
        async def fake_navigate(page, url, wait_selector=None, timeout=15000):
            await page.set_content(
                """<!doctype html><html><body>
                <h1 data-qa="vacancies-search-header">Найдено 100 вакансий «Codex»</h1>
                <div data-qa="serp-item">
                  <a data-qa="serp-item__title"
                     href="https://hh.ru/vacancy/137050747?query=Codex">
                    Fullstack-разработчик
                  </a>
                </div>
                </body></html>"""
            )

        with patch(
            "hh_mcp_server.scraping.vacancy_search.navigate_and_wait",
            new=AsyncMock(side_effect=fake_navigate),
        ) as navigate:
            result = await search_vacancies(self.page, text="Codex", max_pages=5)

        self.assertEqual(result["vacancy_ids"], ["137050747"])
        self.assertEqual(result["pages_loaded"], 1)
        self.assertEqual(navigate.await_count, 2)


if __name__ == "__main__":
    unittest.main()
