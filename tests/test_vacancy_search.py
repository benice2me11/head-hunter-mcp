"""Regression tests for current hh.ru vacancy search markup."""
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

from hh_mcp_server.scraping.vacancy_search import search_vacancies


class VacancySearchMarkupTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
