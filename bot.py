"""QQQ(나스닥) 시그마 이탈 경고 디스코드 봇.

장중 CHECK_INTERVAL_MIN 분마다 시세를 확인해
  - 전일 대비 변동이 1σ, 2σ, 3σ ... 를 처음 넘을 때
  - N일 누적 변동이 Nσ를 처음 넘을 때 (하루 변동 알림보다 높은 단계일 때만)
채널에 경고(과거 통계 한 줄 + 차트)를 보낸다.
같은 날 같은 종류·방향·단계 알림은 한 번만 보낸다.
"""
import asyncio
import io
import json
import os
from datetime import date, datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from chart import make_chart
from sigma import ET, SigmaResult, compute_sigma, get_closes, is_market_open
from stats import HORIZON_NAMES, event_stats

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
TICKERS = [t.strip().upper() for t in os.getenv("TICKERS", "QQQ").split(",") if t.strip()]
LOOKBACK = int(os.getenv("LOOKBACK_DAYS", "252"))
MIN_LOOKBACK, MAX_LOOKBACK = 5, 1000  # 1000거래일 ≈ 4년 (시세는 10년치 조회)
CUMULATIVE_DAYS = tuple(int(d) for d in os.getenv("CUMULATIVE_DAYS", "3,5").split(",") if d.strip())
THRESHOLD = int(os.getenv("SIGMA_THRESHOLD", "1"))
INTERVAL = float(os.getenv("CHECK_INTERVAL_MIN", "5"))
ALERT_ROLE_ID = os.getenv("ALERT_ROLE_ID")  # 비워두면 멘션 없음
MARKET_HOURS_ONLY = os.getenv("MARKET_HOURS_ONLY", "true").lower() == "true"

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# {(날짜, 티커, 종류, 방향): 이미 알린 최고 시그마 단계}  종류: "1d", "3d", "5d" ...
alerted: dict[tuple, int] = {}

# /period, /watch 로 바꾼 값은 settings.json 에 저장해 재시작 후에도 유지 (.env 값보다 우선)
SETTINGS_FILE = Path(__file__).with_name("settings.json")
if SETTINGS_FILE.exists():
    _saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    LOOKBACK = _saved.get("lookback", LOOKBACK)
    TICKERS = _saved.get("tickers", TICKERS)


def save_settings() -> None:
    SETTINGS_FILE.write_text(
        json.dumps({"lookback": LOOKBACK, "tickers": TICKERS}, ensure_ascii=False), encoding="utf-8"
    )


def describe_days(days: int) -> str:
    """거래일 수를 대략적인 달력 기간으로 표시"""
    months = days / 21
    return f"약 {months / 12:.1f}년" if months >= 12 else f"약 {months:.1f}개월"


def level_color(level: int) -> discord.Color:
    if level >= 3:
        return discord.Color.dark_red()
    if level >= 2:
        return discord.Color.red()
    if level >= 1:
        return discord.Color.orange()
    return discord.Color.green()


def chart_file(ticker: str, closes, lookback: int) -> discord.File:
    return discord.File(io.BytesIO(make_chart(ticker, closes, lookback)), filename="chart.png")


LOC_SIGMA = 2  # /sigma 에 보여줄 LOC 참고가의 σ 단계


def make_embed(
    r: SigmaResult, title: str, lookback: int, show_loc: bool = False, past: bool = False
) -> discord.Embed:
    """past=True 면 지난 날짜 조회: 현재가 대신 종가, LOC 체결 여부 표시"""
    lo1, hi1 = r.band(1)
    lo2, hi2 = r.band(2)
    arrow = "📈" if r.z > 0 else "📉"
    e = discord.Embed(title=title, color=level_color(r.level), timestamp=None if past else datetime.now(ET))
    e.add_field(name="종가" if past else "현재가", value=f"{r.price:,.2f}", inline=True)
    e.add_field(name="전일 대비", value=f"{arrow} {r.change_pct:+.2f}%", inline=True)
    e.add_field(name="시그마", value=f"**{r.z:+.2f}σ**", inline=True)
    e.add_field(name=f"1σ ({lookback}거래일)", value=f"±{r.sigma_pct:.2f}%", inline=True)
    e.add_field(name="1σ 밴드", value=f"{lo1:,.2f} ~ {hi1:,.2f}", inline=True)
    e.add_field(name="2σ 밴드", value=f"{lo2:,.2f} ~ {hi2:,.2f}", inline=True)
    if r.cumulative:
        lines = [f"{c.days}일: {c.change_pct:+.2f}% (**{c.z:+.2f}σ**)" for c in r.cumulative]
        e.add_field(name="누적 변동", value="\n".join(lines), inline=False)
    if show_loc:
        loc, _ = r.band(LOC_SIGMA)
        if past:
            result = (
                f"→ 종가 {r.price:,.2f}, **체결됐을 날** ✅" if r.price <= loc
                else f"→ 종가 {r.price:,.2f}, 미체결 ❌"
            )
        else:
            result = "종가가 이 가격 이하로 끝나야 체결 · 오늘 하루 고정"
        e.add_field(
            name=f"💡 LOC 매수 참고가 (-{LOC_SIGMA}σ)",
            value=(
                f"**{loc:,.2f}**  = 전일 종가 {r.prev_close:,.2f} × (1 − {LOC_SIGMA} × {r.sigma_pct:.2f}%)\n"
                f"{result}"
            ),
            inline=False,
        )
    e.set_footer(text="Yahoo Finance · 무료 시세는 지연될 수 있음")
    return e


