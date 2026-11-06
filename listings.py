#!/usr/bin/env python3
"""
Мгновенные посты о новых листингах монет на Binance и OKX.

Запускается каждые 10 минут (см. .github/workflows/listings.yml).
Берёт свежие объявления Binance (раздел «New Cryptocurrency Listing») и OKX
(раздел новых листингов, сразу на русском), отбирает листинги и запуски
фьючерсов и публикует новые в канал.
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
# my.okx.com — международная (EU) версия OKX; www.okx.com с серверов GitHub
# отдаёт только американские объявления
OKX_LIST = "https://my.okx.com/api/v5/support/announcements?annType=announcements-new-listings"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36")

MAX_AGE_HOURS = 24  # объявления старше суток не публикуем (на случай долгого перерыва)


def http_json(url, payload=None, lang=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"User-Agent": UA}
    if lang:
        headers["Accept-Language"] = lang
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


def okx_kind(title_ru):
    t = title_ru.lower()
    if any(w in t for w in ("миграц", "делист", "delist", "migration", "прекратит")):
        return None
    if any(w in t for w in ("perp", "фьючерс", "бессрочн")):
        return "futures"
    return "spot"


def build_okx_post(item, kind):
    title = item["title"]
    head = "🆕 <b>Новый листинг на OKX</b>" if kind == "spot" else "📈 <b>OKX: новые контракты</b>"
    lines = [head, "", f"<b>{escape_html(title)}</b>", ""]
    lines.append(f'🔗 <a href="{escape_html(item["url"])}">Объявление OKX</a>')
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
            data = json.load(f)
    except FileNotFoundError:
        return {}
    if isinstance(data, list):  # старый формат: только Binance
        return {"binance": data}
    return data


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump({k: v[-300:] for k, v in seen.items()}, f, ensure_ascii=False)


def process(seen, key, items, item_id, item_time_ms, kind_of, build, label):
    """Общая логика для одной биржи. Возвращает число опубликованных постов."""
    if key not in seen:
        # Первый запуск для биржи: запоминаем текущее, ничего не публикуем.
        seen[key] = [item_id(x) for x in items]
        print(f"[{label}] первый запуск: запомнено {len(items)} объявлений")
        return 0
    known = set(seen[key])
    now_ms = time.time() * 1000
    posted = 0
    for x in sorted(items, key=item_time_ms):  # от старых к новым
        xid = item_id(x)
        if xid in known:
            continue
        known.add(xid)
        seen[key].append(xid)
        kind = kind_of(x)
        age_h = (now_ms - item_time_ms(x)) / 3_600_000
        if kind is None or age_h > MAX_AGE_HOURS:
            print(f"[{label}] пропуск: {x['title']}")
            continue
        send(build(x, kind))
        posted += 1
        print(f"[{label}] опубликовано: {x['title']}")
    return posted


def main():
    if not TELEGRAM_BOT_TOKEN or not TARGET:
        print("Не заданы TELEGRAM_BOT_TOKEN и канал/чат", file=sys.stderr)
        sys.exit(1)

    seen = load_seen()
    total = 0
    failed = False

    try:
        data = http_json(BINANCE_LIST)
        articles = data["data"]["catalogs"][0]["articles"]
        total += process(seen, "binance", articles,
                         item_id=lambda a: a["id"],
                         item_time_ms=lambda a: a.get("releaseDate", 0),
                         kind_of=lambda a: classify(a["title"]),
                         build=build_post, label="Binance")
    except Exception as e:
        failed = True
        print(f"[Binance] ошибка: {e}", file=sys.stderr)

    try:
        data = http_json(OKX_LIST, lang="ru-RU")
        details = data["data"][0]["details"]
        total += process(seen, "okx", details,
                         item_id=lambda x: x["url"].split("/help/")[-1],
                         item_time_ms=lambda x: int(x.get("pTime", 0)),
                         kind_of=lambda x: okx_kind(x["title"]),
                         build=build_okx_post, label="OKX")
    except Exception as e:
        failed = True
        print(f"[OKX] ошибка: {e}", file=sys.stderr)

    save_seen(seen)
    print(f"Новых постов: {total}")
    # Ошибку одной биржи не считаем падением всего запуска, иначе GitHub
    # будет слать письма каждые 10 минут. Подробности — в журнале запуска.
    if failed:
        print("Внимание: одна из бирж не ответила (см. выше)")


if __name__ == "__main__":
    main()
