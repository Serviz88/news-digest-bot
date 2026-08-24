#!/usr/bin/env python3
"""
Ежедневный дайджест новостей в Telegram.

Запускается по расписанию (см. .github/workflows/daily-digest.yml).
Темы для отслеживания читаются из topics.json в этой же папке.

Нужные переменные окружения (задаются как GitHub Secrets):
  ANTHROPIC_API_KEY   - ключ Anthropic API
  TELEGRAM_BOT_TOKEN  - токен бота от BotFather
  TELEGRAM_CHAT_ID    - id чата/пользователя, куда слать сообщения
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

MAX_ITEMS_PER_TOPIC = 4
MOSCOW_TZ = timezone(timedelta(hours=3))


def load_topics():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "topics.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            topics = data.get("topics", [])
            if topics:
                return topics
    except FileNotFoundError:
        pass
    return ["Экономика и финансы"]


def http_post_json(url, payload, headers, timeout=90):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def extract_json_array(raw_text):
    cleaned = raw_text.replace("```json", "").replace("```", "").strip()
    start = cleaned.find("[")
    if start == -1:
        raise ValueError("в ответе нет массива")
    sliced = cleaned[start:]

    try:
        return json.loads(sliced)
    except json.JSONDecodeError:
        pass

    last_brace = sliced.rfind("}")
    while last_brace > 0:
        candidate = sliced[: last_brace + 1] + "]"
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            last_brace = sliced.rfind("}", 0, last_brace)

    raise ValueError("не удалось разобрать ответ модели")


def fetch_news_for_topic(topic):
    system_prompt = (
        "Ты — ассистент новостного мониторинга. Найди через веб-поиск "
        f"{MAX_ITEMS_PER_TOPIC} самые свежие новости (за последние 24-48 часов) "
        "по теме от пользователя.\n"
        "Ответь ТОЛЬКО компактным JSON-массивом в одну строку, без переносов "
        "строк, без markdown-обрамления и без единого слова текста до или после массива.\n"
        'Формат элемента: {"title":"..","source":"..","summary":"..","url":"..","time":".."}\n'
        "Правила:\n"
        "- title и summary — на русском, даже если источник на другом языке.\n"
        "- summary — не длиннее 15 слов, самая суть.\n"
        "- source — короткое название издания.\n"
        "- url — прямая ссылка на статью.\n"
        '- time — относительное время/дата, иначе "".\n'
        "- Если ничего свежего нет — верни [].\n"
        "Экономь токены: без рассуждений, сразу ищи и сразу отвечай JSON."
    )

    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1000,
        "system": system_prompt,
        "messages": [{"role": "user", "content": f"Тема: {topic}"}],
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
    }

    data = http_post_json(ANTHROPIC_URL, payload, headers)
    text_parts = [
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    text = "\n".join(p for p in text_parts if p).strip()
    if not text:
        raise ValueError("пустой ответ от модели")

    items = extract_json_array(text)
    if not isinstance(items, list):
        raise ValueError("неожиданный формат ответа")
    return items


def escape_html(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build_digest(topic_results):
    now_str = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
    lines = [f"<b>📰 Дайджест новостей — {now_str} МСК</b>", ""]

    any_items = False
    for topic, result in topic_results:
        lines.append(f"<b>{escape_html(topic)}</b>")
        if result["error"]:
            lines.append(f"⚠️ не удалось получить новости: {escape_html(result['error'])}")
        elif not result["items"]:
            lines.append("Свежих новостей не найдено.")
        else:
            any_items = True
            for item in result["items"]:
                title = escape_html(item.get("title") or "Без названия")
                url = item.get("url") or ""
                summary = escape_html(item.get("summary") or "")
                source = escape_html(item.get("source") or "")
                time_ = escape_html(item.get("time") or "")
                meta = " · ".join(x for x in [source, time_] if x)
                title_line = f'<a href="{escape_html(url)}">{title}</a>' if url else title
                lines.append(f"• {title_line}")
                if summary:
                    lines.append(f"  {summary}")
                if meta:
                    lines.append(f"  <i>{meta}</i>")
        lines.append("")

    if not any_items:
        lines.append("Похоже, сегодня без значимых свежих новостей по отслеживаемым темам.")

    return "\n".join(lines).strip()


def split_message(text, limit=4000):
    if len(text) <= limit:
        return [text]
    parts = []
    current = []
    current_len = 0
    for line in text.split("\n"):
        if current_len + len(line) + 1 > limit:
            parts.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        parts.append("\n".join(current))
    return parts


def send_telegram_message(text):
    url = TELEGRAM_URL.format(token=TELEGRAM_BOT_TOKEN)
    for chunk in split_message(text):
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        headers = {"Content-Type": "application/json"}
        http_post_json(url, payload, headers)


def main():
    missing = [
        name
        for name, val in [
            ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
        ]
        if not val
    ]
    if missing:
        print(f"Не заданы переменные окружения: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    topics = load_topics()
    topic_results = []

    for topic in topics:
        try:
            items = fetch_news_for_topic(topic)
            topic_results.append((topic, {"items": items, "error": None}))
        except (urllib.error.URLError, ValueError, json.JSONDecodeError) as e:
            topic_results.append((topic, {"items": [], "error": str(e)}))

    digest = build_digest(topic_results)

    try:
        send_telegram_message(digest)
    except urllib.error.HTTPError as e:
        print(f"Ошибка отправки в Telegram: {e.read().decode('utf-8', 'ignore')}", file=sys.stderr)
        sys.exit(1)

    print("Дайджест отправлен.")


if __name__ == "__main__":
    main()