async def send_alert(
    channel: discord.abc.Messageable,
    ticker: str,
    closes,
    embed: discord.Embed,
    level: int,
    down: bool,
    window: int = 1,
) -> None:
    """경고 공통: 과거 통계 한 줄 + 차트 첨부"""
    s = await asyncio.to_thread(event_stats, ticker, closes, LOOKBACK, level, down, window)
    what = "하루" if window == 1 else f"{window}일 누적"
    cmd = f"/stats ticker:{ticker} level:{level}" + ("" if window == 1 else f" window:{window}")
    embed.add_field(
        name=f"📊 과거에 {what} {level}σ 이상 {s.direction}한 뒤",
        value=f"{s.summary_line()}\n자세히: `{cmd}`",
        inline=False,
    )
    embed.set_image(url="attachment://chart.png")
    file = await asyncio.to_thread(chart_file, ticker, closes, LOOKBACK)
    mention = f"<@&{ALERT_ROLE_ID}>" if ALERT_ROLE_ID else None
    await channel.send(content=mention, embed=embed, file=file)


def should_alert(kind: str, ticker: str, z: float) -> bool:
    """처음 도달한 단계면 True 를 돌려주고 기록"""
    level = int(abs(z))
    if level < THRESHOLD:
        return False
    key = (datetime.now(ET).date(), ticker, kind, "up" if z > 0 else "down")
    if alerted.get(key, 0) >= level:
        return False  # 이미 이 단계(또는 그 이상)를 알렸음
    alerted[key] = level
    return True


async def check_ticker(ticker: str, channel: discord.abc.Messageable) -> None:
    closes = await asyncio.to_thread(get_closes, ticker)
    r = compute_sigma(ticker, LOOKBACK, CUMULATIVE_DAYS, closes)

    if should_alert("1d", ticker, r.z):
        direction = "상승" if r.z > 0 else "하락"
        embed = make_embed(r, f"⚠️ {ticker} {r.level}σ {direction} 이탈", LOOKBACK)
        await send_alert(channel, ticker, closes, embed, r.level, r.z < 0)

    for c in r.cumulative:
        # 하루 변동만으로 이미 같은 단계 이상이면 누적 알림은 중복이므로 생략
        same_side = (c.z > 0) == (r.z > 0)
        if same_side and r.level >= c.level:
            continue
        if not should_alert(f"{c.days}d", ticker, c.z):
            continue
        direction = "상승" if c.z > 0 else "하락"
        embed = make_embed(r, f"⚠️ {ticker} {c.days}일 누적 {c.level}σ {direction}", LOOKBACK)
        embed.description = (
            f"{c.days}거래일 동안 {c.change_pct:+.2f}% "
            f"({c.days}일 기준 1σ = ±{c.sigma_pct:.2f}%)"
        )
        embed.color = level_color(c.level)
        await send_alert(channel, ticker, closes, embed, c.level, c.z < 0, c.days)


@tasks.loop(minutes=INTERVAL)
async def monitor() -> None:
    if MARKET_HOURS_ONLY and not is_market_open():
        return
    channel = client.get_channel(CHANNEL_ID) or await client.fetch_channel(CHANNEL_ID)
    for t in list(TICKERS):
        try:
            await check_ticker(t, channel)
        except Exception as ex:  # 한 종목 실패가 루프 전체를 멈추지 않도록
            print(f"[{datetime.now(ET):%H:%M}] {t} 확인 실패: {ex}")


def parse_date(text: str) -> date:
    """2026-10-30, 2026.10.30, 20261030, 10-30(올해) 형식 허용"""
    s = text.strip().replace(".", "-").replace("/", "-")
    if s.isdigit() and len(s) == 8:
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    parts = s.split("-")
    if len(parts) == 2:
        parts.insert(0, str(datetime.now(ET).year))
    if len(parts) != 3:
        raise ValueError
    return date(int(parts[0]), int(parts[1]), int(parts[2]))


