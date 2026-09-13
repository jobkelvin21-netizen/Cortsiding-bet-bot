import asyncio
import sys
import time
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

        self.sportybet = SportyBetFeed(poll_interval=2.0)
        self.sportybet.set_alerter(self.alerter)

        self.polymarket = PolymarketFeed(self._noop_callback)

        self.linker = MatchLinker(self.polymarket, self.sportybet.matches, self.alerter)
        self.linker.set_goal_callback(self.on_goal)
        self.linker.set_link_callback(self.on_new_link)  # NEW

        self.executor = None
        self.cashout = CashOutManager(self.alerter)

        # match_id -> Page (pre-opened or opened-on-demand)
        self.match_pages = {}
        # match_id -> True while a page open is in progress, avoids duplicates
        self._opening_pages = set()

        self.running = False
        self._processing = {}
        self.browser = None

    async def _noop_callback(self, match):
        pass

    async def setup(self):
        if not self.account_manager.accounts:
            print("No accounts! Add one:")
            u = input("Username: ")
            p = input("Password: ")
            ph = input("Phone: ")
            self.account_manager.add_account(u, p, ph)

        acc = self.account_manager.get_active()
        if not acc:
            logger.error("No active account")
            sys.exit(1)

    async def start(self):
        await self.setup()

        logger.info("=" * 60)
        logger.info("BOT STARTING - POLYMARKET (goals) + SPORTYBET REST (matching + score) + SPORTYBET BROWSER (betting)")
        logger.info("=" * 60)

        print("\n" + "=" * 60)
        print("SPORTYBET LOGIN (for placing bets)")
        print("=" * 60)
        phone = input("Phone Number: ")
        password = input("Password: ")
        print("=" * 60)

        from playwright.async_api import async_playwright
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.browser = browser

        page = await browser.new_page(viewport={"width": 412, "height": 915})

        logger.info("Opening SportyBet login (for betting)...")
        await page.goto("https://www.sportybet.com/ng/", wait_until="domcontentloaded")
        await asyncio.sleep(2)

        try:
            await page.click('button:has-text("Log In"), a:has-text("Log In")', timeout=5000)
            await asyncio.sleep(1)
        except Exception:
            pass

        logger.info("Logging in...")
        await page.wait_for_selector('input[name="phone"]', state="visible", timeout=15000)
        await page.fill('input[name="phone"]', phone)
        await asyncio.sleep(0.5)

        await page.wait_for_selector('input[name="psd"]', state="visible", timeout=10000)
        await page.fill('input[name="psd"]', password)
        await asyncio.sleep(0.5)

        await page.click('button[name="logIn"]')
        await asyncio.sleep(5)

        logger.success("SportyBet login complete (browser ready for betting)!")

        self.auth.browser = browser
        self.auth.page = page

        self.executor = BetExecutor(self.alerter, self.account_manager, self.sportybet.matches, None)
        self.executor.current_account = self.account_manager.get_active()

        print("\nEnter your current SportyBet balance:")
        bal = float(input("Balance: "))
        self.executor.balance = bal

        await self.alerter.notify_startup(bal)

        logger.info("Starting feeds...")
        self.running = True

        self.polymarket.callback = self.linker.on_polymarket

        await self.polymarket.start()
        asyncio.create_task(self.sportybet.start(self.on_sportybet_update))

        logger.info("Waiting for SportyBet feed...")
        sporty_ready = await self.sportybet.wait_until_ready(timeout=15.0)

        if sporty_ready:
            logger.success(f"SportyBet feed ready: {len(self.sportybet.matches)} live matches loaded.")
        else:
            logger.warning("SportyBet feed did not become ready within 15 seconds.")

        await self.linker.start()

        try:
            self.linker._reconcile_once()
        except Exception as e:
            logger.error(f"Initial match reconciliation error: {e}")

        logger.success("Bot running! Continuously linking matches, pre-opening pages, watching for goals.")

        while self.running:
            await asyncio.sleep(1)

    async def on_sportybet_update(self, data):
        if not isinstance(data, dict):
            return
        if data.get("type") != "snapshot":
            return

        try:
            count = data.get("count", 0)
            logger.debug(f"[SPORTYBET] Snapshot received: {count} live matches")

            if self.linker.running:
                self.linker._reconcile_once()

        except Exception as e:
            logger.error(f"SportyBet update handling error: {e}")

    # =========================================================================
    # NEW: pre-open a match's SportyBet page the moment it's linked, so the
    # goal->bet path at execution time is just click->bet, not navigate->bet.
    # Capped so we don't open unlimited tabs.
    # =========================================================================

    async def on_new_link(self, sb_match: dict):
        mid = sb_match.get("match_id")
        if not mid or mid in self.match_pages or mid in self._opening_pages:
            return

        if len(self.match_pages) >= Config.MAX_PREOPENED_MATCH_PAGES:
            logger.debug(
                f"[PAGE_LOAD_ERROR] Pre-open cap reached ({Config.MAX_PREOPENED_MATCH_PAGES}) — "
                f"match {mid} will open on-demand at goal time instead"
            )
            return

        self._opening_pages.add(mid)
        try:
            await self._open_match_page(sb_match)
        finally:
            self._opening_pages.discard(mid)

    # =========================================================================
    # REAL NAVIGATION: no guessed direct URL — go to the live listing and
    # click the actual match by team name, same technique confirmed working
    # earlier in this project.
    # =========================================================================

    async def _open_match_page(self, sb_match: dict) -> bool:
        mid = sb_match.get("match_id")
        home = sb_match.get("home_team", "")
        away = sb_match.get("away_team", "")

        try:
            new_context = await self.browser.new_context(viewport={"width": 412, "height": 915})
            new_page = await new_context.new_page()

            await new_page.goto(
                "https://www.sportybet.com/ng/sport/football",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            await asyncio.sleep(2)

            try:
                await new_page.click("text=Live", timeout=3000)
                await asyncio.sleep(2)
            except Exception:
                logger.debug(f"[PAGE_LOAD_ERROR] Could not click 'Live' tab for {home} vs {away}, continuing anyway")

            # Search for the match by team name using Playwright's locator
            # API (safe against special characters, no manual CSS building)
            home_word = home.split()[0] if home else ""
            locator = new_page.get_by_text(home_word, exact=False)

            found = False
            try:
                count = await locator.count()
                if count > 0:
                    await locator.first.click(timeout=5000)
                    await asyncio.sleep(2)
                    found = True
            except Exception as e:
                logger.warning(f"[PAGE_LOAD_ERROR] Click on '{home_word}' failed for {home} vs {away}: {e}")

            if not found:
                logger.error(
                    f"[PAGE_LOAD_ERROR] Could not locate '{home}' vs '{away}' on the live listing page — "
                    f"match_id={mid}, will retry at goal-time if needed"
                )
                await new_context.close()
                return False

            try:
                await new_page.click("text=Next Goal", timeout=3000)
                await asyncio.sleep(1.5)
            except Exception:
                logger.debug(f"[PAGE_LOAD_ERROR] 'Next Goal' market tab not found for {home} vs {away}")

            self.match_pages[mid] = new_page
            logger.success(f"[PAGE_READY] Pre-opened SportyBet page for {home} vs {away} (match_id={mid})")
            return True

        except Exception as e:
            logger.error(f"[PAGE_LOAD_ERROR] Failed to open page for {home} vs {away} (match_id={mid}): {e}")
            return False

    async def on_goal(self, goal_data: dict):
        try:
            mid = goal_data["match_id"]

            # Page should already be open from on_new_link. If not
            # (cap reached, earlier failure), open it now as a fallback —
            # slower, but still correct.
            if mid not in self.match_pages:
                logger.warning(f"[PAGE_LOAD_ERROR] No pre-opened page for match {mid} — opening now (slower path)")
                sb_match = {
                    "match_id": mid,
                    "home_team": goal_data["home_team"],
                    "away_team": goal_data["away_team"],
                }
                opened = await self._open_match_page(sb_match)
                if not opened:
                    logger.error(f"Could not open SportyBet page for {mid} — cannot bet on this goal")
                    return

            if self._processing.get(mid):
                return
            self._processing[mid] = True

            try:
                page = self.match_pages.get(mid)
                if not page:
                    logger.warning(f"No open SportyBet page for {mid} — cannot bet")
                    return

                match = {
                    "match_id": mid,
                    "home_team": goal_data["home_team"],
                    "away_team": goal_data["away_team"],
                }

                result = await self.executor.execute(
                    page, match, goal_data["scoring_team"], goal_data["goal_num"],
                    expected_home_score=goal_data["home_score"],
                    expected_away_score=goal_data["away_score"],
                )

                if result == BetResult.SWITCHED:
                    logger.info("Account switch requested")

                elif result == BetResult.SUCCESS:
                    bet_id = f"{mid}_G{goal_data['goal_num']}_{int(time.time())}"
                    stake = (Config.VALIDATION_STAKE if not self.executor.validation_passed
                             else self.executor.calc_stake(self.executor.last_odds_used))
                    self.cashout.register(mid, bet_id, stake, f"Goal {goal_data['goal_num']}")
                    asyncio.create_task(self.cashout.monitor(bet_id, goal_data, page))

                elif result == BetResult.ABORTED_SAFETY:
                    logger.info(f"Bet aborted by safety check for match {mid}")

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
        try:
            asyncio.run(bot.alerter.send_daily_report())
        except Exception:
            pass
