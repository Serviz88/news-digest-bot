#!/usr/bin/env python3
"""
Ежедневный дайджест новостей в Telegram.

Как работает:
  1. Для каждой темы из topics.json бот забирает свежие новости из бесплатных
     RSS-лент (только за последние сутки).
  2. Дешёвая модель Claude Haiku выбирает самые важные, убирает повторы,
     переводит на русский и пишет суть в одну строку.
  3. Готовый дайджест уходит в Telegram.

Запускается по расписанию (см. .github/workflows/daily-digest.yml).

Нужные переменные окружения (задаются как GitHub Secrets):
  ANTHROPIC_API_KEY   - ключ Anthropic API
  TELEGRAM_BOT_TOKEN  - токен бота от BotFather
  TELEGRAM_CHAT_ID    - твой личный чат: сюда приходят служебные сообщения о сбоях
                        (а если канал не задан — и сам дайджест)
  TELEGRAM_CHANNEL_ID - (необязательно) канал для дайджеста, например @my_channel;
                        бот должен быть администратором канала
"""

import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")        # твой личный чат (служебные сообщения)
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")  # канал, например @my_channel
CHANNEL_FOOTER = os.environ.get("CHANNEL_FOOTER", "")            # подпись внизу поста в канале (необязательно)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
MODEL = "claude-haiku-4-5"

MOSCOW_TZ = timezone(timedelta(hours=3))
FRESH_HOURS = 26            # берём новости не старше этого (сутки + запас)
MAX_ITEMS_TO_MODEL = 120    # сколько заголовков максимум отдаём модели по одной теме
DESC_CHARS = 200            # сколько символов описания статьи отдаём модели
USER_AGENT = "Mozilla/5.0 (compatible; news-digest-bot/2.0)"
DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"


# ---------------------------------------------------------------- настройки

def load_config():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "topics.json"), "r", encoding="utf-8") as f:
        data = json.load(f)
    topics = data.get("topics", [])
    if not topics:
        raise SystemExit("В topics.json нет тем")
    return topics


# ---------------------------------------------------------------- RSS

_FEED_CACHE = {}


def http_get(url, timeout=25):
    # одна и та же лента может быть в нескольких темах — качаем её один раз
    if url not in _FEED_CACHE:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            _FEED_CACHE[url] = resp.read()
    return _FEED_CACHE[url]


def clean_text(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def local_name(tag):
    return tag.rsplit("}", 1)[-1].lower()


def child_text(elem, *names):
    for child in elem:
        if local_name(child.tag) in names and (child.text or "").strip():
            return child.text.strip()
    return ""


def parse_date(value):
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_feed(raw, source_name):
    """Разбирает RSS 2.0 и Atom. Возвращает список новостей."""
    root = ET.fromstring(raw)
    items = []
    for elem in root.iter():
        name = local_name(elem.tag)
        if name not in ("item", "entry"):
            continue
        title = clean_text(child_text(elem, "title"))
        if not title:
            continue
        link = child_text(elem, "link")
        if not link:  # Atom: <link href="..."/>
            for child in elem:
                if local_name(child.tag) == "link" and child.get("href"):
                    if child.get("rel", "alternate") == "alternate":
                        link = child.get("href")
                        break
        desc = clean_text(child_text(elem, "description", "summary", "content"))
        date = parse_date(child_text(elem, "pubdate", "published", "updated", "date"))
        items.append({
            "title": title,
            "link": link.strip(),
            "desc": desc[:DESC_CHARS],
            "date": date,
            "source": source_name,
        })
    return items


def collect_topic_items(topic, problems):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=FRESH_HOURS)
    all_items = []
    seen_titles = set()
    for feed in topic.get("feeds", []):
        name = feed.get("name", "?")
        urls = feed.get("urls") or [feed.get("url")]
        got = None
        for url in [u for u in urls if u]:
            try:
                got = parse_feed(http_get(url), name)
                break
            except Exception as e:  # пробуем запасной адрес
                print(f"[{name}] {url}: {e}", file=sys.stderr)
        if got is None:
            problems.add(name)
            continue
        for it in got:
            if it["date"] is not None and it["date"] < cutoff:
                continue
            key = it["title"].lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            all_items.append(it)

    old = datetime(1970, 1, 1, tzinfo=timezone.utc)
    all_items.sort(key=lambda x: x["date"] or old, reverse=True)
    return all_items[:MAX_ITEMS_TO_MODEL]


