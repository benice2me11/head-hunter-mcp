import asyncio
import time
from urllib.parse import urlparse

from playwright.async_api import Page

from hh_mcp_server.constants import BASE_URL
from hh_mcp_server.drivers.browser import close_browser, get_page, save_state, set_headless
from hh_mcp_server.exceptions import AuthenticationError

LOGIN_URL = f"{BASE_URL}/account/login"
RESUMES_URL = f"{BASE_URL}/applicant/resumes"
ACCOUNT_PATHS = {'/applicant/resumes', '/applicant/profile/me'}
ACCOUNT_MARKERS = [
    '[data-qa="resume"]',
    '[data-qa="applicant-profile-common-name"]',
    '[data-qa="mainmenu_applicantProfilePage"]',
    '[data-qa="mainmenu_myResumes"]',
    '[data-qa="mainmenu_applicantProfile"]',
]


async def check_authenticated(page: Page) -> bool:
    url = urlparse(page.url)
    if url.hostname not in {"hh.ru", "www.hh.ru"} or "login" in url.path or "captcha" in url.path:
        return False
    # Require a positive account marker, never just the absence of a login URL.
    for selector in ACCOUNT_MARKERS:
        if await page.locator(selector).first.is_visible():
            return True
    return False


async def ensure_authenticated(page: Page) -> None:
    # Reuse a positively identified protected page. This avoids a redundant
    # navigation to /applicant/resumes before every tool call in one browser.
    if urlparse(page.url).path.rstrip('/') in ACCOUNT_PATHS and await check_authenticated(page):
        return

    # A public vacancy can contain a "my resumes" link even for a guest.
    # Verify access to the protected applicant page before trusting any marker.
    await page.goto(RESUMES_URL, wait_until="domcontentloaded")
    try:
        await page.wait_for_selector(', '.join(ACCOUNT_MARKERS), timeout=10000)
    except Exception:
        pass
    if urlparse(page.url).path.rstrip('/') not in ACCOUNT_PATHS or not await check_authenticated(page):
        raise AuthenticationError("HH login is required. Run the project login script; do not send applications.")


async def run_login_flow() -> None:
    set_headless(False)
    page = await get_page()
    print("Войдите в hh.ru в открывшемся окне. Пароль и код вводите только на сайте.", flush=True)
    print("После входа сессия будет проверена и сохранена локально.", flush=True)
    try:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            if page.is_closed():
                raise AuthenticationError("Login window was closed before verification.")
            if await check_authenticated(page):
                await page.goto(RESUMES_URL, wait_until="domcontentloaded")
                await ensure_authenticated(page)
                await save_state()
                print("HH_AUTHENTICATED: доступ к личному кабинету подтверждён; сессия сохранена.", flush=True)
                return
            await asyncio.sleep(2)
        raise AuthenticationError("Login timed out; run the login script again.")
    finally:
        await close_browser()
