import logging

from playwright.async_api import Page

from hh_mcp_server.constants import BASE_URL
from hh_mcp_server.scraping import selectors as S
from hh_mcp_server.scraping.extractor import (
    extract_all_texts,
    extract_employer_id,
    extract_href,
    extract_inner_html,
    extract_text,
    navigate_and_wait,
)

logger = logging.getLogger(__name__)


async def parse_vacancy_page(page: Page, vacancy_id: str) -> dict:
    url = f"{BASE_URL}/vacancy/{vacancy_id}"
    await navigate_and_wait(page, url, S.DETAIL_TITLE)
    return await extract_vacancy_page(page, vacancy_id)


async def extract_vacancy_page(page: Page, vacancy_id: str) -> dict:
    """Read the current page, including terms outside the job description."""
    url = f"{BASE_URL}/vacancy/{vacancy_id}"

    title = await extract_text(page, S.DETAIL_TITLE)
    salary = await extract_text(page, S.DETAIL_SALARY)
    employer = await extract_text(page, S.DETAIL_EMPLOYER)
    employer_href = await extract_href(page, S.DETAIL_EMPLOYER)
    employer_id = extract_employer_id(employer_href) if employer_href else None
    experience = await extract_text(page, S.DETAIL_EXPERIENCE)
    employment = await extract_text(page, S.DETAIL_EMPLOYMENT)
    work_format = await extract_text(page, S.DETAIL_WORK_FORMAT)
    description = await extract_inner_html(page, S.DETAIL_DESCRIPTION)
    skills = await extract_all_texts(page, S.DETAIL_SKILLS)
    details = {}
    for field, selector in {
        'schedule': '[data-qa="work-schedule-by-days-text"]',
        'working_hours': '[data-qa="working-hours-text"]',
        'hiring_formats': '[data-qa="vacancy-hiring-formats"]',
        'payment_frequency': '[data-qa="compensation-frequency-text"]',
        'address': '[data-qa="vacancy-address-with-map"], [data-qa="vacancy-view-raw-address"]',
    }.items():
        details[field] = await extract_text(page, selector)

    already_applied = await page.query_selector(S.ALREADY_APPLIED)

    return {
        "id": vacancy_id,
        "url": url,
        "title": title,
        "salary": salary,
        "employer": {
            "name": employer,
            "id": employer_id,
            "url": f"{BASE_URL}/employer/{employer_id}" if employer_id else None,
        },
        "experience": experience,
        "employment": employment,
        "work_format": work_format,
        "description": description,
        "skills": skills,
        "already_applied": already_applied is not None,
        **details,
    }
