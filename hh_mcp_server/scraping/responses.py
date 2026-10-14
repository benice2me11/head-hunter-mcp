import logging
import re
from urllib.parse import urljoin

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from hh_mcp_server.constants import BASE_URL
from hh_mcp_server.exceptions import ScrapingError
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


async def _extract_visible_responses(
    page: Page, *, deleted: bool, limit: int | None = None
) -> list[dict]:
    # Extract a whole page in one browser round-trip. Per-field Locator calls are
    # noticeably expensive across dozens of HH response cards.
    raw_items = await page.locator('[data-qa="negotiations-item-vacancy"]').evaluate_all(
        r"""
        (vacancies, limit) => vacancies.slice(0, limit ?? vacancies.length).map((vacancy) => {
            const card = vacancy.closest('div:has([data-qa="negotiations-item-company"]):has([data-qa="negotiations-item-date"])');
            if (!card) return null;
            const vacancyLink = vacancy.closest('a');
            const company = card.querySelector('[data-qa="negotiations-item-company"]');
            const companyLink = company ? company.closest('a') : null;
            const date = card.querySelector('[data-qa="negotiations-item-date"]');
            const status = card.querySelector('[data-qa*="negotiations-tag"]');
            return {
                vacancy_href: vacancyLink?.getAttribute('href') ?? null,
                title: vacancy.textContent?.trim() ?? '',
                company_name: company?.textContent?.trim() ?? '',
                company_href: companyLink?.getAttribute('href') ?? null,
                applied_at: date?.textContent?.trim() ?? null,
                status_text: status?.textContent?.trim() ?? null,
                status_qa: status?.getAttribute('data-qa') ?? null,
            };
        }).filter(Boolean)
        """,
        limit,
    )

    items: list[dict] = []
    for raw in raw_items:
        vacancy_id = _id_from_href(raw["vacancy_href"], "vacancy")
        if not vacancy_id:
            continue
        items.append(
            {
                "vacancy_id": vacancy_id,
                "vacancy_url": f"{BASE_URL}/vacancy/{vacancy_id}",
                "title": raw["title"],
                "employer": raw["company_name"] or None,
                "employer_id": _id_from_href(raw["company_href"], "employer"),
                "status": raw["status_text"],
                "status_code": _status_code(raw["status_qa"]),
                "applied_at": raw["applied_at"],
                "deleted": deleted,
            }
        )
    return items


async def _visible_vacancy_signature(page: Page) -> tuple[str, ...]:
    """Ordered exact vacancy links currently displayed in this tab.

    Unlike aria-current, this detects whether HH actually replaced the cards
    after a pagination click, even when the pager's active-state marker lags.
    """
    values = await page.locator('[data-qa="negotiations-item-vacancy"]').evaluate_all(
        """
        (elements) => elements.map((element) => {
            const href = element.closest('a')?.getAttribute('href') ?? '';
            return /\/vacancy\/(\d+)/.exec(href)?.[1] ?? '';
        }).filter(Boolean)
        """
    )
    return tuple(values)


async def _wait_for_distinct_page(
    page: Page, previous_signature: tuple[str, ...], *, next_page: int, deleted: bool
) -> None:
    try:
        await page.wait_for_function(
            r"""
            (previous) => {
                const values = Array.from(
                    document.querySelectorAll('[data-qa="negotiations-item-vacancy"]')
                ).map(el => {
                    const href = el.closest('a')?.getAttribute('href') ?? '';
                    return /\/vacancy\/(\d+)/.exec(href)?.[1] ?? '';
                }).filter(Boolean);
                return values.length > 0 &&
                    JSON.stringify(values) !== JSON.stringify(previous);
            }
            """,
            arg=list(previous_signature),
            timeout=12000,
        )
    except PlaywrightTimeoutError as exc:
        bucket = "deleted" if deleted else "active"
        raise ScrapingError(
            f"Incomplete {bucket} HH history: page {next_page} did not load "
            "distinct vacancy cards within 12s after pagination click"
        ) from exc


