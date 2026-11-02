"""종가와 ±1σ/±2σ 밴드 차트 (PNG).

각 날짜의 밴드 = 전일 종가 × (1 ± kσ), σ는 그 전날까지의 LOOKBACK 거래일 기준.
한글 폰트가 없는 서버에서도 깨지지 않도록 차트 안 글자는 영어로 쓴다.
"""
import io

import matplotlib

matplotlib.use("Agg")  # 화면 없이 이미지로만 그림
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd


def make_chart(ticker: str, closes: pd.Series, lookback: int, days: int = 63) -> bytes:
    ret = closes.pct_change()
    sigma = ret.rolling(lookback).std().shift(1)
    prev = closes.shift(1)
    z = ret / sigma

    view = slice(-days, None)
    c, p, s, zz = closes.iloc[view], prev.iloc[view], sigma.iloc[view], z.iloc[view]
    x = c.index.tz_localize(None) if c.index.tz is not None else c.index

    fig, ax = plt.subplots(figsize=(8, 4), dpi=110)
    ax.fill_between(x, p * (1 - 2 * s), p * (1 + 2 * s), step="mid", color="#F09595", alpha=0.25, label="±2σ")
    ax.fill_between(x, p * (1 - s), p * (1 + s), step="mid", color="#85B7EB", alpha=0.35, label="±1σ")
    ax.plot(x, c.values, color="#2C2C2A", linewidth=1.6, label="Close")

    lv1 = (zz.abs() >= 1) & (zz.abs() < 2)
    lv2 = zz.abs() >= 2
    ax.scatter(x[lv1.values], c[lv1].values, color="#EF9F27", s=28, zorder=3, label="≥1σ day")
    ax.scatter(x[lv2.values], c[lv2].values, color="#E24B4A", s=40, zorder=3, label="≥2σ day")

    ax.set_title(f"{ticker.upper()}  close vs ±1σ / ±2σ band  ({lookback}d σ)", fontsize=11)
    ax.grid(alpha=0.25)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.08), fontsize=8, ncol=5, frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


if __name__ == "__main__":
    import sys

    from sigma import get_closes

    t = sys.argv[1] if len(sys.argv) > 1 else "QQQ"
    out = sys.argv[2] if len(sys.argv) > 2 else f"{t}_chart.png"
    with open(out, "wb") as f:
        f.write(make_chart(t, get_closes(t), 252))
    print("저장:", out)
