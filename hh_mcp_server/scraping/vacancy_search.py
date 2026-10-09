import logging
from urllib.parse import urlencode

from playwright.async_api import Page

from hh_mcp_server.constants import AREA_CODES, BASE_URL, EXPERIENCE_MAP, SCHEDULE_MAP
from hh_mcp_server.scraping import selectors as S
from hh_mcp_server.scraping.extractor import (
    extract_text,
    extract_vacancy_id,
    navigate_and_wait,
)

logger = logging.getLogger(__name__)


def build_search_url(
    text: str,
    area: str | None = None,
    salary_from: int | None = None,
    salary_to: int | None = None,
    experience: str | None = None,
    schedule: str | None = None,
    page: int = 0,
) -> str:
    params: dict[str, str] = {
        "text": text,
        "page": str(page),
        "per_page": "20",
        "order_by": "publication_time",
    }

    if area:
        area_lower = area.lower().strip()
        params["area"] = AREA_CODES.get(area_lower, area)

    if salary_from:
        params["salary"] = str(salary_from)
        params["only_with_salary"] = "true"

    if experience:
        exp_val = EXPERIENCE_MAP.get(experience)
        if exp_val:
            params["experience"] = exp_val

    if schedule:
        sched_val = SCHEDULE_MAP.get(schedule)
        if sched_val:
            params["schedule"] = sched_val

    return f"{BASE_URL}/search/vacancy?{urlencode(params)}"


async def parse_vacancy_cards(page: Page) -> list[dict]:
    cards = await page.query_selector_all(S.VACANCY_CARD)
    vacancies = []

    for card in cards:
        title_el = await card.query_selector(S.VACANCY_TITLE)
        if not title_el:
            continue

        title = await title_el.inner_text()
        href = await title_el.get_attribute("href") or ""
        vacancy_id = extract_vacancy_id(href)

        salary_el = await card.query_selector(S.VACANCY_SALARY)
        if not salary_el:
            # Current HH SERP renders compensation without data-qa, as a span
            # containing numeric and currency <data> elements.
            salary_el = await card.query_selector("span:has(> data[value])")
        salary = (await salary_el.inner_text()).strip() if salary_el else None

        employer_el = await card.query_selector(S.VACANCY_EMPLOYER)
        employer = (await employer_el.inner_text()).strip() if employer_el else None

        address_el = await card.query_selector(S.VACANCY_ADDRESS)
        address = (await address_el.inner_text()).strip() if address_el else None

        snippet_resp_el = await card.query_selector(S.VACANCY_SNIPPET_RESP)
        snippet_resp = (await snippet_resp_el.inner_text()).strip() if snippet_resp_el else None

        snippet_req_el = await card.query_selector(S.VACANCY_SNIPPET_REQ)
        snippet_req = (await snippet_req_el.inner_text()).strip() if snippet_req_el else None

        vacancies.append({
            "id": vacancy_id,
            "title": title.strip(),
            "url": f"{BASE_URL}/vacancy/{vacancy_id}" if vacancy_id else href,
            "salary": salary,
            "employer": employer,
            "location": address,
            "responsibility": snippet_resp,
            "requirement": snippet_req,
        })

    return vacancies


async def search_vacancies(
    page: Page,
    *,
    text: str,
    area: str | None = None,
    salary_from: int | None = None,
    salary_to: int | None = None,
    experience: str | None = None,
    schedule: str | None = None,
    max_pages: int = 3,
) -> dict:
    all_vacancies = []
    seen_keys: set[str] = set()
    total_found = None
    pages_loaded = 0

    for page_num in range(max_pages):
        url = build_search_url(text, area, salary_from, salary_to, experience, schedule, page_num)
        logger.info("Searching page %d: %s", page_num, url)
        await navigate_and_wait(page, url, S.VACANCY_CARD)

        if total_found is None:
            total_found = await extract_text(page, S.SEARCH_RESULT_COUNT)
            if total_found is None:
                total_found = await extract_text(page, "[data-qa='vacancies-search-header']")

        vacancies = await parse_vacancy_cards(page)
        if not vacancies:
            break

        unique_vacancies = []
        for vacancy in vacancies:
            key = vacancy.get("id") or vacancy.get("url")
            if key and key in seen_keys:
                continue
            if key:
                seen_keys.add(key)
            unique_vacancies.append(vacancy)

        if not unique_vacancies:
            logger.warning(
                "Vacancy search page %d produced no new vacancy IDs; stopping pagination",
                page_num,
            )
            break

        all_vacancies.extend(unique_vacancies)
        pages_loaded += 1

    return {
        "total_found": total_found,
        "pages_loaded": pages_loaded,
        "vacancies": all_vacancies,
        "vacancy_ids": [v["id"] for v in all_vacancies if v["id"]],
    }


def build_recommended_url(
    resume_id: str,
    page: int = 0,
) -> str:
    params: dict[str, str] = {
        "resume": resume_id,
        "from": "resumelist",
        "page": str(page),
        "per_page": "20",
    }
    return f"{BASE_URL}/search/vacancy?{urlencode(params)}"


async def get_recommended_vacancies(
    page: Page,
    *,
    resume_id: str,
    max_pages: int = 5,
) -> dict:
    """Fetch vacancies recommended by hh.ru for a specific resume."""
    all_vacancies = []
    seen_keys: set[str] = set()
    total_found = None
    pages_loaded = 0

    for page_num in range(max_pages):
        url = build_recommended_url(resume_id, page_num)
        logger.info("Fetching recommended vacancies page %d: %s", page_num, url)
        await navigate_and_wait(page, url, S.VACANCY_CARD)

        if total_found is None:
            total_found = await extract_text(page, S.SEARCH_RESULT_COUNT)
            if total_found is None:
                total_found = await extract_text(page, "[data-qa='vacancies-search-header']")

        vacancies = await parse_vacancy_cards(page)
        if not vacancies:
            break

        unique_vacancies = []
        for vacancy in vacancies:
            key = vacancy.get("id") or vacancy.get("url")
            if key and key in seen_keys:
                continue
            if key:
                seen_keys.add(key)
            unique_vacancies.append(vacancy)

        if not unique_vacancies:
            logger.warning(
                "Recommended vacancies page %d produced no new vacancy IDs; stopping pagination",
                page_num,
            )
            break

        all_vacancies.extend(unique_vacancies)
        pages_loaded += 1

    return {
        "total_found": total_found,
        "pages_loaded": pages_loaded,
        "vacancies": all_vacancies,
        "vacancy_ids": [v["id"] for v in all_vacancies if v["id"]],
    }