async def _collect_tab(
    page: Page,
    *,
    tab_qa: str | None,
    deleted: bool,
    limit: int | None = None,
    expected_count: int | None = None,
) -> list[dict]:
    bucket = "deleted" if deleted else "active"
    if tab_qa:
        tab = page.locator(f'button[data-qa="{tab_qa}"]')
        if await tab.count() == 0:
            if expected_count:
                raise ScrapingError(
                    f"Incomplete {bucket} HH history: tab missing despite "
                    f"expected count {expected_count}"
                )
            if tab_qa != "tab_filter_all":
                return []
        else:
            await tab.click()
            try:
                await page.wait_for_selector(
                    f'button[data-qa="{tab_qa}"][aria-selected="true"]', timeout=10000
                )
            except PlaywrightTimeoutError as exc:
                raise ScrapingError(
                    f"Incomplete {bucket} HH history: tab did not become selected"
                ) from exc
            await page.wait_for_timeout(250)

    results: list[dict] = []
    seen_ids: set[str] = set()
    seen_signatures: set[tuple[str, ...]] = set()
    current_page = 1

    while True:
        remaining = None if limit is None else max(limit - len(results), 0)
        if remaining == 0:
            break
        signature = await _visible_vacancy_signature(page)
        if not signature:
            if expected_count == 0:
                break
            raise ScrapingError(
                f"Incomplete {bucket} HH history: no vacancy cards on page "
                f"{current_page}"
            )
        if signature in seen_signatures:
            raise ScrapingError(
                f"Incomplete {bucket} HH history: repeated page "
                f"{current_page} (same vacancy links)"
            )
        seen_signatures.add(signature)

        extracted = await _extract_visible_responses(
            page, deleted=deleted, limit=remaining
        )
        if not extracted:
            raise ScrapingError(
                f"Incomplete {bucket} HH history: could not extract vacancy IDs "
                f"from page {current_page}"
            )
        for item in extracted:
            vacancy_id = item["vacancy_id"]
            if vacancy_id not in seen_ids:
                seen_ids.add(vacancy_id)
                results.append(item)
        if limit is not None and len(results) >= limit:
            break

        next_page = current_page + 1
        next_button = page.locator(f'button[data-qa^="number-pages-{next_page}"]')
        if await next_button.count() == 0:
            if expected_count is not None and len(results) < expected_count:
                raise ScrapingError(
                    f"Incomplete {bucket} HH history: reached page {current_page}, "
                    f"but extracted {len(results)} of {expected_count} expected IDs"
                )
            break
        await next_button.click()
        await _wait_for_distinct_page(
            page, signature, next_page=next_page, deleted=deleted
        )
        current_page = next_page

    return results


async def get_my_responses(
    page: Page, *, limit: int | None = None, include_deleted: bool = True
) -> dict:
    url = f"{BASE_URL}/applicant/negotiations"
    await navigate_and_wait(page, url)
    await page.wait_for_timeout(800)

    # raw_text exists for the legacy/full-history consumer. Bounded reads use
    # structured cards only and avoid serializing the entire negotiations page.
    raw_text = await page.inner_text("body") if limit is None else None
    tab_counts: dict[str, int] = {}
    verified_tab_counts: dict[str, int] = {}
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
            if match:
                verified_tab_counts[key] = int(match.group(1))

    active = await _collect_tab(
        page, tab_qa="tab_filter_all", deleted=False, limit=limit,
        expected_count=verified_tab_counts.get("all"),
    )
    deleted_limit = None if limit is None else max(limit - len(active), 0)
    deleted = (
        await _collect_tab(
            page, tab_qa="tab_filter_deleted", deleted=True, limit=deleted_limit,
            expected_count=verified_tab_counts.get("deleted"),
        )
        if include_deleted and deleted_limit != 0
        else []
    )

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

    # A bounded first-page read does not establish a negative dedup check for
    # unseen IDs. Only claim full history when both tab headings and extracted
    # records independently confirm completeness.
    history_complete = (
        "all" in verified_tab_counts
        and (not include_deleted or "deleted" in verified_tab_counts)
        and len(active) >= verified_tab_counts["all"]
        and (not include_deleted or len(deleted) >= verified_tab_counts["deleted"])
    )
    return {
        "history_complete": history_complete,
        "history_partial": not history_complete,
        "url": url,
        "raw_text": raw_text,
        "tab_counts": tab_counts,
        "responses": responses,
        "active_count": sum(not item["deleted"] for item in responses),
        "deleted_count": sum(item["deleted"] for item in responses),
    }
