import logging
import re
from urllib.parse import urljoin

from playwright.async_api import Page

from hh_mcp_server.constants import BASE_URL
from hh_mcp_server.scraping.extractor import navigate_and_wait

logger = logging.getLogger(__name__)


def _id_from_href(href: str | None, kind: str) -> str | None:
    if not href:
        return None
    match = re.search(rf"/{kind}/(\d+)", href)
    return match.group(1) if match else None


def _status_code(data_qa: str | None) -> str | None:
    if not data_qa:
        return None
    match = re.search(r"negotiations-item-([\w-]+)", data_qa)
    return match.group(1) if match else None


async def _extract_visible_responses(page: Page, *, deleted: bool) -> list[dict]:
    items: list[dict] = []
    vacancies = page.locator('[data-qa="negotiations-item-vacancy"]')

    for index in range(await vacancies.count()):
        vacancy = vacancies.nth(index)
        card = vacancy.locator(
            'xpath=ancestor::div[.//*[@data-qa="negotiations-item-company"] and .//*[@data-qa="negotiations-item-date"]][1]'
        )
        if await card.count() != 1:
            continue

        vacancy_link = vacancy.locator("xpath=ancestor::a[1]")
        vacancy_href = await vacancy_link.get_attribute("href")
        vacancy_id = _id_from_href(vacancy_href, "vacancy")
        if not vacancy_id:
            continue

        company = card.locator('[data-qa="negotiations-item-company"]')
        company_name = (await company.inner_text()).strip() if await company.count() else ""
        company_link = company.locator("xpath=ancestor::a[1]") if await company.count() else None
        company_href = (
            await company_link.get_attribute("href")
            if company_link is not None and await company_link.count()
            else None
        )

        date = card.locator('[data-qa="negotiations-item-date"]')
        applied_at = (await date.inner_text()).strip() if await date.count() else None

        status = card.locator('[data-qa*="negotiations-tag"]')
        status_text = (await status.inner_text()).strip() if await status.count() else None
        status_qa = await status.get_attribute("data-qa") if await status.count() else None

        items.append(
            {
                "vacancy_id": vacancy_id,
                "vacancy_url": f"{BASE_URL}/vacancy/{vacancy_id}",
                "title": (await vacancy.inner_text()).strip(),
                "employer": company_name or None,
                "employer_id": _id_from_href(company_href, "employer"),
                "status": status_text,
                "status_code": _status_code(status_qa),
                "applied_at": applied_at,
                "deleted": deleted,
            }
        )

    return items


async def _collect_tab(page: Page, *, tab_qa: str | None, deleted: bool) -> list[dict]:
    if tab_qa:
        tab = page.locator(f'button[data-qa="{tab_qa}"]')
        if await tab.count() == 0:
            if tab_qa != "tab_filter_all":
                return []
        else:
            await tab.click()
            await page.wait_for_selector(
                f'button[data-qa="{tab_qa}"][aria-selected="true"]', timeout=3000
            )
            await page.wait_for_timeout(250)

    results: list[dict] = []
    seen_pages: set[int] = set()

    while True:
        selected = page.locator('button[aria-current="true"][data-qa^="number-pages-"]')
        current_page = 1
        if await selected.count():
            qa = await selected.first.get_attribute("data-qa")
            match = re.search(r"number-pages-(\d+)", qa or "")
            if match:
                current_page = int(match.group(1))

        if current_page in seen_pages:
            break
        seen_pages.add(current_page)
        results.extend(await _extract_visible_responses(page, deleted=deleted))

        next_page = current_page + 1
        next_button = page.locator(f'button[data-qa^="number-pages-{next_page}"]')
        if await next_button.count() == 0:
            break
        await next_button.click()
        await page.wait_for_selector(
            f'button[data-qa^="number-pages-{next_page}"][aria-current="true"]',
            timeout=3000,
        )
        await page.wait_for_timeout(250)

    return results


async def get_my_responses(page: Page) -> dict:
    url = f"{BASE_URL}/applicant/negotiations"
    await navigate_and_wait(page, url)
    await page.wait_for_timeout(800)

    raw_text = await page.inner_text("body")
    tab_counts: dict[str, int] = {}
    for key, qa in (
        ("all", "tab_filter_all"),
        ("invitation", "tab_filter_invitation"),
        ("interview", "tab_filter_interview"),
        ("awaiting", "tab_filter_awaiting"),
        ("discard", "tab_filter_discard"),
        ("deleted", "tab_filter_deleted"),
    ):
        tab = page.locator(f'button[data-qa="{qa}"]')
        if await tab.count():
            label = await tab.get_attribute("aria-label") or ""
            match = re.search(r"(\d+)\s*$", label)
            tab_counts[key] = int(match.group(1)) if match else 0

    active = await _collect_tab(page, tab_qa="tab_filter_all", deleted=False)
    deleted = await _collect_tab(page, tab_qa="tab_filter_deleted", deleted=True)

    # Keep one row per exact HH vacancy and bucket. Deleted applications remain
    # useful for deduplication because deleting the card does not undo the fact
    # that the user already applied.
    responses: list[dict] = []
    seen: set[tuple[str, bool]] = set()
    for item in active + deleted:
        key = (item["vacancy_id"], item["deleted"])
        if key in seen:
            continue
        seen.add(key)
        responses.append(item)

    return {
        "url": url,
        "raw_text": raw_text,
        "tab_counts": tab_counts,
        "responses": responses,
        "active_count": sum(not item["deleted"] for item in responses),
        "deleted_count": sum(item["deleted"] for item in responses),
    }
