"""한국투자증권(KIS) Open API 현재가 조회. 시세 조회만 하고 주문 API는 쓰지 않는다.

  - 거래소 코드: 프리장·정규장·애프터장은 NAS/NYS/AMS, 주간거래는 BAQ/BAY/BAA
  - 토큰은 하루 유효하고 재발급 횟수 제한이 있어 파일에 저장해 재사용한다 (.gitignore 대상)
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

BASE_URL = "https://openapi.koreainvestment.com:9443"
TOKEN_FILE = Path(__file__).with_name(".kis_token.json")
DAY_CODES = {"NAS": "BAQ", "NYS": "BAY", "AMS": "BAA"}  # 정규 거래소 → 주간거래 코드
TICKER_RE = re.compile(r"^[A-Z]+(\.[A-Z]+)?$")  # ^NDX, NQ=F, 005930.KS 등은 KIS 미지원으로 취급

MIN_CALL_GAP = 0.6  # 초

_lock = threading.Lock()
_throttle_lock = threading.Lock()
_last_call = 0.0
_exchange: dict[str, str] = {}  # {티커: 정규 거래소 코드}


class KisError(RuntimeError):
    pass


@dataclass
class Quote:
    ticker: str
    price: float
    base: float      # KIS가 준 기준가(직전 정규장 종가)
    code: str        # 실제 조회한 거래소 코드


def enabled() -> bool:
    return bool(os.getenv("KIS_APP_KEY") and os.getenv("KIS_APP_SECRET"))


def supports(ticker: str) -> bool:
    return enabled() and bool(TICKER_RE.match(ticker))


def _call(req: urllib.request.Request) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return res.status, json.load(res)
    except urllib.error.HTTPError as ex:
        try:
            return ex.code, json.loads(ex.read().decode("utf-8", "replace"))
        except ValueError:
            return ex.code, {}
    except (urllib.error.URLError, TimeoutError, ValueError) as ex:
        raise KisError(f"KIS 연결 실패: {type(ex).__name__}") from None


def _token(force_new: bool = False) -> str:
    with _lock:
        if not force_new and TOKEN_FILE.exists():
            try:
                saved = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
                if saved["expires_at"] > time.time() + 300:
                    return saved["token"]
            except (ValueError, KeyError):
                pass
        status, data = _call(urllib.request.Request(
            BASE_URL + "/oauth2/tokenP",
            data=json.dumps({
                "grant_type": "client_credentials",
                "appkey": os.environ["KIS_APP_KEY"],
                "appsecret": os.environ["KIS_APP_SECRET"],
            }).encode(),
            headers={"content-type": "application/json"},
        ))
        token = data.get("access_token")
        if not token:
            raise KisError(f"KIS 토큰 발급 실패 (HTTP {status}, {data.get('error_code', '')})")
        TOKEN_FILE.write_text(
            json.dumps({"token": token, "expires_at": time.time() + int(data.get("expires_in", 86400))}),
            encoding="utf-8",
        )
        return token


def _throttle() -> None:
    """초당 호출 제한(EGW00201)을 넘지 않도록 호출 간격을 둔다."""
    global _last_call
    with _throttle_lock:
        wait = _last_call + MIN_CALL_GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _price(ticker: str, code: str, retry: bool = True) -> dict:
    query = urllib.parse.urlencode({"AUTH": "", "EXCD": code, "SYMB": ticker})
    _throttle()
    status, data = _call(urllib.request.Request(
        f"{BASE_URL}/uapi/overseas-price/v1/quotations/price?{query}",
        headers={
            "authorization": f"Bearer {_token()}",
            "appkey": os.environ["KIS_APP_KEY"],
            "appsecret": os.environ["KIS_APP_SECRET"],
            "tr_id": "HHDFS00000300",
            "custtype": "P",
            "content-type": "application/json; charset=utf-8",
        },
    ))
    if status == 401 or data.get("msg_cd") == "EGW00123":  # 토큰 만료
        if retry:
            _token(force_new=True)
            return _price(ticker, code, retry=False)
    if data.get("rt_cd") != "0":
        raise KisError(f"KIS 시세 조회 실패 (HTTP {status}, {data.get('msg_cd', '')})")
    return data.get("output") or {}


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _regular_exchange(ticker: str) -> str:
    """티커가 어느 거래소인지 찾아 기억한다."""
    if ticker in _exchange:
        return _exchange[ticker]
    for code in DAY_CODES:
        if _to_float(_price(ticker, code).get("last")) > 0:
            _exchange[ticker] = code
            return code
    raise KisError(f"{ticker}: KIS에서 찾을 수 없는 티커")


def get_quote(ticker: str, session: str) -> Quote:
    """session: pre / regular / after / day"""
    regular = _regular_exchange(ticker)
    code = DAY_CODES[regular] if session == "day" else regular
    out = _price(ticker, code)
    price = _to_float(out.get("last"))
    if price <= 0:
        raise KisError(f"{ticker}: {code} 현재가 없음")
    return Quote(ticker, price, _to_float(out.get("base")), code)
