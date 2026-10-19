"""SportyBet API — login in RAM; place / rebet / cashout; next-goal 1H then FT."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from cryptography.hazmat.primitives import padding as spad
from cryptography.hazmat.primitives.asymmetric import padding as apad
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from config import Config

BASE = Config.SPORTYBET_BASE_URL
UA = (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36"
)
N_HEX = (
    "c207d6f2db3fa03c7545b7e73d97550f30ef5c3019c4ba5f303fe7b1d2059c22"
    "eebde8a8cbd87310e91f84f7ecf697d365dfdb7ad5522b6fb185ea33281cf456"
    "ed845320a2e59cbaf507b1741554a478f7c21e06996018a20f00b0baffc65aeeb"
    "3b36193f6992b0f240ce84bce3bc07a4187d5ba5c5543884a6b624aab0507cf"
)
NEXTGOAL_RE = re.compile(
    r"(next\s*goal|\b\d{1,2}(?:st|nd|rd|th)\s*goals?\b)", re.I
)
GOAL_NUM_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)", re.I)


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _md5h(s: str) -> str:
    return hashlib.md5(str(s).encode()).hexdigest()


def _aes_encrypt(plain: str, key: bytes) -> str:
    iv = secrets.token_bytes(16)
    p = spad.PKCS7(128).padder()
    data = p.update(plain.encode()) + p.finalize()
    c = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return _b64e(iv + c.update(data) + c.finalize())


def _aes_decrypt(payload: str, key: bytes) -> str:
    raw = base64.b64decode(payload.strip())
    d = Cipher(algorithms.AES(key), modes.CBC(raw[:16])).decryptor()
    data = d.update(raw[16:]) + d.finalize()
    try:
        u = spad.PKCS7(128).unpadder()
        return (u.update(data) + u.finalize()).decode()
    except Exception:
        return data.decode("utf-8", "ignore")


def _rsa_pub():
    return RSAPublicNumbers(65537, int(N_HEX, 16)).public_key()


def _period_rank(name: str) -> int:
    n = (name or "").lower()
    if "1st half" in n or "first half" in n:
        return 0
    if "2nd half" in n or "second half" in n:
        return 2
    return 1  # full time / plain next goal


def _goal_num_in_name(name: str) -> Optional[int]:
    m = GOAL_NUM_RE.search(name or "")
    return int(m.group(1)) if m else None


@dataclass
class ArmedSelection:
    event_id: str
    sport_id: str
    market_id: str
    outcome_id: str
    specifier: str
    odds: float
    probability: float
    product: str
    side: str
    market_name: str = ""
    outcome_name: str = ""
    goal_no: Optional[int] = None
    period_rank: int = 1


class SportyBetClient:
    def __init__(self):
        self.cookie = ""
        self.user_id = ""
        self.logged_in = False

    def _headers(self, extra: Optional[dict] = None) -> dict:
        h = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Origin": BASE,
            "Referer": BASE + "/ng/m/",
            "Platform": "wap",
            "ClientId": "wap",
            "OperId": "2",
        }
        if self.cookie:
            h["Cookie"] = self.cookie
        if extra:
            h.update(extra)
        return h

    def _post(self, path: str, data: bytes, headers: dict) -> Tuple[int, bytes]:
        req = urllib.request.Request(BASE + path, data=data, method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read() if e.fp else b""

    def _getjson(self, path: str, params: Optional[dict] = None) -> dict:
        url = BASE + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    def _phone_variants(self, p: str) -> List[str]:
        d = "".join(c for c in str(p) if c.isdigit())
        bare = d.lstrip("0")
        if bare.startswith("234"):
            bare = bare[3:]
        out = []
        for v in (d, bare, "234" + bare, "0" + bare):
            if v and v not in out:
                out.append(v)
        return out

    def open_channel(self) -> Tuple[Optional[bytes], Optional[str]]:
        aes = secrets.token_bytes(16)
        plain = "password=" + urllib.parse.quote(_b64e(aes), safe="")
        wrapped = _b64e(_rsa_pub().encrypt(plain.encode(), apad.PKCS1v15()))
        st, body = self._post(
            "/api/ng/base/cipher",
            wrapped.encode(),
            self._headers({"Content-Type": "text/plain"}),
        )
        cj = json.loads(body)
        if cj.get("bizCode") != 10000:
            return None, None
        return aes, cj["data"]["transId"]

    def send_secure(self, path: str, payload: dict) -> dict:
        aes, tid = self.open_channel()
        if not aes or not tid:
            return {"bizCode": -1, "message": "cipher failed"}
        js = json.dumps(payload, separators=(",", ":"))
        st, body = self._post(
            path,
            _aes_encrypt(js, aes).encode(),
            self._headers(
                {
                    "Content-Type": "application/json;charset=UTF-8",
                    "encryption-type": "2",
                    "transId": tid,
                }
            ),
        )
        try:
            return json.loads(_aes_decrypt(body.decode(), aes))
        except Exception:
            try:
                return json.loads(body.decode())
            except Exception:
                return {"http": st}

    def login(self, phone: str, password: str) -> bool:
        st, body = self._post(
            "/api/ng/patron/cipher",
            b"",
            self._headers({"Content-Type": "text/plain"}),
        )
        cj = json.loads(body)
        urs = cj["data"]["ursId"]
        key = base64.b64decode(cj["data"]["password"])

        def try_login(u: str):
            device = str(uuid.uuid4())
            form = (
                f"username={urllib.parse.quote(u)}"
                f"&password={urllib.parse.quote(_md5h(password))}"
                f"&keepSignedIn=false"
            )
            st, body = self._post(
                "/api/ng/patron/accessToken",
                _aes_encrypt(form, key).encode(),
                self._headers(
                    {
                        "Content-Type": "text/plain",
                        "encryption-type": "1",
                        "ursId": urs,
                        "Fingerprint": secrets.token_hex(16),
                        "Cookie": f"deviceId={device}",
                    }
                ),
            )
            try:
                resp = json.loads(_aes_decrypt(body.decode(), key))
            except Exception:
                resp = json.loads(body)
            return resp, device

        for v in self._phone_variants(phone):
            resp, device = try_login(v)
            if resp.get("bizCode") == 10000:
                d = resp.get("data") or {}
                self.cookie = (
                    f"deviceId={device}; accessToken={d.get('accessToken', '')}; "
                    f"refreshToken={d.get('refreshToken', '')}; "
                    f"userId={d.get('userId', '')}"
                )
                self.user_id = str(d.get("userId") or "")
                self.logged_in = True
                logger.success("[SPORTY] API login OK (session in memory)")
                return True
        return False

    def fetch_event(self, event_id: str) -> dict:
        for path in ("/api/ng/factsCenter/event", "/api/ng/realSportsGame/getEvent"):
            try:
                j = self._getjson(
                    path, {"eventId": event_id, "_t": int(time.time() * 1000)}
                )
                if j.get("data") or j.get("bizCode") == 10000:
                    return j.get("data") or j
            except Exception:
                pass
        return {}

    def _collect_next_goal_markets(self, data: dict) -> List[dict]:
        markets = []

        def walk(n):
            if isinstance(n, dict):
                name = str(n.get("name") or n.get("marketName") or n.get("desc") or "")
                if NEXTGOAL_RE.search(name) or str(n.get("marketId") or "") in ("8", "14"):
                    markets.append(n)
                for v in n.values():
                    walk(v)
            elif isinstance(n, list):
                for v in n:
                    walk(v)

        walk(data)
        return markets

    def arm_for_side(
        self,
        event_id: str,
        side: str,
        goal_no: Optional[int] = None,
    ) -> Optional[ArmedSelection]:
        """Pick next-goal outcome for home|away. Prefer 1H, then FT. Match goal_no if set."""
        data = self.fetch_event(event_id)
        if not data:
            return None
        candidates: List[ArmedSelection] = []
        for m in self._collect_next_goal_markets(data):
            mname = str(m.get("name") or m.get("marketName") or "Next Goal")
            mid = str(m.get("id") or m.get("marketId") or "")
            product = str(m.get("product") or m.get("productId") or "1")
            sport_id = str(m.get("sportId") or "sr:sport:1")
            spec = str(m.get("specifier") or m.get("specifiers") or "")
            gnum = _goal_num_in_name(mname)
            pr = _period_rank(mname)
            outcomes = m.get("outcomes") or m.get("odds") or m.get("items") or []
            if isinstance(outcomes, dict):
                outcomes = list(outcomes.values())
            for o in outcomes:
                if not isinstance(o, dict):
                    continue
                oname = str(o.get("desc") or o.get("name") or o.get("outcomeName") or "")
                oid = str(o.get("id") or o.get("outcomeId") or "")
                try:
                    odds = float(o.get("odds") or o.get("price") or 0)
                except Exception:
                    odds = 0.0
                if odds <= 1.01 or not oid:
                    continue
                low = oname.lower()
                s = None
                if "home" in low or low in ("1", "h"):
                    s = "home"
                elif "away" in low or low in ("2", "a"):
                    s = "away"
                if s != side:
                    continue
                candidates.append(
                    ArmedSelection(
                        event_id=event_id,
                        sport_id=sport_id,
                        market_id=mid,
                        outcome_id=oid,
                        specifier=spec if isinstance(spec, str) else "",
                        odds=odds,
                        probability=float(o.get("probability") or o.get("prob") or 0),
                        product=product,
                        side=s,
                        market_name=mname,
                        outcome_name=oname,
                        goal_no=gnum,
                        period_rank=pr,
                    )
                )
        if not candidates:
            return None

        def sort_key(a: ArmedSelection):
            # prefer matching goal number, then 1H (0) before FT (1) before 2H (2)
            gn = a.goal_no
            match = 0 if (goal_no is not None and gn == goal_no) else (
                1 if gn is None else 2
            )
            return (match, a.period_rank, -a.odds)

        candidates.sort(key=sort_key)
        return candidates[0]

    def arm_next_goal_both(
        self, event_id: str, goal_no: Optional[int] = None
    ) -> Dict[str, ArmedSelection]:
        out: Dict[str, ArmedSelection] = {}
        for side in ("home", "away"):
            arm = self.arm_for_side(event_id, side, goal_no=goal_no)
            if arm:
                out[side] = arm
        return out

    def stake_for_odds(self, odds: float, balance: float = 0.0) -> float:
        if odds <= 1.01:
            return 0.0
        stake = float(Config.MAX_PROFIT_PER_BET) / (odds - 1.0)
        if balance and balance > 0:
            stake = min(stake, balance)
        return max(10.0, round(stake, 2))

    def place_armed(self, arm: ArmedSelection, stake_ngn: float) -> dict:
        units = int(round(float(stake_ngn) * 10000))
        sid = f"uof:{arm.product}/{arm.sport_id}/{arm.market_id}/{arm.outcome_id}"
        if arm.specifier:
            sid += f"?{arm.specifier}"
        sel: Dict[str, Any] = {
            "eventId": arm.event_id,
            "id": sid,
            "odds": f"{float(arm.odds):.2f}",
            "probability": arm.probability or 0,
            "banker": False,
            "betPosition": 0,
            "sportId": arm.sport_id,
        }
        if arm.specifier:
            sel["specifier"] = arm.specifier
        order = {
            "bizType": 1,
            "orderType": 1,
            "subBizType": 2 if str(arm.product) == "1" else 1,
            "paymentType": 0,
            "actualPayAmount": units,
            "isBonusFactor": False,
            "ticket": {
                "selections": [sel],
                "bets": [
                    {
                        "selectedSystems": [1],
                        "stake": {"value": units},
                        "selectionRefs": [{"selectionIndex": 0}],
                    }
                ],
            },
        }
        return self.send_secure("/api/ng/orders/order", order)

    def place_and_rebet(
        self, arm: ArmedSelection, stake_ngn: float, max_rebet: Optional[int] = None
    ) -> List[dict]:
        n = Config.MAX_REBET if max_rebet is None else max_rebet
        results = [self.place_armed(arm, stake_ngn)]
        if results[0].get("bizCode") != 10000:
            return results
        for _ in range(n):
            time.sleep(0.25)
            r = self.place_armed(arm, stake_ngn)
            results.append(r)
            if r.get("bizCode") != 10000:
                break
        return results

    def cashout_bet(self, bet_id: str, stake: float, amount: float) -> dict:
        aes, tid = self.open_channel()
        if not aes or not tid:
            return {"bizCode": -1, "message": "cipher failed"}

        def send(path, payload):
            js = json.dumps(payload, separators=(",", ":"))
            st, body = self._post(
                path,
                _aes_encrypt(js, aes).encode(),
                self._headers(
                    {
                        "Content-Type": "application/json;charset=UTF-8",
                        "encryption-type": "2",
                        "transId": tid,
                    }
                ),
            )
            try:
                return json.loads(_aes_decrypt(body.decode(), aes))
            except Exception:
                try:
                    return json.loads(body.decode())
                except Exception:
                    return {"http": st}

        send("/api/ng/realSportsGame/cashOut/data", {"betId": bet_id})
        cur = str(float(amount))
        last = {}
        for _ in range(5):
            last = send(
                "/api/ng/realSportsGame/cashOut",
                {
                    "betId": bet_id,
                    "usedStake": str(float(stake)),
                    "isPartial": False,
                    "amount": cur,
                },
            )
            if last.get("bizCode") == 10000:
                return last
            if last.get("bizCode") == 32000:
                new = (last.get("data") or {}).get("maxCashOutAmount")
                if new and str(float(new)) != cur:
                    cur = str(float(new))
                    time.sleep(0.3)
                    continue
            break
        return last

    def cashout_latest_open(self) -> dict:
        try:
            lst = self._getjson(
                "/api/ng/realSportsGame/cashAbleBets",
                {"pageSize": 20, "pageNum": 1, "_t": int(time.time() * 1000)},
            )
        except Exception as e:
            return {"bizCode": -1, "message": str(e)}

        def walk(node, acc=None):
            if acc is None:
                acc = []
            if isinstance(node, dict):
                acc.append(node)
                for v in node.values():
                    walk(v, acc)
            elif isinstance(node, list):
                for v in node:
                    walk(v, acc)
            return acc

        for n in walk(lst.get("data")):
            bid = n.get("id") or n.get("betId")
            if not bid or "bet" not in str(bid):
                continue
            off = self._getjson(
                "/api/ng/realSportsGame/cashAbleBet",
                {"betId": str(bid), "_t": int(time.time() * 1000)},
            )
            data = off.get("data") or {}
            co = data.get("cashOut") or {}
            try:
                amt = float(co.get("maxCashOutAmount") or 0)
                stake = float(data.get("stake") or 0)
            except Exception:
                continue
            if amt > 0:
                return self.cashout_bet(str(bid), stake, amt)
        return {"bizCode": -1, "message": "no cashable offer"}
