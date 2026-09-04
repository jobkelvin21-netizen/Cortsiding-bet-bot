import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List
from loguru import logger

LEARNING_FILE = Path.home() / '.betbot_learning.json'

class LearningEngine:
    def __init__(self):
        self.data = self.load_data()
        self.patterns = {}
        
    def load_data(self) -> Dict:
        if LEARNING_FILE.exists():
            with open(LEARNING_FILE) as f:
                return json.load(f)
        return {
            'total_bets': 0,
            'wins': 0,
            'losses': 0,
            'league_success': {},
            'time_success': {},
            'odds_success': {},
            'var_cancellations': 0
        }
        
    def save_data(self):
        with open(LEARNING_FILE, 'w') as f:
            json.dump(self.data, f)
            
    def record_bet(self, match_id: str, league: str, odds: float, 
                   time: str, won: bool, var_cancelled: bool = False):
        self.data['total_bets'] += 1
        if won:
            self.data['wins'] += 1
        else:
            self.data['losses'] += 1
        if var_cancelled:
            self.data['var_cancellations'] += 1
            
        if league not in self.data['league_success']:
            self.data['league_success'][league] = {'wins': 0, 'total': 0}
        self.data['league_success'][league]['total'] += 1
        if won:
            self.data['league_success'][league]['wins'] += 1
            
        hour = time.split(':')[0]
        if hour not in self.data['time_success']:
            self.data['time_success'][hour] = {'wins': 0, 'total': 0}
        self.data['time_success'][hour]['total'] += 1
        if won:
            self.data['time_success'][hour]['wins'] += 1
            
        odds_range = f"{int(odds)}-{int(odds)+1}"
        if odds_range not in self.data['odds_success']:
            self.data['odds_success'][odds_range] = {'wins': 0, 'total': 0}
        self.data['odds_success'][odds_range]['total'] += 1
        if won:
            self.data['odds_success'][odds_range]['wins'] += 1
            
        self.save_data()
        
    def should_bet_league(self, league: str) -> float:
        if league not in self.data['league_success']:
            return 0.5
        stats = self.data['league_success'][league]
        if stats['total'] < 5:
            return 0.5
        return stats['wins'] / stats['total']
        
    def get_insights(self) -> str:
        if self.data['total_bets'] < 10:
            return "Learning... need more data"
        win_rate = self.data['wins'] / self.data['total_bets']
        best_league = max(self.data['league_success'].items(), 
                         key=lambda x: x[1]['wins']/max(x[1]['total'],1))
        msg = f"📊 LEARNING INSIGHTS\n\n"
        msg += f"Total Bets: {self.data['total_bets']}\n"
        msg += f"Win Rate: {win_rate:.1%}\n"
        msg += f"Best League: {best_league[0]} ({best_league[1]['wins']}/{best_league[1]['total']})\n"
        msg += f"VAR Cancellations: {self.data['var_cancellations']}"
        return msg
