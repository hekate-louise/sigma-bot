"""과거 통계: 'Nσ 이상 움직인 날' 이후 주가가 어떻게 됐는지.

각 날짜의 σ는 그 전날까지의 LOOKBACK 거래일로만 계산한다 (미래 데이터 미사용).
과거 기록일 뿐이며 미래를 예측하지 않는다.
"""
from dataclasses import dataclass

import pandas as pd

HORIZONS = (1, 5, 20)  # 이후 며칠 수익률을 볼지 (거래일)
HORIZON_NAMES = {1: "다음날", 5: "5일 후", 20: "20일 후"}


@dataclass
class HorizonStats:
    days: int
    count: int
    mean_pct: float
    median_pct: float
    up_ratio: float       # 0~1
    best_pct: float
    worst_pct: float


@dataclass
class Event:
    date: pd.Timestamp
    change_pct: float
    z: float
    forward_pct: dict[int, float | None]


@dataclass
class EventStats:
    ticker: str
    level: int
    down: bool
    start: pd.Timestamp
    end: pd.Timestamp
    events: list[Event]
    horizons: list[HorizonStats]

    @property
    def years(self) -> float:
        return (self.end - self.start).days / 365.25

    @property
    def direction(self) -> str:
        return "하락" if self.down else "상승"

    def summary_line(self) -> str:
        """알림용 한 줄 요약"""
        if not self.events:
            return f"최근 {self.years:.0f}년간 {self.level}σ 이상 {self.direction}한 날 없음"
        parts = [f"최근 {self.years:.0f}년간 {len(self.events)}회"]
        for h in self.horizons:
            if h.days in (1, 20) and h.count:  # 알림에는 다음날, 20일 후만 짧게
                parts.append(f"{HORIZON_NAMES[h.days]} 평균 {h.mean_pct:+.1f}% (상승 {h.up_ratio:.0%})")
        return " · ".join(parts)


def event_stats(
    ticker: str, closes: pd.Series, lookback: int, level: int, down: bool, window: int = 1
) -> EventStats:
    """window=1 이면 하루 변동, N 이면 N일 누적 변동 기준 (σ × √N)."""
    # 오늘(마지막 행)은 아직 확정되지 않았으므로 제외
    hist = closes.iloc[:-1]
    sigma = hist.pct_change().rolling(lookback).std().shift(1) * window ** 0.5
    ret = hist / hist.shift(window) - 1
    z = (ret / sigma).dropna()

    mask = z <= -level if down else z >= level
    if window > 1:
        # 누적은 며칠 연속 조건을 만족하므로, 연속 구간의 첫날만 1회로 센다
        mask = mask & ~mask.shift(1, fill_value=False)
    forwards = {h: hist.shift(-h) / hist - 1 for h in HORIZONS}

    events = []
    for d in z[mask].index:
        fwd = {h: (None if pd.isna(forwards[h][d]) else float(forwards[h][d]) * 100) for h in HORIZONS}
        events.append(Event(d, float(ret[d]) * 100, float(z[d]), fwd))

    horizons = []
    for h in HORIZONS:
        vals = pd.Series([e.forward_pct[h] for e in events if e.forward_pct[h] is not None], dtype=float)
        if vals.empty:
            horizons.append(HorizonStats(h, 0, 0, 0, 0, 0, 0))
            continue
        horizons.append(HorizonStats(
            days=h,
            count=len(vals),
            mean_pct=float(vals.mean()),
            median_pct=float(vals.median()),
            up_ratio=float((vals > 0).mean()),
            best_pct=float(vals.max()),
            worst_pct=float(vals.min()),
        ))

    return EventStats(ticker.upper(), level, down, z.index[0], z.index[-1], events, horizons)


if __name__ == "__main__":
    import sys

    from sigma import get_closes

    t = sys.argv[1] if len(sys.argv) > 1 else "QQQ"
    closes = get_closes(t)
    for lv in (1, 2, 3):
        s = event_stats(t, closes, 252, lv, down=True)
        print(f"{lv}σ↓ {s.summary_line()}")
