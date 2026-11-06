#!/usr/bin/env python3
"""
Мгновенные посты о новых листингах монет на Binance.

Запускается каждые 10 минут (см. .github/workflows/listings.yml).
Берёт свежие объявления Binance из раздела «New Cryptocurrency Listing»,
отбирает листинги (спот) и запуски фьючерсов и публикует новые в канал.
Какие объявления уже опубликованы — хранится в файле listings_seen.json.

Переменные окружения (GitHub Secrets):
  TELEGRAM_BOT_TOKEN  - токен бота
  TELEGRAM_CHANNEL_ID - канал, например @bullrunyerevan
                        (если не задан — посты идут в TELEGRAM_CHAT_ID)
  TELEGRAM_CHAT_ID    - личный чат владельца
"""

import json
import os
import re
import sys
import time
import urllib.request

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TARGET = os.environ.get("TELEGRAM_CHANNEL_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")

HERE = os.path.dirname(os.path.abspath(__file__))
SEEN_FILE = os.path.join(HERE, "listings_seen.json")

BINANCE_LIST = ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
                "?type=1&pageNo=1&pageSize=20&catalogId=48")
BINANCE_LINK = "https://www.binance.com/ru/support/announcement/detail/{code}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36")

MAX_AGE_HOURS = 24  # объявления старше суток не публикуем (на случай долгого перерыва)


def http_json(url, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"User-Agent": UA}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))


def escape_html(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def classify(title):
    """Что за объявление. None — не публикуем."""
    t = title.lower()
    if "will list" in t:
        return "spot"
    if "futures will launch" in t or ("will launch" in t and "perpetual" in t):
        return "futures"
    if "will add" in t and ("spot" in t or "trading" in t):
        return "spot"
    return None


def tickers(title):
    found = re.findall(r"\(([A-Z0-9]{2,15})\)", title)
    seen, result = set(), []
    for t in found:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


def build_post(article, kind):
    title = article["title"]
    tags = tickers(title)
    if kind == "spot":
        head = "🚨 <b>Новый листинг на Binance</b>"
        what = "Binance добавляет на спот: "
    else:
        head = "📈 <b>Binance: новые фьючерсы</b>"
        what = "Запуск фьючерсных контрактов: "
    lines = [head, ""]
    if tags:
        lines.append(what + ", ".join(f"<b>${escape_html(t)}</b>" for t in tags))
        lines.append("")
    lines.append(f"<i>{escape_html(title)}</i>")
    if "seed tag" in title.lower():
        lines.append("")
        lines.append("⚠️ Seed Tag — новый проект повышенного риска, цена может сильно колебаться.")
    lines.append("")
    lines.append(f'🔗 <a href="{BINANCE_LINK.format(code=article["code"])}">Объявление Binance</a>')
    lines.append("")
    lines.append("<i>Не является индивидуальной инвестиционной рекомендацией.</i>")
    return "\n".join(lines)


def send(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    http_json(url, {
        "chat_id": TARGET,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


def load_seen():
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_seen(ids):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids)[-300:], f)


def main():
    if not TELEGRAM_BOT_TOKEN or not TARGET:
        print("Не заданы TELEGRAM_BOT_TOKEN и канал/чат", file=sys.stderr)
        sys.exit(1)

    data = http_json(BINANCE_LIST)
    articles = data["data"]["catalogs"][0]["articles"]

    seen = load_seen()
    if seen is None:
        # Первый запуск: запоминаем всё текущее, ничего не публикуем,
        # чтобы не завалить канал старыми объявлениями.
        save_seen([a["id"] for a in articles])
        print(f"Первый запуск: запомнено {len(articles)} объявлений, ничего не опубликовано.")
        return

    seen = set(seen)
    now_ms = time.time() * 1000
    posted = 0
    # от старых к новым, чтобы в канале был правильный порядок
    for a in sorted(articles, key=lambda x: x.get("releaseDate", 0)):
        if a["id"] in seen:
            continue
        seen.add(a["id"])
        kind = classify(a["title"])
        age_h = (now_ms - a.get("releaseDate", now_ms)) / 3_600_000
        if kind is None or age_h > MAX_AGE_HOURS:
            print(f"пропуск: {a['title']}")
            continue
        send(build_post(a, kind))
        posted += 1
        print(f"опубликовано: {a['title']}")

    save_seen(seen)
    print(f"Новых постов: {posted}")


if __name__ == "__main__":
    main()
