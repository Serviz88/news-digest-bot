#!/usr/bin/env python3
"""
Рубрика «🔥 Аномальный объём» — по будням, перед открытием рынка США.

Берёт все монеты Binance к USDT, сравнивает объём торгов за последние 24 часа
с объёмом за предыдущие 24 часа и публикует топ-5 монет с самым сильным ростом.

Данные — публичные котировки Binance (data-api.binance.vision), без ключей.
Запускается из .github/workflows/volume.yml.

Переменные окружения:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID (или TELEGRAM_CHAT_ID), CHANNEL_FOOTER
  PREVIEW=true — ничего не отправлять, только напечатать пост в журнал
  FORCE=true   — не проверять время открытия США и «уже публиковали сегодня»
"""

import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TARGET = os.environ.get("TELEGRAM_CHANNEL_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")
FOOTER = os.environ.get("CHANNEL_FOOTER", "")
PREVIEW = os.environ.get("PREVIEW") == "true"
FORCE = os.environ.get("FORCE") == "true"

API = "https://data-api.binance.vision/api/v3"
HERE = os.path.dirname(os.path.abspath(__file__))
LAST_FILE = os.path.join(HERE, "volume_last.txt")

MIN_QUOTE_VOLUME = 2_000_000   # не меньше $2 млн за сутки — отсекаем совсем мёртвые монеты
MIN_RATIO = 2.0                # аномалия — объём хотя бы вдвое больше, чем днём ранее
TOP_N = 5
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "USD1", "XUSD",
           "AEUR", "EURI", "USDE", "PYUSD", "BFUSD", "RLUSD", "UST", "USTC"}

# Не монеты: токенизированные акции/ETF на Binance и «обёрнутые» копии других монет.
# Если появятся новые — дописать сюда.
NOT_COINS = {"WBTC", "WETH", "WBETH", "BETH", "PAXG", "XAUT",
             "SPYB", "QQQB", "TQQQB", "EWYB", "BMNRB", "NVDAB", "TSLAB", "AAPLB",
             "MSFTB", "AMZNB", "GOOGLB", "METAB", "COINB", "MSTRB", "HOODB", "CRCLB"}

NY = ZoneInfo("America/New_York")
MSK = ZoneInfo("Europe/Moscow")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "news-digest-bot/volume"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def candidates():
    tickers = get(f"{API}/ticker/24hr")
    result = []
    for t in tickers:
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in STABLES or base in NOT_COINS or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
            continue
        if float(t["quoteVolume"]) < MIN_QUOTE_VOLUME or int(t.get("count", 0)) == 0:
            continue
        result.append(t)
    return result


def volume_stats(ticker):
    """Объём за 24ч против предыдущих 24ч (по часовым свечам) и доля покупок."""
    sym = ticker["symbol"]
    kl = get(f"{API}/klines?symbol={sym}&interval=1h&limit=49")[:-1]  # без текущей неполной свечи
    if len(kl) < 48:
        return None
    last, prev = kl[-24:], kl[-48:-24]
    v_last = sum(float(k[7]) for k in last)
    v_prev = sum(float(k[7]) for k in prev)
    if v_prev <= 0:
        return None
    buy_share = sum(float(k[10]) for k in last) / v_last * 100 if v_last else 50
    price_change = (float(last[-1][4]) - float(last[0][1])) / float(last[0][1]) * 100
    return {
        "base": sym[:-4],
        "symbol": sym,
        "ratio": v_last / v_prev,
        "volume": v_last,
        "price_change": price_change,
        "buy_share": buy_share,
    }


def fires(ratio):
    if ratio >= 10:
        return "🔥🔥🔥🔥🔥"
    if ratio >= 6:
        return "🔥🔥🔥🔥"
    if ratio >= 4:
        return "🔥🔥🔥"
    if ratio >= 3:
        return "🔥🔥"
    return "🔥"


