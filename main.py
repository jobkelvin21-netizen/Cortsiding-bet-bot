import asyncio
import sys
import time
from loguru import logger

from config import Config
from auth_sportybet_login import SportyBetAuth
from feeds.polymarket_ws import PolymarketFeed
from feeds.sportybet_api import SportyBetFeed
from core.match_linker import MatchLinker
from core.executor import BetExecutor
from core.cashout import CashOutManager
from utils.telegram import TelegramAlerter
from utils.account_manager import AccountManager


class ArbitrageBot:

    def __init__(self):

        self.account_manager = AccountManager()
        self.alerter = TelegramAlerter()
        self.auth = SportyBetAuth()

        # =====================================================================
        # SPORTYBET FEED
        # =====================================================================

        self.sportybet = SportyBetFeed(
            poll_interval=2.0
        )

        self.sportybet.set_alerter(
            self.alerter
        )

        # =====================================================================
        # POLYMARKET FEED
        # =====================================================================

        self.polymarket = PolymarketFeed(
            self._noop_callback
        )

        # =====================================================================
        # MATCH LINKER
        #
        # IMPORTANT:
        # Pass the actual SportyBet matches dictionary.
        # SportyBetFeed now updates this dictionary in-place, so MatchLinker
        # always sees the latest matches.
        # =====================================================================

        self.linker = MatchLinker(
            self.polymarket,
            self.sportybet.matches,
            self.alerter,
        )

        self.linker.set_goal_callback(
            self.on_goal
        )

        # =====================================================================
        # OTHER COMPONENTS
        # =====================================================================

        self.executor = None

        self.cashout = CashOutManager(
            self.alerter
        )

        self.slow_pages = {}

        self.running = False

        self._processing = {}

        self.browser = None

    # =========================================================================
    # POLYMARKET PLACEHOLDER
    # =========================================================================

    async def _noop_callback(self, match):
        pass

    # =========================================================================
    # SETUP
    # =========================================================================

    async def setup(self):

        if not self.account_manager.accounts:

            print("No accounts! Add one:")

            u = input("Username: ")
            p = input("Password: ")
            ph = input("Phone: ")

            self.account_manager.add_account(
                u,
                p,
                ph,
            )

        acc = self.account_manager.get_active()

        if not acc:

            logger.error(
                "No active account"
            )

            sys.exit(1)

    # =========================================================================
    # START
    # =========================================================================

    async def start(self):

        await self.setup()

        logger.info(
            "=" * 60
        )

        logger.info(
            "BOT STARTING - "
            "POLYMARKET (goals) + "
            "SPORTYBET REST (matching) + "
            "SPORTYBET BROWSER (betting)"
        )

        logger.info(
            "=" * 60
        )

        # =====================================================================
        # SPORTYBET LOGIN
        # =====================================================================

        print(
            "\n" + "=" * 60
        )

        print(
            "SPORTYBET LOGIN (for placing bets)"
        )

        print(
            "=" * 60
        )

        phone = input(
            "Phone Number: "
        )

        password = input(
            "Password: "
        )

        print(
            "=" * 60
        )

        # =====================================================================
        # PLAYWRIGHT
        # =====================================================================

        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()

        browser = await playwright.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled"
            ],
        )

        self.browser = browser

        page = await browser.new_page(
            viewport={
                "width": 412,
                "height": 915,
            }
        )

        # =====================================================================
        # OPEN SPORTYBET
        # =====================================================================

        logger.info(
            "Opening SportyBet login (for betting)..."
        )

        await page.goto(
            "https://www.sportybet.com/ng/",
            wait_until="domcontentloaded",
        )

        await asyncio.sleep(2)

        try:

            await page.click(
                'button:has-text("Log In"), '
                'a:has-text("Log In")',
                timeout=5000,
            )

            await asyncio.sleep(1)

        except Exception:
            pass

        # =====================================================================
        # LOGIN
        # =====================================================================

        logger.info(
            "Logging in..."
        )

        await page.wait_for_selector(
            'input[name="phone"]',
            state="visible",
            timeout=15000,
        )

        await page.fill(
            'input[name="phone"]',
            phone,
        )

        await asyncio.sleep(0.5)

        await page.wait_for_selector(
            'input[name="psd"]',
            state="visible",
            timeout=10000,
        )

        await page.fill(
            'input[name="psd"]',
            password,
        )

        await asyncio.sleep(0.5)

        await page.click(
            'button[name="logIn"]'
        )

        await asyncio.sleep(5)

        logger.success(
            "SportyBet login complete "
            "(browser ready for betting)!"
        )

        # =====================================================================
        # AUTH
        # =====================================================================

        self.auth.browser = browser
        self.auth.page = page

        # =====================================================================
        # BET EXECUTOR
        # =====================================================================

        self.executor = BetExecutor(
            self.alerter,
            self.account_manager,
            None,
        )

        self.executor.current_account = (
            self.account_manager.get_active()
        )

        # =====================================================================
        # BALANCE
        # =====================================================================

        print(
            "\nEnter your current SportyBet balance:"
        )

        bal = float(
            input("Balance: ")
        )

        self.executor.balance = bal

        await self.alerter.notify_startup(
            bal
        )

        # =====================================================================
        # START FEEDS
        # =====================================================================

        logger.info(
            "Starting feeds..."
        )

        self.running = True

        # ---------------------------------------------------------------------
        # Polymarket callback
        # ---------------------------------------------------------------------

        self.polymarket.callback = (
            self.linker.on_polymarket
        )

        # ---------------------------------------------------------------------
        # Start Polymarket.
        # ---------------------------------------------------------------------

        await self.polymarket.start()

        # ---------------------------------------------------------------------
        # Start SportyBet REST feed.
        #
        # IMPORTANT:
        # This runs in the background.
        # ---------------------------------------------------------------------

        asyncio.create_task(
            self.sportybet.start(
                self.on_sportybet_update
            )
        )

        # ---------------------------------------------------------------------
        # Wait for SportyBet to receive its first successful response.
        #
        # This gives the linker an initial match list before the bot starts
        # relying on continuous linking.
        # ---------------------------------------------------------------------

        logger.info(
            "Waiting for SportyBet feed..."
        )

        sporty_ready = (
            await self.sportybet.wait_until_ready(
                timeout=15.0
            )
        )

        if sporty_ready:

            logger.success(
                "SportyBet feed ready: "
                f"{len(self.sportybet.matches)} "
                "live matches loaded."
            )

        else:

            logger.warning(
                "SportyBet feed did not become ready "
                "within 15 seconds."
            )

        # ---------------------------------------------------------------------
        # Start MatchLinker.
        # ---------------------------------------------------------------------

        await self.linker.start()

        # ---------------------------------------------------------------------
        # Immediate initial reconciliation.
        #
        # This means we don't have to wait for the first 2-second cycle.
        # ---------------------------------------------------------------------

        try:

            self.linker._reconcile_once()

        except Exception as e:

            logger.error(
                f"Initial match reconciliation error: {e}"
            )

        logger.success(
            "Bot running! Continuously linking matches, "
            "watching for goals."
        )

        # =====================================================================
        # MAIN LOOP
        # =====================================================================

        while self.running:

            await asyncio.sleep(1)

    # =========================================================================
    # SPORTYBET UPDATE
    # =========================================================================

    async def on_sportybet_update(
        self,
        data,
    ):
        """
        Receives SportyBet snapshots.

        SportyBetFeed updates self.sportybet.matches in-place.
        MatchLinker holds a reference to that same dictionary.

        We also trigger an immediate reconciliation instead of waiting for
        the normal background reconciliation interval.
        """

        if not isinstance(data, dict):
            return

        if data.get("type") != "snapshot":
            return

        try:

            count = data.get(
                "count",
                0,
            )

            logger.debug(
                f"[SPORTYBET] Snapshot received: "
                f"{count} live matches"
            )

            # -------------------------------------------------------------
            # Immediately attempt to link new SportyBet matches.
            # -------------------------------------------------------------

            if self.linker.running:

                self.linker._reconcile_once()

        except Exception as e:

            logger.error(
                f"SportyBet update handling error: {e}"
            )

    # =========================================================================
    # GOAL HANDLER
    # =========================================================================

    async def on_goal(
        self,
        goal_data: dict,
    ):

        try:

            mid = goal_data[
                "match_id"
            ]

            # =================================================================
            # OPEN SPORTYBET MATCH PAGE
            # =================================================================

            if mid not in self.slow_pages:

                await self.open_match_page(
                    goal_data
                )

            # =================================================================
            # PREVENT DUPLICATE PROCESSING
            # =================================================================

            if self._processing.get(mid):

                return

            self._processing[mid] = True

            try:

                page = self.slow_pages.get(
                    mid
                )

                if not page:

                    logger.warning(
                        f"No open SportyBet page for "
                        f"{mid} — cannot bet"
                    )

                    return

                # =============================================================
                # MATCH OBJECT
                # =============================================================

                match = {
                    "match_id": mid,

                    "home_team": goal_data[
                        "home_team"
                    ],

                    "away_team": goal_data[
                        "away_team"
                    ],
                }

                # =============================================================
                # EXECUTE BET
                # =============================================================

                result = await self.executor.execute(
                    page,
                    match,
                    goal_data[
                        "scoring_team"
                    ],
                    goal_data[
                        "goal_num"
                    ],
                    expected_home_score=goal_data[
                        "home_score"
                    ],
                    expected_away_score=goal_data[
                        "away_score"
                    ],
                )

                # =============================================================
                # ACCOUNT SWITCH
                # =============================================================

                if result == "SWITCH":

                    logger.info(
                        "Account switch requested"
                    )

                # =============================================================
                # SUCCESSFUL BET
                # =============================================================

                elif result is True:

                    bet_id = (
                        f"{mid}_G"
                        f"{goal_data['goal_num']}_"
                        f"{int(time.time())}"
                    )

                    stake = (
                        Config.VALIDATION_STAKE
                        if not self.executor.validation_passed
                        else self.executor.calc_stake(
                            self.executor.last_odds_used
                        )
                    )

                    self.cashout.register(
                        mid,
                        bet_id,
                        stake,
                        f"Goal {goal_data['goal_num']}",
                    )

                    asyncio.create_task(
                        self.cashout.monitor(
                            bet_id,
                            goal_data,
                            page,
                        )
                    )

            finally:

                self._processing[mid] = False

        except Exception as e:

            logger.error(
                f"Goal handling error: {e}"
            )

    # =========================================================================
    # OPEN SPORTYBET MATCH PAGE
    # =========================================================================

    async def open_match_page(
        self,
        goal_data: dict,
    ):

        mid = goal_data[
            "match_id"
        ]

        try:

            # =================================================================
            # NEW BROWSER CONTEXT
            # =================================================================

            new_context = (
                await self.browser.new_context(
                    viewport={
                        "width": 412,
                        "height": 915,
                    }
                )
            )

            new_page = (
                await new_context.new_page()
            )

            # =================================================================
            # OPEN MATCH
            # =================================================================

            await new_page.goto(
                f"{Config.SPORTYBET_BASE_URL}/ng/m/{mid}"
            )

            await asyncio.sleep(4)

            # =================================================================
            # NEXT GOAL MARKET
            # =================================================================

            try:

                await new_page.click(
                    "text=Next Goal"
                )

                await asyncio.sleep(1.5)

            except Exception:

                pass

            # =================================================================
            # SAVE PAGE
            # =================================================================

            self.slow_pages[
                mid
            ] = new_page

            # =================================================================
            # TELEGRAM
            # =================================================================

            await self.alerter.notify_slow_match_found(
                goal_data[
                    "home_team"
                ],

                goal_data[
                    "away_team"
                ],

                0,
            )

        except Exception as e:

            logger.error(
                f"Page open error for {mid}: {e}"
            )


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":

    bot = ArbitrageBot()

    try:

        asyncio.run(
            bot.start()
        )

    except KeyboardInterrupt:

        logger.info(
            "Stopping..."
        )

        try:

            asyncio.run(
                bot.alerter.send_daily_report()
            )

        except Exception:

            pass