# ---------------------------------------------------------------- Claude Haiku

def http_post_json(url, payload, headers, timeout=90):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def extract_json_array(raw_text):
    cleaned = raw_text.replace("```json", "").replace("```", "").strip()
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("в ответе модели нет списка")
    return json.loads(cleaned[start:end + 1])


def pick_top_news(topic, items, already_chosen):
    count = int(topic.get("count", 5))
    lines = []
    for i, it in enumerate(items):
        line = f"{i}. [{it['source']}] {it['title']}"
        if it["desc"]:
            line += f" — {it['desc']}"
        lines.append(line)

    avoid = ""
    if already_chosen:
        avoid = (
            "\nЭти новости уже есть в других разделах дайджеста, НЕ выбирай их "
            "и новости про то же самое:\n- " + "\n- ".join(already_chosen) + "\n"
        )

    system_prompt = (
        "Ты — редактор утреннего новостного дайджеста. Тебе дают пронумерованный "
        "список свежих новостей из нескольких источников.\n"
        f"Раздел дайджеста: «{topic['name']}». {topic.get('focus', '')}\n"
        f"Выбери до {count} самых важных новостей для этого раздела.\n"
        "Правила:\n"
        "- Если одну и ту же новость пишут несколько источников — бери её один раз.\n"
        "- Пропускай рекламу, пресс-релизы, мелкие и развлекательные новости.\n"
        "- title — короткий заголовок на русском (перевод, если источник на английском).\n"
        "- summary — суть на русском, не длиннее 15 слов.\n"
        "Ответь ТОЛЬКО JSON-массивом без текста до или после, формат элемента:\n"
        '{"id": <номер новости из списка>, "title": "...", "summary": "..."}\n'
        "Если подходящих новостей нет — верни []."
    )

    payload = {
        "model": MODEL,
        "max_tokens": 1500,
        "system": system_prompt,
        "messages": [{"role": "user", "content": avoid + "\nНовости:\n" + "\n".join(lines)}],
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
    }
    data = http_post_json(ANTHROPIC_URL, payload, headers)
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    chosen = extract_json_array(text)

    result = []
    used_ids = set()
    for c in chosen:
        try:
            idx = int(c.get("id"))
        except (TypeError, ValueError):
            continue
        if idx in used_ids or not (0 <= idx < len(items)):
            continue
        used_ids.add(idx)
        src = items[idx]
        result.append({
            "title": c.get("title") or src["title"],
            "summary": c.get("summary") or "",
            "url": src["link"],  # ссылку берём из самой ленты, а не из ответа модели
            "source": src["source"],
            "date": src["date"],
        })
        if len(result) >= count:
            break
    return result


# ---------------------------------------------------------------- Пульс дня (котировки)

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=10d&interval=1d"


def fetch_change(symbol):
    """Последняя цена и изменение в % к закрытию предыдущего дня (данные Yahoo Finance)."""
    url = YAHOO_CHART.format(sym=urllib.parse.quote(symbol))
    data = json.loads(http_get(url).decode("utf-8"))
    res = data["chart"]["result"][0]
    closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
    price = res.get("meta", {}).get("regularMarketPrice") or closes[-1]
    if len(closes) < 2:
        raise ValueError("мало данных")
    # Обычно последняя дневная свеча уже содержит текущую цену — тогда
    # сравниваем с закрытием свечи перед ней. Если текущей свечи ещё нет —
    # сравниваем с последним закрытием.
    if abs(closes[-1] - price) <= abs(price) * 1e-4:
        prev = closes[-2]
    else:
        prev = closes[-1]
    return price, (price - prev) / prev * 100


def fmt_price(value):
    if value >= 1000:
        text = f"{value:,.0f}".replace(",", " ")
    else:
        text = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return text


