import asyncio
import re
import sys
import time
import gc
from loguru import logger

from config import Config
from auth_sportybet_login import SportyBetAuth
from feeds.polymarket_ws import PolymarketFeed
from feeds.sportybet_api import SportyBetFeed
from core.match_linker import MatchLinker
from core.executor import BetExecutor, BetResult
from core.cashout import CashOutManager
from utils.telegram import TelegramAlerter
from utils.account_manager import AccountManager


class ArbitrageBot:
    def __init__(self):
        self.account_manager = AccountManager()
        self.alerter = TelegramAlerter()
        self.auth = SportyBetAuth()

        self.sportybet = SportyBetFeed(poll_interval=Config.SPORTYBET_POLL_INTERVAL)
        self.sportybet.set_alerter(self.alerter)

        self.polymarket = PolymarketFeed(self._noop_callback)

        self.linker = MatchLinker(self.polymarket, self.sportybet.matches, self.alerter)
        self.linker.set_goal_callback(self.on_goal)
        self.linker.set_link_callback(self.on_new_link)
        self.linker.set_unlink_callback(self.on_unlink)

        self.executor = None
        self.cashout = CashOutManager(self.alerter)

        self.match_pages = {}
        self._page_contexts = {}
        self._opening_pages = set()

        self.running = False
        self._processing = {}
        self.browser = None
        self._cleanup_task = None

    async def _noop_callback(self, match):
        pass

    async def setup(self):
        if not self.account_manager.accounts:
            print("No accounts! Add one:")
            u = input("Username: ")
            p = input("Password: ")
            ph = input("Phone: ")
            self.account_manager.add_account(u, p, ph)
        if not self.account_manager.get_active():
            logger.error("No active account")
            sys.exit(1)

    async def start(self):
        await self.setup()
        logger.info("BOT STARTING — page score only + All-tab scroll markets")

        print("\nSPORTYBET LOGIN")
        phone = input("Phone Number: ")
        password = input("Password: ")

        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.browser = browser

        page = await browser.new_page(viewport={"width": 1365, "height": 900})
        await page.goto("https://www.sportybet.com/ng/", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        try:
            await page.click(
                'button:has-text("Log In"), a:has-text("Log In")', timeout=5000
            )
            await asyncio.sleep(1)
        except Exception:
            pass

        await page.wait_for_selector(
            'input[name="phone"]', state="visible", timeout=15000
        )
        await page.fill('input[name="phone"]', phone)
        await page.wait_for_selector(
            'input[name="psd"]', state="visible", timeout=10000
        )
        await page.fill('input[name="psd"]', password)
        await page.click('button[name="logIn"]')
        await asyncio.sleep(5)
        logger.success("SportyBet login complete")

        self.auth.browser = browser
        self.auth.page = page
        self.executor = BetExecutor(
            self.alerter, self.account_manager, self.sportybet.matches, None
        )
        self.executor.current_account = self.account_manager.get_active()

        bal = float(input("Balance: "))
        self.executor.balance = bal
        await self.alerter.notify_startup(bal)

        self.running = True
        self.polymarket.callback = self.linker.on_polymarket
        await self.polymarket.start()
        asyncio.create_task(self.sportybet.start(self.on_sportybet_update))
        await self.sportybet.wait_until_ready(timeout=15.0)
        await self.linker.start()
        self._cleanup_task = asyncio.create_task(self._page_cleanup_loop())
        try:
            self.linker._reconcile_once()
        except Exception as e:
            logger.error(e)

        logger.success("Bot running")
        while self.running:
            await asyncio.sleep(1)

    async def on_sportybet_update(self, data):
        if not isinstance(data, dict) or data.get("type") != "snapshot":
            return
        if self.linker.running:
            self.linker._reconcile_once()

    async def on_new_link(self, sb_match: dict):
        mid = sb_match.get("match_id")
        if not mid or mid in self.match_pages or mid in self._opening_pages:
            return
        if len(self.match_pages) >= getattr(Config, "MAX_PREOPENED_MATCH_PAGES", 8):
            return
        self._opening_pages.add(mid)
        try:
            await self._open_match_page(sb_match)
        finally:
            self._opening_pages.discard(mid)

    async def on_unlink(self, sb_match: dict):
        mid = sb_match.get("match_id")
        if mid:
            await self._close_match_page(mid, reason="unlinked")

    async def _close_match_page(self, mid: str, reason: str = ""):
        page = self.match_pages.pop(mid, None)
        ctx = self._page_contexts.pop(mid, None)
        if self.executor:
            try:
                self.executor.stop_watching(mid)
            except Exception:
                pass
        self._processing.pop(mid, None)
        if page is not None:
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass
        if page is not None or ctx is not None:
            logger.info(
                f"[CLEANUP] closed {mid} ({reason}) open={len(self.match_pages)}"
            )

    async def _page_cleanup_loop(self):
        while self.running:
            try:
                await asyncio.sleep(20)
                active = {
                    (v or {}).get("match_id")
                    for v in self.linker.links.values()
                    if v
                }
                for mid in list(self.match_pages.keys()):
                    page = self.match_pages.get(mid)
                    dead = False
                    try:
                        dead = page is None or page.is_closed()
                    except Exception:
                        dead = True
                    if dead or (
                        mid not in active and mid not in self._opening_pages
                    ):
                        await self._close_match_page(mid, reason="cleanup")
                gc.collect()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"[CLEANUP] {e}")

    def _build_match_urls(self, sb_match: dict) -> list:
        live = (sb_match.get("live_url") or "").strip()
        urls = []
        if live:
            urls.append(live)
            if "/ng/sport/" in live:
                urls.append(live.replace("/ng/sport/", "/ng/m/sport/", 1))
        mid = sb_match.get("match_id") or sb_match.get("event_id") or ""
        home = sb_match.get("home_team") or "Home"
        away = sb_match.get("away_team") or "Away"
        country = sb_match.get("country") or "World"
        league = (
            sb_match.get("league") or sb_match.get("tournament_name") or "League"
        )
        eid = str(sb_match.get("event_id") or mid)
        if not eid.startswith("sr:match:"):
            eid = f"sr:match:{self._extract_digits(eid)}"
        path = (
            f"{self._slug(country)}/{self._slug(league)}/"
            f"{self._slug(home)}_vs_{self._slug(away)}/{eid}"
        )
        desktop = f"https://www.sportybet.com/ng/sport/football/live/{path}"
        mobile = (
            f"https://www.sportybet.com/ng/m/sport/football/live/{path}"
            f"?liveChannel=1&navigatedFrom=live"
        )
        for u in (desktop, mobile):
            if u not in urls:
                urls.append(u)
        return urls

    @staticmethod
    def _extract_digits(text: str) -> str:
        m = re.search(r"(\d+)", str(text or ""))
        return m.group(1) if m else ""

    @staticmethod
    def _slug(text: str) -> str:
        text = (text or "").strip()
        text = re.sub(r"[^a-zA-Z0-9]+", "_", text)
        return text.strip("_") or "Unknown"

    async def _open_match_page(self, sb_match: dict) -> bool:
        mid = sb_match.get("match_id")

        feed = {}
        try:
            feed = self.sportybet.matches.get(mid) or {}
        except Exception:
            pass
        merged = dict(feed)
        merged.update({k: v for k, v in (sb_match or {}).items() if v})
        home = (merged.get("home_team") or "").strip()
        away = (merged.get("away_team") or "").strip()
        urls = self._build_match_urls(merged)

        for attempt in range(1, 4):
            new_context = None
            try:
                new_context = await self.browser.new_context(
                    viewport={"width": 1365, "height": 900},
                    locale="en-NG",
                )
                new_page = await new_context.new_page()
                new_page.set_default_timeout(20000)
                try:
                    await new_page.bring_to_front()
                except Exception:
                    pass

                opened = False
                for url in urls:
                    logger.info(
                        f"[PAGE_LOAD] attempt {attempt}/3 {home} vs {away} → {url[:130]}"
                    )
                    await new_page.goto(
                        url, wait_until="domcontentloaded", timeout=45000
                    )
                    await asyncio.sleep(2.0)

                    for sel in (
                        'button:has-text("Accept")',
                        'button:has-text("OK")',
                        'button:has-text("Got it")',
                        'button:has-text("Close")',
                    ):
                        try:
                            loc = new_page.locator(sel)
                            if await loc.count() > 0:
                                await loc.first.click(timeout=600)
                        except Exception:
                            pass

                    ready = await self.executor.wait_match_details_ready(
                        new_page, home=home, away=away, timeout_s=14.0
                    )
                    if ready:
                        opened = True
                        break

                if not opened:
                    logger.warning(
                        f"[PAGE_LOAD] details not ready {attempt}/3 url={new_page.url}"
                    )
                    await new_context.close()
                    await asyncio.sleep(1.0)
                    continue

                await self.executor.open_all_tab(new_page)
                market = await self.executor.prepare_goal_market(new_page)

                self.match_pages[mid] = new_page
                self._page_contexts[mid] = new_context
                self.executor.start_watching(mid, new_page, home, away)

                logger.success(
                    f"[PAGE_READY] {home} vs {away} id={mid} "
                    f"market={market or 'pending'} url={new_page.url}"
                )
                return True

            except Exception as e:
                logger.warning(f"[PAGE_LOAD] attempt {attempt}/3 error: {e}")
                if new_context is not None:
                    try:
                        await new_context.close()
                    except Exception:
                        pass
                await asyncio.sleep(1.0)

        logger.error(f"[PAGE_LOAD_ERROR] {home} vs {away} id={mid}")
        return False

    async def on_goal(self, goal_data: dict):
        try:
            mid = goal_data["match_id"]
            detected_at = goal_data.get("detected_at")

            if mid not in self.match_pages:
                opened = await self._open_match_page(
                    {
                        "match_id": mid,
                        "home_team": goal_data["home_team"],
                        "away_team": goal_data["away_team"],
                    }
                )
                if not opened:
                    return

            if self._processing.get(mid):
                return
            self._processing[mid] = True
            try:
                page = self.match_pages.get(mid)
                if not page or page.is_closed():
                    await self._close_match_page(mid, reason="missing_on_goal")
                    return

                match = {
                    "match_id": mid,
                    "home_team": goal_data["home_team"],
                    "away_team": goal_data["away_team"],
                }
                result = await self.executor.execute(
                    page,
                    match,
                    goal_data["scoring_team"],
                    goal_data["goal_num"],
                    expected_home_score=goal_data["home_score"],
                    expected_away_score=goal_data["away_score"],
                    detected_at=detected_at,
                )
                if result == BetResult.SUCCESS:
                    bet_id = f"{mid}_G{goal_data['goal_num']}_{int(time.time())}"
                    stake = (
                        Config.VALIDATION_STAKE
                        if not self.executor.validation_passed
                        else self.executor.calc_stake(self.executor.last_odds_used)
                    )
                    self.cashout.register(
                        mid, bet_id, stake, f"Goal {goal_data['goal_num']}"
                    )
                    asyncio.create_task(
                        self.cashout.monitor(bet_id, goal_data, page)
                    )
            finally:
                self._processing[mid] = False
        except Exception as e:
            logger.error(f"Goal handling error: {e}")


if __name__ == "__main__":
    bot = ArbitrageBot()
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        logger.info("Stopping...")
