import asyncio
import os
from datetime import datetime
from dotenv import load_dotenv

from config import Config
from core.arb_engine import ArbEngine
from core.detector import OpportunityDetector
from core.stakes import StakeCalculator
from core.timestamp import TimestampValidator
from feeds.flashscore_ws import FlashScoreFeed  # CHANGED: from bet365 to flashscore
from betting.sportybet import SportyBetAPI
from utils.telegram import TelegramNotifier

load_dotenv()

class ArbitrageBot:
    def __init__(self):
        self.config = Config()
        self.feed = FlashScoreFeed(self.process_match)  # CHANGED: from Bet365Feed to FlashScoreFeed
        self.sportybet = SportyBetAPI()
        self.detector = OpportunityDetector()
        self.arb_engine = ArbEngine()
        self.stake_calc = StakeCalculator()
        self.timestamp = TimestampValidator()
        self.telegram = TelegramNotifier()
        self.running = False
        
    async def process_match(self, match_data):
        """Process incoming match data from FlashScore"""
        try:
            # Check for arbitrage opportunity
            opportunity = self.detector.check_opportunity(match_data)
            
            if opportunity and self.timestamp.is_valid(match_data.get('timestamp')):
                # Calculate stakes
                stakes = self.stake_calc.calculate(opportunity)
                
                if stakes:
                    # Log opportunity
                    print(f"[{datetime.now()}] ARBITRAGE FOUND: {match_data.get('home')} vs {match_data.get('away')}")
                    print(f"  Profit: {opportunity.get('profit_percent', 0):.2f}%")
                    
                    # Send notification
                    await self.telegram.send_alert(opportunity, stakes)
                    
                    # Place bets if in real mode
                    if not self.config.TEST_MODE:
                        await self.place_bets(opportunity, stakes)
                        
        except Exception as e:
            print(f"Error processing match: {e}")
            
    async def place_bets(self, opportunity, stakes):
        """Place bets on SportyBet"""
        try:
            result = await self.sportybet.place_bet(
                match_id=opportunity.get('match_id'),
                home_stake=stakes.get('home'),
                away_stake=stakes.get('away'),
                odds=opportunity.get('odds')
            )
            print(f"Bet placed: {result}")
        except Exception as e:
            print(f"Error placing bet: {e}")
            
    async def run(self):
        """Main bot loop"""
        print(f"[{datetime.now()}] Bot starting...")
        print(f"Test mode: {self.config.TEST_MODE}")
        
        self.running = True
        self.feed.start()
        
        try:
            while self.running:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping bot...")
            self.feed.stop()
            
    def stop(self):
        self.running = False
        self.feed.stop()

if __name__ == "__main__":
    bot = ArbitrageBot()
    asyncio.run(bot.run())