@tree.command(name="sigma", description="몇 시그마 움직였는지 확인 (날짜를 넣으면 그날 기준)")
@app_commands.describe(
    ticker="티커 (기본 QQQ)",
    days="이번 조회에만 쓸 σ 기간(거래일). 비우면 현재 설정값",
    date="조회할 날짜 (예: 2026-10-30). 비우면 오늘",
)
async def sigma_cmd(
    interaction: discord.Interaction,
    ticker: str = "QQQ",
    days: app_commands.Range[int, MIN_LOOKBACK, MAX_LOOKBACK] | None = None,
    date: str | None = None,
) -> None:
    on = None
    if date:
        try:
            on = parse_date(date)
        except ValueError:
            await interaction.response.send_message("날짜 형식을 확인해 주세요. 예: `2026-10-30`", ephemeral=True)
            return
        if on > datetime.now(ET).date():
            await interaction.response.send_message("미래 날짜는 조회할 수 없어요.", ephemeral=True)
            return

    await interaction.response.defer()
    lookback = days or LOOKBACK
    try:
        r = await asyncio.to_thread(compute_sigma, ticker.upper(), lookback, CUMULATIVE_DAYS, None, on)
    except Exception as ex:
        await interaction.followup.send(f"조회 실패: {ex}")
        return

    actual = r.as_of.date()
    past = on is not None and actual < datetime.now(ET).date()
    if past:
        title = f"{r.ticker} 시그마 ({actual:%Y-%m-%d})"
        if actual != on:
            title += f" · {on:%m/%d}은 휴장일이라 직전 거래일"
    else:
        title = f"{r.ticker} 시그마 현황"
    await interaction.followup.send(embed=make_embed(r, title, lookback, show_loc=True, past=past))


@tree.command(name="stats", description="과거에 Nσ 이상 움직인 날 이후 주가가 어땠는지 자세히 보기")
@app_commands.describe(
    ticker="티커 (기본 QQQ)",
    level="몇 σ 이상 (기본 2)",
    direction="하락 또는 상승 (기본 하락)",
    window="1이면 하루 변동, 3이면 3일 누적 변동 기준 (기본 1)",
)
@app_commands.choices(direction=[
    app_commands.Choice(name="하락", value="down"),
    app_commands.Choice(name="상승", value="up"),
])
async def stats_cmd(
    interaction: discord.Interaction,
    ticker: str = "QQQ",
    level: app_commands.Range[int, 1, 5] = 2,
    direction: app_commands.Choice[str] | None = None,
    window: app_commands.Range[int, 1, 20] = 1,
) -> None:
    await interaction.response.defer()
    ticker = ticker.upper()
    down = direction is None or direction.value == "down"
    try:
        closes = await asyncio.to_thread(get_closes, ticker)
        s = await asyncio.to_thread(event_stats, ticker, closes, LOOKBACK, level, down, window)
    except Exception as ex:
        await interaction.followup.send(f"조회 실패: {ex}")
        return

    what = "하루" if window == 1 else f"{window}일 누적"
    e = discord.Embed(
        title=f"📊 {ticker} {what} {level}σ 이상 {s.direction}한 뒤",
        description=(
            f"기간 {s.start:%Y.%m}~{s.end:%Y.%m} ({s.years:.1f}년) · σ 기준 {LOOKBACK}거래일\n"
            f"총 **{len(s.events)}회** (연 {len(s.events) / s.years:.1f}회)"
        ),
        color=level_color(level),
    )
    for h in s.horizons:
        if not h.count:
            value = "데이터 없음"
        else:
            value = (
                f"평균 **{h.mean_pct:+.2f}%** · 중앙값 {h.median_pct:+.2f}%\n"
                f"상승 확률 **{h.up_ratio:.0%}** ({h.count}회 중)\n"
                f"최고 {h.best_pct:+.1f}% / 최저 {h.worst_pct:+.1f}%"
            )
        e.add_field(name=HORIZON_NAMES[h.days], value=value, inline=True)

    def fmt(v: float | None) -> str:
        return "-" if v is None else f"{v:+.1f}%"

    recent = s.events[-5:][::-1]
    if recent:
        lines = [
            f"`{ev.date:%Y-%m-%d}` {ev.change_pct:+.2f}% ({ev.z:+.1f}σ) → "
            f"다음날 {fmt(ev.forward_pct[1])}, 5일 {fmt(ev.forward_pct[5])}, 20일 {fmt(ev.forward_pct[20])}"
            for ev in recent
        ]
        e.add_field(name="최근 발생 (최신순)", value="\n".join(lines), inline=False)
    e.set_footer(text="과거 기록일 뿐이며 미래 수익을 보장하지 않습니다")
    await interaction.followup.send(embed=e)


