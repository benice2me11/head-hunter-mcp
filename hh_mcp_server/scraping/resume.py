import logging

from playwright.async_api import Page

from hh_mcp_server.constants import BASE_URL
from hh_mcp_server.scraping import selectors as S
from hh_mcp_server.scraping.extractor import extract_resume_id, navigate_and_wait

logger = logging.getLogger(__name__)


async def get_my_resumes(page: Page) -> list[dict]:
    url = f"{BASE_URL}/applicant/resumes"
    await navigate_and_wait(page, url, S.RESUME_CARD)

    cards = await page.query_selector_all(S.RESUME_CARD)
    resumes = []
    seen = set()

    for card in cards:
        title_el = await card.query_selector(S.RESUME_TITLE_LINK)
        if not title_el:
            continue

        href = await title_el.get_attribute("href") or ""
        resume_id = extract_resume_id(href)
        if not resume_id or resume_id in seen:
            continue
        # The current profile page wraps the title, salary and schedule in one
        # link; use the observed title element and retain the legacy fallback.
        heading = await card.query_selector(S.RESUME_TITLE) or title_el
        title = (await heading.inner_text()).strip()
        if not title:
            continue
        seen.add(resume_id)

        resumes.append({
            "id": resume_id,
            "title": title,
            "url": f"{BASE_URL}/resume/{resume_id}",
        })

    return resumes


async def parse_resume_page(page: Page, resume_id: str) -> dict:
    url = f"{BASE_URL}/resume/{resume_id}"
    await navigate_and_wait(page, url)

    await page.wait_for_timeout(2000)
    text = await page.inner_text("body")

    return {
        "id": resume_id,
        "url": url,
        "raw_text": text,
    }