def comment(s):
    p, b = s["price_change"], s["buy_share"]
    if abs(p) < 2:
        return "объём растёт, а цена стоит — часто так бывает перед сильным ходом"
    if p > 0 and b >= 55:
        return "покупатели активно заходят"
    if p < 0 and b <= 45:
        return "идут крупные продажи"
    if p > 0:
        return "цена растёт на повышенном интересе"
    return "цена снижается при высоком объёме"


def fmt_num(x, digits=1):
    return f"{x:.{digits}f}".replace(".", ",")


def fmt_money(v):
    if v >= 1e9:
        return f"${fmt_num(v / 1e9)} млрд"
    return f"${fmt_num(v / 1e6)} млн"


def esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_post(top):
    date = datetime.now(MSK).strftime("%d.%m.%Y")
    lines = [
        "🔥🔥🔥 <b>АНОМАЛЬНЫЙ ОБЪЁМ</b> 🔥🔥🔥",
        f"<i>{date} · перед открытием рынка США</i>",
        "",
        "Где сегодня торгуют больше всего по сравнению со вчерашним днём:",
        "",
    ]
    for s in top:
        sign = "+" if s["price_change"] > 0 else ""
        link = f"https://www.binance.com/ru/trade/{s['base']}_USDT"
        lines.append(f'{fires(s["ratio"])} <a href="{link}"><b>${esc(s["base"])}</b></a> — объём <b>×{fmt_num(s["ratio"])}</b>')
        lines.append(f"Цена: {sign}{fmt_num(s['price_change'])}% · Покупки: {s['buy_share']:.0f}% · {fmt_money(s['volume'])}")
        lines.append(f"💬 {comment(s)}")
        lines.append("")
    lines.append("<i>Объём за 24 часа против предыдущих 24 часов, Binance. "
                 "Аномальный объём — сигнал внимания, а не сигнал на покупку. "
                 "Не является индивидуальной инвестиционной рекомендацией.</i>")
    if FOOTER:
        lines.append("")
        lines.append(esc(FOOTER))
    return "\n".join(lines)


def send(text):
    body = json.dumps({"chat_id": TARGET, "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def right_time():
    """Публикуем примерно за час до открытия США (09:30 по Нью-Йорку).
    Расписание стоит на двух временах — для зимнего и летнего времени США;
    лишний запуск этой проверкой пропускается."""
    ny = datetime.now(NY)
    if ny.weekday() >= 5:  # суббота и воскресенье — рынок США закрыт
        return False
    minutes = ny.hour * 60 + ny.minute
    return 8 * 60 + 15 <= minutes <= 9 * 60 + 29


def main():
    today = datetime.now(MSK).strftime("%Y-%m-%d")
    if not FORCE and not PREVIEW:
        if not right_time():
            print("Сейчас не время (ждём ~час до открытия США), пропуск")
            return
        if os.path.exists(LAST_FILE) and open(LAST_FILE).read().strip() == today:
            print("Сегодня уже публиковали, пропуск")
            return

    cands = candidates()
    print(f"Монет для проверки: {len(cands)}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        stats = [s for s in pool.map(lambda t: _safe(volume_stats, t), cands) if s]
    stats.sort(key=lambda s: s["ratio"], reverse=True)
    top = [s for s in stats if s["ratio"] >= MIN_RATIO][:TOP_N]
    for s in stats[:10]:
        print(f"{s['base']}: ×{s['ratio']:.2f}, цена {s['price_change']:+.1f}%, покупки {s['buy_share']:.0f}%")

    if not top:
        print("Сегодня нет монет с объёмом хотя бы ×2 — пост не публикуем")
        return

    post = build_post(top)
    if PREVIEW:
        print("----- ПОСТ -----")
        print(post)
        return
    send(post)
    with open(LAST_FILE, "w") as f:
        f.write(today + "\n")
    print(f"Опубликовано в {TARGET}")


def _safe(fn, arg):
    try:
        return fn(arg)
    except Exception as e:
        print(f"{arg.get('symbol')}: {e}", file=sys.stderr)
        return None


if __name__ == "__main__":
    main()