@tree.command(name="chart", description="종가와 ±1σ/±2σ 밴드 차트")
@app_commands.describe(ticker="티커 (기본 QQQ)", days="최근 며칠(거래일)을 그릴지 (기본 63 ≈ 3개월)")
async def chart_cmd(
    interaction: discord.Interaction,
    ticker: str = "QQQ",
    days: app_commands.Range[int, 10, 504] = 63,
) -> None:
    await interaction.response.defer()
    ticker = ticker.upper()
    try:
        closes = await asyncio.to_thread(get_closes, ticker)
        png = await asyncio.to_thread(make_chart, ticker, closes, LOOKBACK, days)
    except Exception as ex:
        await interaction.followup.send(f"조회 실패: {ex}")
        return
    await interaction.followup.send(file=discord.File(io.BytesIO(png), filename=f"{ticker}_chart.png"))


@tree.command(name="period", description="자동 알림의 σ 계산 기간(거래일)을 확인하거나 변경")
@app_commands.describe(days="새 기간 (21≈1개월, 63≈3개월, 126≈6개월, 252≈1년). 비우면 현재값만 표시")
@app_commands.default_permissions(manage_guild=True)  # 기본: 서버 관리 권한자만 사용 가능
async def period_cmd(
    interaction: discord.Interaction,
    days: app_commands.Range[int, MIN_LOOKBACK, MAX_LOOKBACK] | None = None,
) -> None:
    global LOOKBACK
    if days is None:
        await interaction.response.send_message(
            f"현재 σ 기간: **{LOOKBACK}거래일** ({describe_days(LOOKBACK)})"
        )
        return
    old = LOOKBACK
    LOOKBACK = days
    save_settings()
    await interaction.response.send_message(
        f"σ 기간 변경: {old}거래일 → **{days}거래일** ({describe_days(days)})\n"
        f"다음 확인부터 새 기준으로 알림을 보냅니다."
    )


watch = app_commands.Group(
    name="watch",
    description="자동 알림 감시 종목 관리",
    default_permissions=discord.Permissions(manage_guild=True),  # 기본: 서버 관리 권한자만
)


@watch.command(name="list", description="감시 중인 종목 보기")
async def watch_list(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(f"감시 종목: **{', '.join(TICKERS) or '없음'}**")


@watch.command(name="add", description="감시 종목 추가")
@app_commands.describe(ticker="추가할 티커 (예: SPY, SOXX, ^NDX)")
async def watch_add(interaction: discord.Interaction, ticker: str) -> None:
    ticker = ticker.strip().upper()
    if ticker in TICKERS:
        await interaction.response.send_message(f"{ticker}는 이미 감시 중입니다.")
        return
    await interaction.response.defer()
    try:  # 실제로 시세가 나오는 티커인지 확인
        await asyncio.to_thread(compute_sigma, ticker, LOOKBACK)
    except Exception as ex:
        await interaction.followup.send(f"{ticker} 추가 실패: {ex}")
        return
    TICKERS.append(ticker)
    save_settings()
    await interaction.followup.send(f"✅ {ticker} 추가. 감시 종목: **{', '.join(TICKERS)}**")


@watch.command(name="remove", description="감시 종목 제거")
@app_commands.describe(ticker="제거할 티커")
async def watch_remove(interaction: discord.Interaction, ticker: str) -> None:
    ticker = ticker.strip().upper()
    if ticker not in TICKERS:
        await interaction.response.send_message(f"{ticker}는 감시 목록에 없습니다.")
        return
    TICKERS.remove(ticker)
    save_settings()
    await interaction.response.send_message(f"🗑️ {ticker} 제거. 감시 종목: **{', '.join(TICKERS) or '없음'}**")


@watch_remove.autocomplete("ticker")
async def watch_remove_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=t, value=t) for t in TICKERS if current.upper() in t][:25]


tree.add_command(watch)


@client.event
async def on_ready() -> None:
    await tree.sync()
    print(
        f"로그인: {client.user} | 감시: {TICKERS} | σ 기간 {LOOKBACK}거래일 | "
        f"누적 {CUMULATIVE_DAYS}일 | {THRESHOLD}σ 이상 | {INTERVAL}분 간격"
    )
    if not monitor.is_running():
        monitor.start()


if __name__ == "__main__":
    client.run(TOKEN)
