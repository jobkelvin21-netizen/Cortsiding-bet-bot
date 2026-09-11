# proxies.py
"""
Free proxy pool from Webshare (free tier).
Rotated automatically by feeds/bet365_ws.py — if one fails or gets
blocked, the next one in the list is tried. After all 10 fail, the
bot waits and retries from the top (free proxies can recover).
"""

PROXY_LIST = [
    {"server": "http://31.59.20.176:6754", "username": "pzbdjger", "password": "4vfkqiesj5kw"},
    {"server": "http://45.38.107.97:6014", "username": "pzbdjger", "password": "4vfkqiesj5kw"},
    {"server": "http://198.105.121.200:6462", "username": "pzbdjger", "password": "4vfkqiesj5kw"},
    {"server": "http://64.137.96.74:6641", "username": "pzbdjger", "password": "4vfkqiesj5kw"},
    {"server": "http://198.23.243.226:6361", "username": "pzbdjger", "password": "4vfkqiesj5kw"},
    {"server": "http://38.154.185.97:6370", "username": "pzbdjger", "password": "4vfkqiesj5kw"},
    {"server": "http://84.247.60.125:6095", "username": "pzbdjger", "password": "4vfkqiesj5kw"},
    {"server": "http://142.111.67.146:5611", "username": "pzbdjger", "password": "4vfkqiesj5kw"},
    {"server": "http://191.96.254.138:6185", "username": "pzbdjger", "password": "4vfkqiesj5kw"},
    {"server": "http://31.58.9.4:6077", "username": "pzbdjger", "password": "4vfkqiesj5kw"},
]


class ProxyRotator:
    """Cycles through PROXY_LIST, skipping proxies marked bad this run."""

    def __init__(self):
        self.proxies = list(PROXY_LIST)
        self.bad_indices = set()
        self.current_index = 0

    def get_next(self):
        """Return the next untried proxy, or None if all are currently marked bad."""
        attempts = 0
        while attempts < len(self.proxies):
            idx = self.current_index % len(self.proxies)
            self.current_index += 1
            attempts += 1
            if idx not in self.bad_indices:
                return self.proxies[idx], idx
        return None, None

    def mark_bad(self, idx):
        self.bad_indices.add(idx)

    def reset(self):
        """Call this after all proxies failed, to give them another chance later."""
        self.bad_indices.clear()

    def all_exhausted(self):
        return len(self.bad_indices) >= len(self.proxies)
