"""시그마(표준편차) 계산 로직. 디스코드와 분리되어 있어 단독 테스트 가능.

정의:
  - 기준 변동성(σ): 최근 LOOKBACK 거래일의 '일간 수익률' 표준편차
  - 오늘 변동: 전일 종가 대비 현재가 수익률
  - Z = 오늘 변동 / σ   →  |Z| >= 1 이면 1시그마 이탈
  - N일 누적: N거래일 전 종가 대비 수익률 / (σ × √N)
"""
import math
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore", module="yfinance")  # 라이브러리 내부 경고 숨김

ET = ZoneInfo("America/New_York")
HISTORY_PERIOD = "10y"  # σ 계산(최대 1000거래일)과 과거 통계에 쓰는 기간

# {티커: (받은 날짜, 종가 시리즈)} - 긴 과거 데이터는 하루 한 번만 받는다
_history_cache: dict[str, tuple[date, pd.Series]] = {}


def _download(ticker: str, period: str) -> pd.Series:
    hist = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
    return hist["Close"].dropna()


def get_closes(ticker: str) -> pd.Series:
    """일간 종가. 마지막 행은 오늘(장중이면 현재가에 가까운 값)."""
    ticker = ticker.upper()
    today = datetime.now(ET).date()
    cached = _history_cache.get(ticker)
    if cached is None or cached[0] != today:
        full = _download(ticker, HISTORY_PERIOD)
        if full.empty:
            raise RuntimeError(f"{ticker}: 시세 데이터를 가져오지 못했습니다 (티커 확인)")
        _history_cache[ticker] = (today, full)
    else:
        full = cached[1]

    # 최근 며칠치만 새로 받아 오늘 값을 갱신
    recent = _download(ticker, "5d")
    if recent.empty:
        return full
    return pd.concat([full[full.index < recent.index[0]], recent])


@dataclass
class CumulativeResult:
    days: int
    change_pct: float     # N일 누적 변동률 (%)
    sigma_pct: float      # N일 기준 1σ = 일간 σ × √N (%)
    z: float

    @property
    def level(self) -> int:
        return int(abs(self.z))


@dataclass
class SigmaResult:
    ticker: str
    price: float          # 현재가 (무료 데이터는 약 15분 지연될 수 있음)
    prev_close: float     # 전일 종가
    change_pct: float     # 전일 대비 변동률 (%)
    sigma_pct: float      # 1σ 크기 (%)
    z: float              # 몇 시그마인지 (부호 포함)
    as_of: datetime
    cumulative: list[CumulativeResult] = field(default_factory=list)

    @property
    def level(self) -> int:
        """벗어난 시그마 단계 (0, 1, 2, 3 ...)"""
        return int(abs(self.z))

    def band(self, n: int) -> tuple[float, float]:
        """±nσ 가격 밴드"""
        d = self.prev_close * self.sigma_pct / 100 * n
        return self.prev_close - d, self.prev_close + d


def compute_sigma(
    ticker: str,
    lookback: int = 252,
    cumulative_days: tuple[int, ...] = (),
    closes: pd.Series | None = None,
    on: date | None = None,
) -> SigmaResult:
    """on 을 주면 그 날짜(휴장일이면 직전 거래일)를 '오늘'로 보고 계산한다."""
    if closes is None:
        closes = get_closes(ticker)
    if len(closes) < lookback + 2:
        raise RuntimeError(f"{ticker}: σ를 계산할 과거 데이터({lookback}거래일)가 부족합니다")
    if on is not None:
        earliest = closes.index[lookback + 1].date()
        if on < earliest:
            raise RuntimeError(
                f"{ticker}: {lookback}거래일 기준으로는 {earliest:%Y-%m-%d} 이후만 조회할 수 있어요"
            )
        closes = closes[closes.index.date <= on]

    # 마지막 행 = 오늘(장중이면 실시간에 가까운 값, 장 시작 전이면 직전 거래일).
    # 마지막 행은 σ 계산에서 제외하고, 그 이전 데이터로만 σ를 구한다.
    price = float(closes.iloc[-1])
    past = closes.iloc[:-1]

    prev_close = float(past.iloc[-1])
    returns = past.pct_change().dropna().iloc[-lookback:]
    sigma = float(returns.std())

    cums = []
    for n in cumulative_days:
        base = float(past.iloc[-n])
        change = price / base - 1
        sigma_n = sigma * math.sqrt(n)
        cums.append(CumulativeResult(n, change * 100, sigma_n * 100, change / sigma_n))

    change = price / prev_close - 1
    return SigmaResult(
        ticker=ticker.upper(),
        price=price,
        prev_close=prev_close,
        change_pct=change * 100,
        sigma_pct=sigma * 100,
        z=change / sigma if sigma > 0 else 0.0,
        as_of=closes.index[-1].to_pydatetime(),
        cumulative=cums,
    )


def current_session(now: datetime | None = None) -> tuple[str | None, date]:
    """(세션, 세션 날짜). 공휴일은 고려하지 않음.

    pre 04:00~09:30, regular 09:30~16:00, after 16:00~20:00 (평일 ET)
    day 20:00~04:00 (일요일 밤~금요일 새벽 ET). 자정을 넘겨도 시작한 날짜로 묶는다.
    """
    now = (now or datetime.now(ET)).astimezone(ET)
    today, weekday = now.date(), now.weekday()
    minutes = now.hour * 60 + now.minute
    if minutes < 4 * 60:
        return ("day", today - timedelta(days=1)) if weekday <= 4 else (None, today)
    if minutes >= 20 * 60:
        return ("day", today) if weekday in (6, 0, 1, 2, 3) else (None, today)
    if weekday >= 5:
        return None, today
    if minutes < 9 * 60 + 30:
        return "pre", today
    return ("regular" if minutes < 16 * 60 else "after"), today


def with_live_price(closes: pd.Series, price: float, session: str, now: datetime | None = None) -> pd.Series:
    """종가 시리즈 끝에 실시간 가격을 '오늘' 행으로 붙인다.

    기준가(직전 행): 프리장·정규장·새벽 주간거래는 직전 거래일 종가,
    애프터장·저녁 주간거래는 그날 정규장 종가.
    """
    now = (now or datetime.now(ET)).astimezone(ET)
    today = now.date()
    dates = closes.index.date
    if session == "after" or (session == "day" and now.hour >= 20):
        base = closes[dates <= today]
        if session == "after" and base.index[-1].date() != today:
            raise RuntimeError("오늘 정규장 종가가 아직 없습니다 (휴장일일 수 있음)")
    else:
        base = closes[dates < today]
    return pd.concat([base, pd.Series([price], index=[pd.Timestamp(today, tz=closes.index.tz)])])


if __name__ == "__main__":
    import sys

    for t in sys.argv[1:] or ["QQQ"]:
        r = compute_sigma(t, cumulative_days=(3, 5))
        lo, hi = r.band(1)
        print(
            f"{r.ticker}  현재 {r.price:.2f} / 전일 {r.prev_close:.2f}  "
            f"변동 {r.change_pct:+.2f}%  1σ={r.sigma_pct:.2f}%  Z={r.z:+.2f}  "
            f"(1σ 밴드 {lo:.2f} ~ {hi:.2f})  기준 {r.as_of:%Y-%m-%d}"
        )
        for c in r.cumulative:
            print(f"   {c.days}일 누적 {c.change_pct:+.2f}%  1σ={c.sigma_pct:.2f}%  Z={c.z:+.2f}")
