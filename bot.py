"""
Сповіщення про оновлення графіка відключень (ГПВ) «Волиньобленерго» у Telegram.

Запускається за розкладом у GitHub Actions. Кожен запуск:
  1. бере з офіційного архіву «Волиньобленерго» графіки на сьогодні й завтра;
  2. для кожного дня обирає найновішу редакцію;
  3. розпізнає з картинки години відключень для вашої черги;
  4. якщо графік вашої черги змінився (або з'явився графік на новий день),
     надсилає в Telegram картинку і текст.

Налаштування (GitHub → Settings → Secrets and variables → Actions):
  TELEGRAM_BOT_TOKEN  (secret)   токен від @BotFather
  TELEGRAM_CHAT_ID    (secret)   ваш chat id
  QUEUE               (variable) ваша підчерга, напр. 3.2 (можна кілька: 3.2,5.1)
  NOTIFY_ALL          (variable) "true" — писати про будь-яку нову редакцію графіка,
                                 навіть якщо для вашої черги нічого не змінилось
"""

import io
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image

API = "https://api-voe-poweron.inneti.net"
KYIV = ZoneInfo("Europe/Kyiv")
STATE_FILE = Path(__file__).with_name("state.json")
SUBQUEUES = [f"{q}.{s}" for q in range(1, 7) for s in (1, 2)]  # 1.1 … 6.2

# Кольори клітинок на картинці «Волиньобленерго»
COLOR_ON = (185, 200, 215)   # є світло
COLOR_OFF = (68, 110, 155)   # немає світла
TOLERANCE = 30

HEADERS = {
    "User-Agent": "Mozilla/5.0 (svitlo-notifier; github actions)",
    "Accept": "application/json",
}


def log(*args):
    print(*args, flush=True)


# ---------------------------------------------------------------- дані з API

