import logging
import hashlib
import re
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.async_api import Page

from hh_mcp_server.constants import AREA_CODES, BASE_URL, EXPERIENCE_MAP, SCHEDULE_MAP
from hh_mcp_server.exceptions import ScrapingError
from hh_mcp_server.scraping import selectors as S
from hh_mcp_server.scraping.extractor import (
    extract_text,
    extract_vacancy_id,
    navigate_and_wait,
)

logger = logging.getLogger(__name__)


async def _wait_for_cards_settled(page: Page) -> int:
    """Wait for HH's client hydration, which can expand 20 SSR cards to 50.

    Waiting only for the first vacancy selector is insufficient: HH returns
    DOMContentLoaded with an initial batch before rendering the remaining cards.
    """
    previous = -1
    unchanged = 0
    for attempt in range(25):
        count = await page.locator(S.VACANCY_CARD).count()
        unchanged = unchanged + 1 if count == previous else 0
        # Observe the page for at least 0.8 seconds, and require a quiet DOM.
        if count and unchanged >= 4 and attempt >= 5:
            return count
        if not count and unchanged >= 8:
            return 0
        previous = count
        await page.wait_for_timeout(150)
    raise ScrapingError("HH vacancy result cards never settled; search coverage unverified")


def _expected_total(heading: str | None) -> int | None:
    match = re.search(r"Найдено\s+([\d\s\u00a0]+)", heading or "")
    return int(re.sub(r"\D", "", match.group(1))) if match else None


def _page_fingerprint(vacancies: list[dict]) -> str:
    ids = sorted(str(v.get("id") or v.get("url") or "") for v in vacancies)
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()[:12]


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
    page_diagnostics: list[dict] = []
    stop_reason = "max_pages_reached"

    for page_num in range(max_pages):
        url = build_search_url(text, area, salary_from, salary_to, experience, schedule, page_num)
        logger.info("Searching page %d: %s", page_num, url)
        await navigate_and_wait(page, url, S.VACANCY_CARD)
        actual_page = parse_qs(urlparse(page.url).query).get("page", [str(page_num)])[0]
        if page.url.startswith(BASE_URL) and actual_page != str(page_num):
            stop_reason = "redirected_page"
            logger.warning("HH search page %d redirected to page %s", page_num, actual_page)
            break

        if total_found is None:
            total_found = await extract_text(page, S.SEARCH_RESULT_COUNT)
            if total_found is None:
                total_found = await extract_text(page, "[data-qa='vacancies-search-header']")

        await _wait_for_cards_settled(page)
        vacancies = await parse_vacancy_cards(page)
        if not vacancies:
            stop_reason = "empty_page"
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
            stop_reason = "repeated_page"
            break

        all_vacancies.extend(unique_vacancies)
        pages_loaded += 1
        page_diagnostics.append({
            "page": page_num,
            "cards": len(vacancies),
            "new_ids": len(unique_vacancies),
            "fingerprint": _page_fingerprint(vacancies),
        })

    expected = _expected_total(total_found)
    return {
        "total_found": total_found,
        "pages_loaded": pages_loaded,
        "vacancies": all_vacancies,
        "vacancy_ids": [v["id"] for v in all_vacancies if v["id"]],
        "pagination": {
            "requested_pages": max_pages,
            "distinct_pages_verified": pages_loaded,
            "page_diagnostics": page_diagnostics,
            "stop_reason": stop_reason,
            "expected_total": expected,
            "unique_returned": len(seen_keys),
            "shortfall": max(expected - len(seen_keys), 0) if expected is not None else None,
            "coverage_complete": expected is not None and len(seen_keys) >= expected,
        },
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
    stop_reason = "max_pages_reached"
    page_diagnostics: list[dict] = []

    for page_num in range(max_pages):
        url = build_recommended_url(resume_id, page_num)
        logger.info("Fetching recommended vacancies page %d: %s", page_num, url)
        await navigate_and_wait(page, url, S.VACANCY_CARD)
        actual_page = parse_qs(urlparse(page.url).query).get("page", [str(page_num)])[0]
        if page.url.startswith(BASE_URL) and actual_page != str(page_num):
            stop_reason = "redirected_page"
            break

        if total_found is None:
            total_found = await extract_text(page, S.SEARCH_RESULT_COUNT)
            if total_found is None:
                total_found = await extract_text(page, "[data-qa='vacancies-search-header']")

        await _wait_for_cards_settled(page)
        vacancies = await parse_vacancy_cards(page)
        if not vacancies:
            stop_reason = "empty_page"
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
            stop_reason = "repeated_page"
            break

        all_vacancies.extend(unique_vacancies)
        pages_loaded += 1
        page_diagnostics.append({
            "page": page_num,
            "cards": len(vacancies),
            "new_ids": len(unique_vacancies),
            "fingerprint": _page_fingerprint(vacancies),
        })

    expected = _expected_total(total_found)
    return {
        "total_found": total_found,
        "pages_loaded": pages_loaded,
        "vacancies": all_vacancies,
        "vacancy_ids": [v["id"] for v in all_vacancies if v["id"]],
        "pagination": {
            "requested_pages": max_pages,
            "distinct_pages_verified": pages_loaded,
            "page_diagnostics": page_diagnostics,
            "stop_reason": stop_reason,
            "expected_total": expected,
            "unique_returned": len(seen_keys),
            "shortfall": max(expected - len(seen_keys), 0) if expected is not None else None,
            "coverage_complete": expected is not None and len(seen_keys) >= expected,
        },
    }