def build_market_lines(topic, problems):
    """Возвращает (строки для текстового варианта, данные для картинки)."""
    lines, groups_data = [], []
    for group in topic.get("groups", []):
        row_parts, row_data = [], []
        for ins in group.get("items", []):
            try:
                price, change = fetch_change(ins["symbol"])
            except Exception as e:
                print(f"[котировки] {ins['symbol']}: {e}", file=sys.stderr)
                problems.add(f"котировка {ins['name']}")
                continue
            mark = "🟢" if change > 0.005 else ("🔴" if change < -0.005 else "⚪")
            pct = f"{change:+.2f}%".replace(".", ",")
            row_parts.append(f"{mark} {escape_html(ins['name'])} {fmt_price(price)} ({pct})")
            row_data.append((ins["name"], fmt_price(price), change))
        if row_data:
            groups_data.append((group.get("title", ""), row_data))
        if row_parts:
            if lines:
                lines.append("")
            lines.append(f"<b>{escape_html(group.get('title', ''))}</b>")
            lines.extend(row_parts)
    return lines, groups_data


# ---------------------------------------------------------------- Telegram

def escape_html(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def time_label(dt):
    if not dt:
        return ""
    return dt.astimezone(MOSCOW_TZ).strftime("%d.%m %H:%M")


def build_pulse(sections):
    """Отдельное сообщение «Пульс дня». Пустая строка, если котировок нет."""
    for name, result in sections:
        if result.get("lines"):
            now_str = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")
            # Длинная линия-разделитель растягивает сообщение на всю ширину экрана
            return "\n".join([f"<b>{escape_html(name)} — {now_str}</b>", DIVIDER, ""]
                             + result["lines"] + ["", DIVIDER])
    return ""


def build_digest(sections, footer=""):
    """Посты с новостями: отдельное сообщение на каждый раздел.
    Подпись (footer) — только под последним. Пустой список, если новостей нет."""
    now_str = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")
    news = [(n, r) for n, r in sections
            if not r.get("lines") and not r["error"] and r["items"]]
    messages = []
    for i, (name, result) in enumerate(news):
        lines = [f"<b>{escape_html(name)} — {now_str}</b>", DIVIDER, ""]
        for item in result["items"]:
            title = escape_html(item["title"])
            if item["url"]:
                title = f'<a href="{escape_html(item["url"])}">{title}</a>'
            lines.append(f"• {title}")
            if item["summary"]:
                lines.append(f"  {escape_html(item['summary'])}")
            meta = " · ".join(x for x in [escape_html(item["source"]), time_label(item["date"])] if x)
            if meta:
                lines.append(f"  <i>{meta}</i>")
            lines.append("")
        lines.append(DIVIDER)
        if footer and i == len(news) - 1:
            lines.append(escape_html(footer))
        messages.append("\n".join(lines).strip())
    return messages


def build_service_report(sections, problems):
    """Служебное сообщение для владельца: что сломалось. Пустая строка — если всё хорошо."""
    lines = []
    for name, result in sections:
        if result.get("lines"):
            continue
        if result["error"]:
            lines.append(f"• {escape_html(name)}: {escape_html(result['error'])}")
        elif not result["items"]:
            lines.append(f"• {escape_html(name)}: свежих новостей не нашлось, раздел пропущен")
    if problems:
        lines.append(f"• Не ответили источники: {escape_html(', '.join(sorted(problems)))}")
    if not lines:
        return ""
    return "<b>⚙️ Служебное: дайджест</b>\n" + "\n".join(lines)


def split_message(text, limit=4000):
    parts, current, size = [], [], 0
    for line in text.split("\n"):
        if current and size + len(line) + 1 > limit:
            parts.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        parts.append("\n".join(current))
    return parts


def send_telegram_message(text, chat_id):
    url = TELEGRAM_URL.format(token=TELEGRAM_BOT_TOKEN)
    for chunk in split_message(text):
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        http_post_json(url, payload, {"Content-Type": "application/json"})


def send_telegram_photo(png_bytes, caption, chat_id):
    """Отправка картинки (multipart/form-data, без сторонних библиотек)."""
    boundary = "----newsdigest" + os.urandom(8).hex()
    parts = []
    for key, val in (("chat_id", chat_id), ("caption", caption), ("parse_mode", "HTML")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{val}\r\n'.encode("utf-8"))
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="pulse.png"\r\n'
                  "Content-Type: image/png\r\n\r\n").encode("utf-8") + png_bytes + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def pulse_as_image(sections):
    """PNG-картинка «Пульса дня» или None (нет данных / нет библиотеки)."""
    for name, result in sections:
        if result.get("data"):
            try:
                import pulse_image
            except ImportError:
                print("Pillow не установлен — пульс уйдёт текстом", file=sys.stderr)
                return None
            date_str = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")
            return pulse_image.render(result["data"], date_str)
    return None


# ---------------------------------------------------------------- main

def main():
    missing = [n for n, v in [
        ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
        ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
        ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
    ] if not v]
    if missing:
        print(f"Не заданы переменные окружения: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    topics = load_config()
    problems = set()
    sections = []
    already_chosen = []

    for topic in topics:
        name = topic["name"]
        if topic.get("type") == "market":
            mlines, mdata = build_market_lines(topic, problems)
            sections.append((name, {"items": [], "lines": mlines, "data": mdata,
                                    "error": None if mlines else "котировки недоступны"}))
            continue
        try:
            items = collect_topic_items(topic, problems)
            if not items:
                sections.append((name, {"items": [], "error": None}))
                continue
            picked = pick_top_news(topic, items, already_chosen)
            already_chosen.extend(p["title"] for p in picked)
            sections.append((name, {"items": picked, "error": None}))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:200]
            print(f"[{name}] Claude API: {e.code} {detail}", file=sys.stderr)
            sections.append((name, {"items": [], "error": f"ошибка Claude API {e.code}"}))
        except Exception as e:
            print(f"[{name}] {e}", file=sys.stderr)
            sections.append((name, {"items": [], "error": str(e)[:150]}))

    preview = os.environ.get("PREVIEW") == "true"
    target = TELEGRAM_CHAT_ID if preview else (TELEGRAM_CHANNEL_ID or TELEGRAM_CHAT_ID)
    report = build_service_report(sections, problems)
    footer = CHANNEL_FOOTER if TELEGRAM_CHANNEL_ID else ""
    failures = []

    # «Пульс дня» — картинкой; если не получилось — текстом
    pulse_sent = False
    png = None
    try:
        png = pulse_as_image(sections)
    except Exception as e:
        print(f"Не удалось нарисовать картинку: {e}", file=sys.stderr)
    if png:
        caption = f"<b>📊 Пульс дня — {datetime.now(MOSCOW_TZ).strftime('%d.%m.%Y')}</b>"
        try:
            send_telegram_photo(png, caption, target)
            pulse_sent = True
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", "ignore")
            print(f"Ошибка отправки картинки: {err}", file=sys.stderr)
            failures.append(err[:200])

    messages = ([] if pulse_sent else [build_pulse(sections)]) + build_digest(sections, footer)
    messages = [m for m in messages if m]
    if preview:
        messages = []  # в режиме просмотра — только картинка

    for msg in messages:
        try:
            send_telegram_message(msg, target)
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", "ignore")
            print(f"Ошибка отправки в Telegram ({target}): {err}", file=sys.stderr)
            failures.append(err[:200])
    if not failures:
        print(f"Отправлено в {target}: картинка={'да' if pulse_sent else 'нет'}, сообщений={len(messages)}.")

    extra = []
    if not messages and not pulse_sent:
        extra.append("• Пост не отправлен: нет ни котировок, ни новостей")
    for err in failures:
        extra.append(f"• Не удалось отправить пост в {escape_html(target)}: {escape_html(err)}")
    if extra:
        report = (report or "<b>⚙️ Служебное: дайджест</b>") + "\n" + "\n".join(extra)

    if report:
        send_telegram_message(report, TELEGRAM_CHAT_ID)
        print("Служебное сообщение отправлено владельцу.")


if __name__ == "__main__":
    main()
