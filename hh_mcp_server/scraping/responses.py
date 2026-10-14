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


async def _extract_visible_responses(
    page: Page, *, deleted: bool, limit: int | None = None
) -> list[dict]:
    # Extract a whole page in one browser round-trip. Per-field Locator calls are
    # noticeably expensive across dozens of HH response cards.
    raw_items = await page.locator('[data-qa="negotiations-item-vacancy"]').evaluate_all(
        """
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


class ResponsePaginationError(RuntimeError):
    """The history could not be verified as complete; never return partial dedup."""


async def _wait_for_distinct_page(page: Page, previous_ids: set[str], page_number: int) -> None:
    # HH sometimes changes the card list without setting aria-current="true"
    # on the numbered button. Validate the data, not only the pager CSS.
    js = """
    (previousIds) => {
        const ids = Array.from(
            document.querySelectorAll('[data-qa="negotiations-item-vacancy"]')
        ).map((node) => {
            const href = node.closest('a')?.getAttribute('href') || '';
            return href.match(/\\/vacancy\\/(\\d+)/)?.[1] || null;
        }).filter(Boolean);
        return ids.length > 0 && ids.some((id) => !previousIds.includes(id));
    }
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    for attempt in range(2):
        try:
            await page.wait_for_function(js, arg=list(previous_ids), timeout=5000)
            return
        except PlaywrightTimeoutError as exc:
            if attempt:
                raise ResponsePaginationError(
                    f"HH response page {page_number} did not load distinct vacancy IDs "
                    "after two attempts; history incomplete"
                ) from exc
            logger.warning(
                "HH response page %d did not change in 5s; retrying numbered page",
                page_number,
            )
            # HH adds a whitespace-separated selected-state token to data-qa,
            # and can leave a trailing space on unselected page buttons.
            next_button = page.locator(f'button[data-qa~="number-pages-{page_number}"]')
            if await next_button.count() == 0:
                raise ResponsePaginationError(
                    f"HH response page {page_number} navigation disappeared; "
                    "history incomplete"
                ) from exc
            await next_button.first.click()


async def _collect_tab(
    page: Page,
    *,
    tab_qa: str | None,
    deleted: bool,
    limit: int | None = None,
    expected_count: int | None = None,
) -> list[dict]:
    if tab_qa:
        tab = page.locator(f'button[data-qa="{tab_qa}"]')
        if await tab.count() == 0:
            if tab_qa != "tab_filter_all":
                if expected_count:
                    raise ResponsePaginationError(
                        f"HH {tab_qa} tab missing with {expected_count} expected records; "
                        "history incomplete"
                    )
                return []
        else:
            await tab.click()
            try:
                await page.wait_for_selector(
                    f'button[data-qa="{tab_qa}"][aria-selected="true"]', timeout=6000
                )
            except PlaywrightTimeoutError as exc:
                raise ResponsePaginationError(
                    f"HH {tab_qa} tab did not become selected; history incomplete"
                ) from exc
            await page.wait_for_timeout(250)

    results: list[dict] = []
    seen_ids: set[str] = set()
    seen_pages: set[frozenset[str]] = set()
    page_number = 1

    while True:
        if page_number > 100:
            raise ResponsePaginationError(
                f"HH {tab_qa or 'response'} exceeded 100 pages; history incomplete"
            )
        remaining = None if limit is None else max(limit - len(results), 0)
        if remaining == 0:
            break
        page_items = await _extract_visible_responses(
            page, deleted=deleted, limit=remaining
        )
        ids = frozenset(item["vacancy_id"] for item in page_items)
        if not ids:
            if expected_count == 0:
                break
            raise ResponsePaginationError(
                f"HH {tab_qa or 'response'} page {page_number} had no readable "
                "vacancy IDs; history incomplete"
            )
        if ids in seen_pages:
            raise ResponsePaginationError(
                f"HH {tab_qa or 'response'} page {page_number} repeated an "
                "earlier set of vacancy IDs; history incomplete"
            )
        seen_pages.add(ids)
        for item in page_items:
            if item["vacancy_id"] not in seen_ids:
                results.append(item)
                seen_ids.add(item["vacancy_id"])
        if limit is not None and len(results) >= limit:
            break

        next_page = page_number + 1
        next_button = page.locator(f'button[data-qa~="number-pages-{next_page}"]')
        if await next_button.count() == 0:
            if expected_count is not None and len(seen_ids) < expected_count:
                raise ResponsePaginationError(
                    f"HH {tab_qa or 'response'} pager ended after "
                    f"{len(seen_ids)} of {expected_count} expected records; "
                    "history incomplete"
                )
            break
        await next_button.first.click()
        await _wait_for_distinct_page(page, set(ids), next_page)
        page_number = next_page

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

    active = await _collect_tab(
        page,
        tab_qa="tab_filter_all",
        deleted=False,
        limit=limit,
        expected_count=tab_counts.get("all"),
    )
    deleted_limit = None if limit is None else max(limit - len(active), 0)
    deleted = (
        await _collect_tab(
            page,
            tab_qa="tab_filter_deleted",
            deleted=True,
            limit=deleted_limit,
            expected_count=tab_counts.get("deleted"),
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

    return {
        "url": url,
        "raw_text": raw_text,
        "tab_counts": tab_counts,
        "responses": responses,
        "active_count": sum(not item["deleted"] for item in responses),
        "deleted_count": sum(item["deleted"] for item in responses),
        "history_complete": include_deleted and (
            "all" in tab_counts and "deleted" in tab_counts
        ) and (
            limit is None or len(active) + len(deleted) < limit
        ),
    }