def fetch_revisions(day_from, day_to):
    """Усі редакції графіків між двома датами (включно)."""
    after = day_from.strftime("%Y-%m-%dT00:00:00")
    before = (day_to + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    items = []
    for page in range(1, 6):
        r = requests.get(
            f"{API}/api/archive_gpv_graphs",
            params={"page": page, "dateGraph[after]": after, "dateGraph[before]": before},
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            data = data.get("hydra:member") or data.get("member") or data.get("items") or []
        if not data:
            break
        items.extend(data)
        if len(data) < 30:
            break
    return items


def parse_dt(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KYIV)
    return dt.astimezone(KYIV)


def image_url(item):
    for key in ("imageUrl", "imageMobileUrl"):
        v = item.get(key)
        if isinstance(v, dict):
            v = v.get("contentUrl")
        if isinstance(v, str) and v:
            return v if v.startswith("http") else API + v
    return None


def latest_per_day(items, wanted_days):
    """{дата: найновіша редакція} лише для потрібних днів."""
    best = {}
    for it in items:
        try:
            day = parse_dt(it["dateGraph"]).date()
            created = parse_dt(it.get("dateCreate") or it["dateGraph"])
        except (KeyError, ValueError):
            continue
        if day not in wanted_days or not image_url(it):
            continue
        if day not in best or created > best[day][0]:
            best[day] = (created, it)
    return {d: v[1] for d, v in best.items()}


# ---------------------------------------------------------- розбір картинки

def classify(px):
    def close(c):
        return all(abs(a - b) <= TOLERANCE for a, b in zip(px, c))
    if close(COLOR_ON):
        return "on"
    if close(COLOR_OFF):
        return "off"
    return None


def parse_image(png_bytes):
    """
    Повертає {"1.1": [True, False, ...48 значень], ...}, де True = немає світла
    в цю пів години. Якщо картинка має неочікуваний вигляд — None.
    """
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    w, h = img.size
    px = img.load()

    # 1. Горизонтальні смуги, де більшість пікселів — кольори таблиці
    step = 2
    is_row = []
    for y in range(h):
        hits = sum(1 for x in range(0, w, step) if classify(px[x, y]))
        is_row.append(hits > 0.3 * (w / step))
    bands, start = [], None
    for y, flag in enumerate(is_row + [False]):
        if flag and start is None:
            start = y
        elif not flag and start is not None:
            if y - start >= 8:
                bands.append((start, y - 1))
            start = None

    # Очікуємо 2 таблиці по 13 рядків (заголовок + 12 підчерг)
    if len(bands) != 26:
        log(f"Розбір картинки: знайдено {len(bands)} рядків замість 26")
        return None
    data_rows = bands[1:13] + bands[14:26]

    def cells_in_row(y):
        segs, s = [], None
        for x in range(w + 1):
            ok = x < w and classify(px[x, y]) is not None
            if ok and s is None:
                s = x
            elif not ok and s is not None:
                if x - s >= 5:
                    segs.append((s, x - 1))
                s = None
        return segs[-24:] if len(segs) >= 24 else None

    result = {q: [] for q in SUBQUEUES}
    for half in (0, 1):
        for i, q in enumerate(SUBQUEUES):
            y0, y1 = data_rows[half * 12 + i]
            yc = (y0 + y1) // 2
            segs = cells_in_row(yc)
            if not segs:
                log(f"Розбір картинки: у рядку {q} не знайдено 24 клітинки")
                return None
            for x0, x1 in segs:
                state = classify(px[(x0 + x1) // 2, yc])
                if state is None:
                    log(f"Розбір картинки: невідомий колір у рядку {q}")
                    return None
                result[q].append(state == "off")
    return result


def intervals(slots):
    """[True, True, False, ...] → ["00:00–01:00", ...]"""
    out, start = [], None
    for i, off in enumerate(slots + [False]):
        if off and start is None:
            start = i
        elif not off and start is not None:
            out.append(f"{start // 2:02d}:{start % 2 * 30:02d}–{i // 2:02d}:{i % 2 * 30:02d}")
            start = None
    return out


# ---------------------------------------------------------------- Telegram

def send_photo(token, chat_id, png_bytes, caption):
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
        files={"photo": ("gpv.png", png_bytes, "image/png")},
        timeout=60,
    )
    if not r.ok:
        log("Telegram помилка:", r.status_code, r.text)
    r.raise_for_status()


# -------------------------------------------------------------------- main

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    queues = [q.strip() for q in os.environ.get("QUEUE", "").split(",") if q.strip()]
    notify_all = os.environ.get("NOTIFY_ALL", "").strip().lower() in ("1", "true", "yes", "так")

    if not token or not chat_id:
        sys.exit("Не задано TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID (див. README).")
    bad = [q for q in queues if q not in SUBQUEUES]
    if not queues or bad:
        sys.exit(f"Змінна QUEUE має бути підчергою на кшталт 3.2 (зараз: {os.environ.get('QUEUE')!r}).")

    state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}
    today = datetime.now(KYIV).date()
    days = {today, today + timedelta(days=1)}

    items = fetch_revisions(today, today + timedelta(days=1))
    log(f"Отримано редакцій: {len(items)}")
    latest = latest_per_day(items, days)

    for day in sorted(latest):
        item = latest[day]
        key = day.isoformat()
        rev_id = str(item.get("id") or item.get("@id") or item.get("dateCreate"))
        prev = state.get(key, {})
        if prev.get("rev") == rev_id:
            log(f"{key}: без змін")
            continue

        url = image_url(item)
        png = requests.get(url, headers=HEADERS, timeout=60).content
        parsed = parse_image(png)

        mine = {q: intervals(parsed[q]) for q in queues} if parsed else None
        changed_for_me = mine is None or mine != prev.get("mine")
        first_for_day = not prev

        created = parse_dt(item.get("dateCreate") or item["dateGraph"])
        day_word = "сьогодні" if day == today else "завтра"
        lines = [
            f"⚡ <b>{'Графік' if first_for_day else 'Оновлено графік'} на {day_word}, {day:%d.%m}</b>",
            f"Редакція від {created:%H:%M %d.%m}",
            "",
        ]
        if mine is None:
            lines.append("Не вдалося автоматично прочитати години — дивіться картинку.")
        else:
            for q in queues:
                hrs = mine[q]
                lines.append(f"Черга <b>{q}</b>: " + (", ".join(hrs) if hrs else "відключень немає ✅"))
                old = (prev.get("mine") or {}).get(q)
                if old is not None and old != hrs:
                    lines.append(f"   було: {', '.join(old) if old else 'без відключень'}")

        if first_for_day or changed_for_me or notify_all:
            send_photo(token, chat_id, png, "\n".join(lines))
            log(f"{key}: надіслано сповіщення")
        else:
            log(f"{key}: нова редакція, але для вашої черги нічого не змінилось")

        state[key] = {"rev": rev_id, "mine": mine}

    # прибираємо старі дні
    state = {k: v for k, v in state.items() if k >= (today - timedelta(days=2)).isoformat()}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")


if __name__ == "__main__":
    main()
