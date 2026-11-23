"""
Telegram-бот із графіком відключень (ГПВ) «Волиньобленерго».

Запускається за розкладом у GitHub Actions (приблизно раз на 10 хвилин). Кожен запуск:
  1. обробляє повідомлення й натискання кнопок, які надійшли боту з минулого запуску
     (вибір черги, запит графіка);
  2. бере з офіційного архіву «Волиньобленерго» графіки на сьогодні й завтра
     і розпізнає з картинки години відключень;
  3. кожному користувачу, у якого змінився графік його черги (або з'явився графік
     на новий день), надсилає картинку й години без світла.

Налаштування (GitHub → Settings → Secrets and variables → Actions):
  TELEGRAM_BOT_TOKEN  (secret)   токен від @BotFather
  TELEGRAM_CHAT_ID    (secret)   chat id тих, кому можна користуватися ботом
                                 (кілька — через кому, напр. для родини)
  QUEUE               (variable) необов'язково: черга за замовчуванням, напр. 3.2
                                 (тепер її можна обрати прямо в боті)
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

PARSER_VERSION = 2  # якщо зміниться, графіки, які не вдалося прочитати, перечитаються
LOOKUP_URL = "https://energy.volyn.ua/spozhyvacham/perervy-u-elektropostachanni/cherga/"

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
    # API віддає київський час, хоч і позначає його як UTC (+00:00),
    # тому позначку часового поясу відкидаємо.
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=KYIV)


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
            if y - start >= 6:
                bands.append((start, y - 1))
            start = None

    def cells_in_row(y):
        segs, s = [], None
        for x in range(w + 1):
            ok = x < w and classify(px[x, y]) is not None
            if ok and s is None:
                s = x
            elif not ok and s is not None:
                if x - s >= 4:
                    segs.append((s, x - 1))
                s = None
        # Якщо дві сусідні клітинки злиплися, ділимо їх за типовою шириною
        if len(segs) >= 12:
            widths = sorted(b - a for a, b in segs[-24:])
            m = widths[len(widths) // 2] or 1
            # вузькі смужки — це розмиті лінії між клітинками, а не клітинки
            segs = [(a, b) for a, b in segs if (b - a) >= 0.5 * m]
            fixed = []
            for a, b in segs:
                n = round((b - a) / m) if (b - a) > 1.6 * m else 1
                step_w = (b - a + 1) / n
                fixed += [(int(a + k * step_w), int(a + (k + 1) * step_w) - 1) for k in range(n)]
            segs = fixed
        return segs

    # Лишаємо тільки рядки основних таблиць (у них ≥ 24 клітинки).
    # Так відкидаються маленька таблиця «Загальна тривалість», легенда тощо.
    rows = [
        (y0, y1) for y0, y1 in bands
        if max(len(cells_in_row(y0 + (y1 - y0) * k // 4)) for k in (1, 2, 3)) >= 24
    ]
    if len(rows) < 4:
        log(f"Розбір картинки: знайдено лише {len(rows)} рядків таблиці")
        return None

    # Якщо два рядки злиплися (розмита біла лінія), ділимо їх навпіл
    heights = sorted(y1 - y0 for y0, y1 in rows)
    m = heights[len(heights) // 2]
    split = []
    for y0, y1 in rows:
        n = max(1, round((y1 - y0) / m)) if (y1 - y0) > 1.6 * m else 1
        step_h = (y1 - y0 + 1) / n
        for k in range(n):
            split.append((int(y0 + k * step_h), int(y0 + (k + 1) * step_h) - 1))

    # Ділимо на дві таблиці за великим проміжком між ними
    gaps = [split[i + 1][0] - split[i][1] for i in range(len(split) - 1)]
    g = sorted(gaps)[len(gaps) // 2]
    tables, cur = [], [split[0]]
    for i, gap in enumerate(gaps):
        if gap > 3 * g + 3:
            tables.append(cur)
            cur = []
        cur.append(split[i + 1])
    tables.append(cur)
    tables = [t for t in tables if len(t) >= 13]
    if len(split) == 26 and [len(t) for t in tables] != [13, 13]:
        tables = [split[:13], split[13:]]
    if len(tables) != 2 or any(len(t) != 13 for t in tables):
        log(f"Розбір картинки: очікували 2 таблиці по 13 рядків, маємо {[len(t) for t in tables]}")
        return None

    data_rows = []
    for t in tables:
        for y0, y1 in t[1:]:  # перший рядок — заголовок з годинами
            yc = (y0 + y1) // 2
            segs = cells_in_row(yc)
            if len(segs) < 24:
                log("Розбір картинки: у рядку менше 24 клітинок")
                return None
            data_rows.append((y0, y1, segs[-24:]))

    result = {q: [] for q in SUBQUEUES}
    for half in (0, 1):
        for i, q in enumerate(SUBQUEUES):
            y0, y1, segs = data_rows[half * 12 + i]
            yc = (y0 + y1) // 2
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

class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, files=None, quiet=False, **data):
        payload = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                   for k, v in data.items() if v is not None}
        r = requests.post(self.base + method, data=payload, files=files, timeout=60)
        if not r.ok:
            log(f"Telegram {method} помилка:", r.status_code, r.text)
            if not quiet:
                r.raise_for_status()
            return None
        return r.json().get("result")

    def send(self, chat_id, text, markup=None):
        return self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                         reply_markup=markup, disable_web_page_preview="true")

    def photo(self, chat_id, png, caption):
        return self.call("sendPhoto", chat_id=chat_id, caption=caption, parse_mode="HTML",
                         files={"photo": ("gpv.png", png, "image/png")})


MAIN_KEYBOARD = {
    "keyboard": [[{"text": "📅 Графік"}, {"text": "⚙️ Моя черга"}]],
    "resize_keyboard": True,
    "is_persistent": True,
}
BTN_GRAPH = "📅 Графік"
BTN_QUEUE = "⚙️ Моя черга"


def queue_keyboard(selected):
    rows, row = [], []
    for q in SUBQUEUES:
        row.append({"text": ("✅ " if q in selected else "") + q, "callback_data": f"q:{q}"})
        if len(row) == 4:
            rows.append(row)
            row = []
    rows.append([{"text": "💾 Готово", "callback_data": "done"}])
    return {"inline_keyboard": rows}


def queue_prompt(selected):
    chosen = ", ".join(selected) if selected else "ще не обрано"
    return (
        "⚙️ <b>Оберіть свою підчергу</b> (можна кілька), потім натисніть «Готово».\n"
        f"Зараз: <b>{chosen}</b>\n\n"
        f'Не знаєте свою підчергу? <a href="{LOOKUP_URL}">Перевірте за адресою</a>.'
    )


HELLO = (
    "👋 Привіт! Я надсилатиму графік відключень «Волиньобленерго» для вашої черги: "
    "щойно з'явиться графік на новий день або зміняться години.\n\n"
    "📅 <b>Графік</b> — показати графік на сьогодні й завтра\n"
    "⚙️ <b>Моя черга</b> — обрати чергу\n\n"
    "⏳ Я працюю на GitHub Actions і перевіряю повідомлення приблизно раз на 10 хвилин, "
    "тож відповідаю не миттєво."
)


def handle_updates(tg, state, allowed, default_queues):
    """Обробляє повідомлення й кнопки. Повертає {chat_id: True} — кому надіслати графік."""
    chats = state.setdefault("chats", {})
    requests_graph = {}
    offset = state.get("tg_offset")
    updates = tg.call("getUpdates", quiet=True, offset=offset, timeout=0,
                      allowed_updates=["message", "callback_query"]) or []
    log(f"Нових повідомлень боту: {len(updates)}")

    for upd in updates:
        state["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message")
        cb = upd.get("callback_query")
        chat_id = str((msg or (cb or {}).get("message") or {}).get("chat", {}).get("id", ""))
        if not chat_id:
            continue
        if chat_id not in allowed:
            if msg:
                tg.send(chat_id, "🔒 Це приватний бот.")
            elif cb:
                tg.call("answerCallbackQuery", quiet=True, callback_query_id=cb["id"])
            continue

        chat = chats.setdefault(chat_id, {"queues": list(default_queues), "sent": {}})

        if cb:
            tg.call("answerCallbackQuery", quiet=True, callback_query_id=cb["id"])
            data = cb.get("data", "")
            mid = cb["message"]["message_id"]
            if data.startswith("q:") and data[2:] in SUBQUEUES:
                q = data[2:]
                chat["queues"] = sorted(set(chat["queues"]) ^ {q}, key=SUBQUEUES.index)
                tg.call("editMessageText", quiet=True, chat_id=chat_id, message_id=mid,
                        text=queue_prompt(chat["queues"]), parse_mode="HTML",
                        disable_web_page_preview="true",
                        reply_markup=queue_keyboard(chat["queues"]))
            elif data == "done":
                chosen = ", ".join(chat["queues"]) or "нічого"
                tg.call("editMessageText", quiet=True, chat_id=chat_id, message_id=mid,
                        text=f"💾 Збережено. Ваша черга: <b>{chosen}</b>", parse_mode="HTML")
                if chat["queues"]:
                    chat["sent"] = {}
                    requests_graph[chat_id] = True
            continue

        text = (msg.get("text") or "").strip()
        if text.startswith("/start"):
            tg.send(chat_id, HELLO, MAIN_KEYBOARD)
            if not chat["queues"]:
                tg.send(chat_id, queue_prompt(chat["queues"]), queue_keyboard(chat["queues"]))
        elif text in (BTN_QUEUE, "/queue", "/cherga"):
            tg.send(chat_id, queue_prompt(chat["queues"]), queue_keyboard(chat["queues"]))
        elif text in (BTN_GRAPH, "/graph", "/grafik"):
            if chat["queues"]:
                requests_graph[chat_id] = True
            else:
                tg.send(chat_id, queue_prompt(chat["queues"]), queue_keyboard(chat["queues"]))
        elif all(p.strip() in SUBQUEUES for p in text.replace(" ", ",").split(",") if p.strip()) and text:
            chat["queues"] = sorted({p.strip() for p in text.replace(" ", ",").split(",") if p.strip()},
                                    key=SUBQUEUES.index)
            tg.send(chat_id, f"💾 Збережено. Ваша черга: <b>{', '.join(chat['queues'])}</b>", MAIN_KEYBOARD)
            chat["sent"] = {}
            requests_graph[chat_id] = True
        else:
            tg.send(chat_id, "Натисніть кнопку внизу або напишіть номер підчерги, напр. <b>3.2</b>.",
                    MAIN_KEYBOARD)
    return requests_graph


# -------------------------------------------------------------------- main

def build_caption(day, today, created, queues, grid, old_mine, first):
    day_word = "сьогодні" if day == today else "завтра"
    lines = [
        f"⚡ <b>{'Графік' if first else 'Оновлено графік'} на {day_word}, {day:%d.%m}</b>",
        f"Редакція від {created:%H:%M %d.%m}",
        "",
    ]
    if grid is None:
        lines.append("Не вдалося автоматично прочитати години — дивіться картинку.")
        return "\n".join(lines)
    for q in queues:
        slots = [c == "1" for c in grid[q]]
        hrs = intervals(slots)
        if hrs:
            lines.append(f"🔴 Черга <b>{q}</b>: {', '.join(hrs)}  (≈{sum(slots) / 2:g} год без світла)")
        else:
            lines.append(f"🟢 Черга <b>{q}</b>: відключень немає")
        if old_mine is not None and q in old_mine and old_mine[q] != hrs:
            lines.append(f"   було: {', '.join(old_mine[q]) if old_mine[q] else 'без відключень'}")
    return "\n".join(lines)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    allowed = {c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()}
    default_queues = [q.strip() for q in os.environ.get("QUEUE", "").split(",")
                      if q.strip() in SUBQUEUES]
    notify_all = os.environ.get("NOTIFY_ALL", "").strip().lower() in ("1", "true", "yes", "так")
    if not token or not allowed:
        sys.exit("Не задано TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID (див. README).")

    tg = Telegram(token)
    state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}
    if "days" not in state:  # старий формат файлу стану
        state = {"days": {}, "chats": {}}

    if state.get("commands_v") != 1:
        tg.call("setMyCommands", quiet=True, commands=[
            {"command": "graph", "description": "Графік на сьогодні й завтра"},
            {"command": "queue", "description": "Обрати чергу"},
        ])
        state["commands_v"] = 1

    for cid in allowed:
        state["chats"].setdefault(cid, {"queues": list(default_queues), "sent": {}})

    asked = handle_updates(tg, state, allowed, default_queues)

    # ---- графіки
    today = datetime.now(KYIV).date()
    days = {today, today + timedelta(days=1)}
    items = fetch_revisions(today, today + timedelta(days=1))
    log(f"Отримано редакцій графіка: {len(items)}")
    latest = latest_per_day(items, days)

    images = {}

    def png_for(day):
        if day not in images:
            url = image_url(latest[day])
            images[day] = requests.get(url, headers=HEADERS, timeout=60).content
        return images[day]

    for day in sorted(latest):
        item, key = latest[day], day.isoformat()
        rev = str(item.get("id") or item.get("@id") or item.get("dateCreate"))
        info = state["days"].get(key, {})
        if info.get("rev") != rev or (info.get("grid") is None and info.get("parser") != PARSER_VERSION):
            parsed = parse_image(png_for(day))
            grid = {q: "".join("1" if x else "0" for x in parsed[q]) for q in SUBQUEUES} if parsed else None
            info = {"rev": rev, "grid": grid, "parser": PARSER_VERSION,
                    "created": parse_dt(item.get("dateCreate") or item["dateGraph"]).isoformat()}
            state["days"][key] = info
            log(f"{key}: нова редакція {rev}, години {'прочитано' if grid else 'НЕ прочитано'}")

    # ---- розсилка
    for cid, chat in state["chats"].items():
        if cid not in allowed or not chat.get("queues"):
            continue
        for day in sorted(latest):
            key = day.isoformat()
            info = state["days"][key]
            grid = info["grid"]
            sent = chat["sent"].get(key)
            mine = {q: intervals([c == "1" for c in grid[q]]) for q in chat["queues"]} if grid else None
            changed = (sent is None
                       or (grid is not None and mine != sent.get("mine"))
                       or (grid is None and info["rev"] != sent.get("rev"))
                       or (notify_all and info["rev"] != sent.get("rev")))
            if not (changed or asked.get(cid)):
                continue
            first = sent is None or asked.get(cid)
            caption = build_caption(day, today, datetime.fromisoformat(info["created"]), chat["queues"],
                                    grid, None if first else sent.get("mine"), first)
            tg.photo(cid, png_for(day), caption)
            chat["sent"][key] = {"rev": info["rev"], "mine": mine}
            log(f"{key}: надіслано в чат …{cid[-3:]}")
        if asked.get(cid) and not latest:
            tg.send(cid, "Графіків на сьогодні й завтра поки немає. Щойно з'являться — надішлю.")

    # ---- прибирання старих днів
    keep = (today - timedelta(days=1)).isoformat()
    state["days"] = {k: v for k, v in state["days"].items() if k >= keep}
    for chat in state["chats"].values():
        chat["sent"] = {k: v for k, v in chat["sent"].items() if k >= keep}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")


if __name__ == "__main__":
    main()
