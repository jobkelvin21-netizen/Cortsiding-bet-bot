import asyncio
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

        logger.info("=" * 60)
        logger.info(
            "BOT STARTING — Polymarket goals + SportyBet REST linking + "
            "DOM next-goal betting"
        )
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
        await page.goto("https://www.sportybet.com/ng/", wait_until="domcontentloaded")
        await asyncio.sleep(2)

        try:
            await page.click('button:has-text("Log In"), a:has-text("Log In")', timeout=5000)
            await asyncio.sleep(1)
        except Exception:
            pass

        await page.wait_for_selector('input[name="phone"]', state="visible", timeout=15000)
        await page.fill('input[name="phone"]', phone)
        await asyncio.sleep(0.4)
        await page.wait_for_selector('input[name="psd"]', state="visible", timeout=10000)
        await page.fill('input[name="psd"]', password)
        await asyncio.sleep(0.4)
        await page.click('button[name="logIn"]')
        await asyncio.sleep(5)
        logger.success("SportyBet login complete")

        self.auth.browser = browser
        self.auth.page = page

        self.executor = BetExecutor(
            self.alerter, self.account_manager, self.sportybet.matches, None
        )
        self.executor.current_account = self.account_manager.get_active()

        print("\nEnter your current SportyBet balance:")
        bal = float(input("Balance: "))
        self.executor.balance = bal
        await self.alerter.notify_startup(bal)

        self.running = True
        self.polymarket.callback = self.linker.on_polymarket
        await self.polymarket.start()
        asyncio.create_task(self.sportybet.start(self.on_sportybet_update))

        sporty_ready = await self.sportybet.wait_until_ready(timeout=15.0)
        if sporty_ready:
            logger.success(f"SportyBet feed ready: {len(self.sportybet.matches)} matches")
        else:
            logger.warning("SportyBet feed not ready within 15s")

        await self.linker.start()
        self._cleanup_task = asyncio.create_task(self._page_cleanup_loop())
        try:
            self.linker._reconcile_once()
        except Exception as e:
            logger.error(f"Initial reconcile error: {e}")

        logger.success("Bot running")
        while self.running:
            await asyncio.sleep(1)

    async def on_sportybet_update(self, data):
        if not isinstance(data, dict) or data.get("type") != "snapshot":
            return
        try:
            if self.linker.running:
                self.linker._reconcile_once()
        except Exception as e:
            logger.error(f"SportyBet update error: {e}")

    async def on_new_link(self, sb_match: dict):
        mid = sb_match.get("match_id")
        if not mid or mid in self.match_pages or mid in self._opening_pages:
            return
        if len(self.match_pages) >= Config.MAX_PREOPENED_MATCH_PAGES:
            logger.debug(f"Pre-open cap reached — skip {mid}")
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
                f"[CLEANUP] Closed page match_id={mid} ({reason}) | "
                f"open={len(self.match_pages)}"
            )

        try:
            for b in list(self.cashout.bets_for_match(mid)):
                self.cashout.active_bets.pop(b.get("bet_id"), None)
        except Exception:
            pass

    async def _page_cleanup_loop(self):
        while self.running:
            try:
                await asyncio.sleep(20)
                active_ids = {
                    (v or {}).get("match_id") for v in self.linker.links.values() if v
                }
                for mid in list(self.match_pages.keys()):
                    page = self.match_pages.get(mid)
                    dead = False
                    try:
                        dead = page is None or page.is_closed()
                    except Exception:
                        dead = True
                    if dead:
                        await self._close_match_page(mid, reason="dead_page")
                        continue
                    if mid not in active_ids and mid not in self._opening_pages:
                        await self._close_match_page(mid, reason="not_linked")
                gc.collect()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"[CLEANUP] {e}")

    async def _open_match_page(self, sb_match: dict) -> bool:
        """
        Stronger open path: retries + waits so DOM is usable
        (addresses earlier 'could not read/open page' failures).
        """
        mid = sb_match.get("match_id")
        home = sb_match.get("home_team", "")
        away = sb_match.get("away_team", "")

        last_err = None
        for attempt in range(1, 4):
            new_context = None
            try:
                new_context = await self.browser.new_context(
                    viewport={"width": 412, "height": 915}
                )
                new_page = await new_context.new_page()
                new_page.set_default_timeout(15000)

                await new_page.goto(
                    "https://www.sportybet.com/ng/sport/football/live_list",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                try:
                    await new_page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                await asyncio.sleep(2.0)

                # Scroll to load lazy rows
                for _ in range(4):
                    await new_page.evaluate("window.scrollBy(0, 1200)")
                    await asyncio.sleep(0.7)
                await new_page.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(0.5)

                found = await new_page.evaluate(
                    """([home, away]) => {
                        const homeLower = (home || '').toLowerCase();
                        const awayLower = (away || '').toLowerCase();
                        const homeParts = homeLower.split(/[\\s\\-]+/).filter(w => w.length > 2);
                        const awayParts = awayLower.split(/[\\s\\-]+/).filter(w => w.length > 2);
                        const candidates = Array.from(document.querySelectorAll('div, a, span, li'));
                        for (const el of candidates) {
                            const text = (el.innerText || el.textContent || '').toLowerCase();
                            if (text.length < 8 || text.length > 350) continue;
                            const hasHome = homeParts.some(p => text.includes(p));
                            const hasAway = awayParts.some(p => text.includes(p));
                            if (hasHome && hasAway) {
                                el.click();
                                return true;
                            }
                        }
                        // weaker: first tokens
                        const hs = homeLower.split(/\\s+/)[0];
                        const as = awayLower.split(/\\s+/)[0];
                        if (hs && as) {
                            for (const el of candidates) {
                                const text = (el.innerText || '').toLowerCase();
                                if (text.includes(hs) && text.includes(as) && text.length < 350) {
                                    el.click();
                                    return true;
                                }
                            }
                        }
                        return false;
                    }""",
                    [home, away],
                )

                if not found:
                    sample = ""
                    try:
                        sample = await new_page.evaluate(
                            "document.body ? document.body.innerText.slice(0, 900) : ''"
                        )
                    except Exception:
                        pass
                    logger.warning(
                        f"[PAGE_LOAD] attempt {attempt}/3 could not locate "
                        f"'{home}' vs '{away}'. sample={sample[:300]!r}"
                    )
                    await new_context.close()
                    await asyncio.sleep(1.2)
                    continue

                # Wait for match details to render
                await asyncio.sleep(2.2)
                try:
                    await new_page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass

                # Open Goals tab if present, then dynamic next-goal market
                try:
                    goals_tab = new_page.get_by_text("Goals", exact=True)
                    if await goals_tab.count() > 0:
                        await goals_tab.first.click(timeout=2000)
                        await asyncio.sleep(0.8)
                except Exception:
                    pass

                market = await self.executor.prepare_goal_market(new_page) if self.executor else ""
                if not market:
                    # fallback helper in executor still runs later
                    pass

                self.match_pages[mid] = new_page
                self._page_contexts[mid] = new_context

                if self.executor:
                    self.executor.start_watching(mid, new_page, home, away)

                logger.success(
                    f"[PAGE_READY] {home} vs {away} (match_id={mid}) "
                    f"market={market or 'pending'} open={len(self.match_pages)}"
                )
                return True

            except Exception as e:
                last_err = e
                logger.warning(f"[PAGE_LOAD] attempt {attempt}/3 error: {e}")
                if new_context is not None:
                    try:
                        await new_context.close()
                    except Exception:
                        pass
                await asyncio.sleep(1.2)

        logger.error(
            f"[PAGE_LOAD_ERROR] Failed to open '{home}' vs '{away}' "
            f"(match_id={mid}) after retries: {last_err}"
        )
        return False

    async def on_goal(self, goal_data: dict):
        try:
            mid = goal_data["match_id"]
            detected_at = goal_data.get("detected_at")

            if mid not in self.match_pages:
                logger.warning(f"No pre-opened page for {mid} — opening now")
                opened = await self._open_match_page({
                    "match_id": mid,
                    "home_team": goal_data["home_team"],
                    "away_team": goal_data["away_team"],
                })
                if not opened:
                    logger.error(f"Cannot bet — page open failed for {mid}")
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

                # No REST on hot path
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
                    asyncio.create_task(self.cashout.monitor(bet_id, goal_data, page))
                elif result == BetResult.ABORTED_SAFETY:
                    logger.info(f"Safety abort for {mid}")
                elif result == BetResult.SWITCHED:
                    logger.info("Account switch requested")
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
