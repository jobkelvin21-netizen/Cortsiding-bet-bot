import asyncio
import json
import websockets
import re
from typing import Callable, Dict, List
from datetime import datetime
import aiohttp


class Bet365Feed:
    def __init__(self, callback: Callable):
    self.callback = callback
        self.running = False
        self.matches: Dict = {}
        self.session_token = None
        self.subscribed_matches = set()
        
    async def get_session_token(self):
        """Get session token from bet365"""
        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.5',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'DNT': '1',
                    'Connection': 'keep-alive',
                    'Upgrade-Insecure-Requests': '1',
                }
                async with session.get('https://www.bet365.com/', headers=headers) as resp:
                    text = await resp.text()
                    # Try to extract session token
                    token_match = re.search(r'sessionToken["\']?\s*:\s*["\']([^"\']+)["\']?', text)
                    if token_match:
                        self.session_token = token_match.group(1)
                        print(f"Got bet365 session token")
        except Exception as e:
            print(f"Token error: {e}")
            
    async def connect(self):
        """Connect to bet365 WebSocket"""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Origin': 'https://www.bet365.com',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        
        # WebSocket extensions for bet365
        extensions = [
            'permessage-deflate; client_max_window_bits=15',
        ]
        subprotocols = ['zap-protocol-v1']
        
        try:
            async with websockets.connect(
                self.ws_url,
                extra_headers=headers,
                extensions=extensions,
                subprotocols=subprotocols,
                ping_interval=None,
                compression=None
            ) as ws:
                print("Bet365 WebSocket connected!")
                
                # Send initial handshake
                await ws.send('__time,')
                await asyncio.sleep(1)
                
                # Subscribe to live football
                await self.subscribe_football(ws)
                
                while self.running:
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=30.0)
                        if message:
                            await self.process_message(ws, message)
                    except asyncio.TimeoutError:
                        # Keep connection alive
                        await ws.send('__time,')
                        await asyncio.sleep(5)
                        
        except Exception as e:
            print(f"Bet365 WebSocket error: {e}")
            await asyncio.sleep(5)
            if self.running:
                await self.connect()
                
    async def subscribe_football(self, ws):
        """Subscribe to football events"""
        try:
            # Subscribe to in-play football
            msg = 'InPlay_20_0'  # Football in-play channel
            await ws.send(f'{msg}')
            print("Subscribed to football in-play")
            
            # Subscribe to specific leagues if needed
            await asyncio.sleep(2)
            
        except Exception as e:
            print(f"Subscribe error: {e}")
            
    async def process_message(self, ws, data: str):
        """Process bet365 WebSocket message"""
        try:
            # Skip keepalive messages
            if data == 'pong' or data == '__time':
                return
                
            # Parse bet365 packet format
            # Format is typically: packet_id|data_type|match_data...
            if '|' in data:
                await self.parse_packet(data)
            elif data.startswith('OV'):  # Odds update
                await self.parse_odds_update(data)
            elif data.startswith('SC'):  # Score update
                await self.parse_score_update(data)
                
        except Exception as e:
            pass
            
    async def parse_packet(self, data: str):
        """Parse bet365 data packet"""
        try:
            parts = data.split('|')
            if len(parts) < 3:
                return
                
            packet_type = parts[0]
            
            # Live match data
            if packet_type in ['OV', 'SC', 'TG']:
                match_data = self.extract_match_data(parts)
                if match_data and self.callback:
                    await self.callback(match_data)
                    
        except Exception as e:
            pass
            
    def extract_match_data(self, parts: List[str]) -> Dict:
        """Extract match data from bet365 packet"""
        try:
            # This is simplified - bet365 format changes
            match_id = parts[1] if len(parts) > 1 else 'unknown'
            
            # Try to find team names and scores
            home_team = 'Home'
            away_team = 'Away'
            home_score = 0
            away_score = 0
            
            # Parse from packet parts
            for i, part in enumerate(parts):
                if 'v' in part and i < len(parts) - 1:
                    teams = part.split(' v ')
                    if len(teams) == 2:
                        home_team = teams[0]
                        away_team = teams[1]
                        
                # Look for scores
                if part.isdigit() and i > 2:
                    if home_score == 0:
                        home_score = int(part)
                    else:
                        away_score = int(part)
                        
            return {
                'id': match_id,
                'match_id': match_id,
                'home': home_team,
                'away': away_team,
                'home_team': home_team,
                'away_team': away_team,
                'home_score': home_score,
                'away_score': away_score,
                'league': 'In-Play',
                'timestamp': datetime.now()
            }
        except:
            return None
            
    async def parse_odds_update(self, data: str):
        """Parse odds update"""
        pass  # Implement if needed
        
    async def parse_score_update(self, data: str):
        """Parse score update"""
        try:
            # Score updates usually contain goal notifications
            if self.callback:
                # Extract match info and notify
                pass
        except:
            pass
            
    def start(self):
        """Start the feed"""
        self.running = True
        asyncio.create_task(self.connect())
        
    def stop(self):
        """Stop the feed"""
        self.running = False
        
    def get_matches(self) -> List[Dict]:
        """Return current matches"""
        return list(self.matches.values())
