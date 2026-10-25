"""
Samarqand <-> Toshkent poyezd chiptalarini qidiruvchi Telegram bot.
Ma'lumot manbai: eticket.railway.uz (O'zbekiston temir yo'llari).

Imkoniyatlar:
  /start   - yo'nalish va sanani tanlab, bo'sh joylarni ko'rish
  /kuzat   - kuzatuvlar ro'yxati (joy chiqsa bot o'zi xabar beradi)
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

# ---------------- Sozlamalar ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "BU_YERGA_TOKEN_YOZING")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))  # soniya (5 daqiqa)
WATCH_FILE = Path("watches.json")

API_URL = "https://eticket.railway.uz/api/v3/trains/availability/space/between/stations"

STATIONS = {
    "TAS": ("2900000", "Toshkent"),
    "SAM": ("2900700", "Samarqand"),
}
DIRECTIONS = {
    "SAM-TAS": ("SAM", "TAS"),
    "TAS-SAM": ("TAS", "SAM"),
}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("train-bot")


# ---------------- railway.uz API ----------------
async def search_trains(dep_code: str, arv_code: str, day: date) -> list[dict]:
    """Berilgan sana uchun poyezdlar va bo'sh joylar ro'yxatini qaytaradi."""
    token = str(uuid.uuid4())  # XSRF double-submit: cookie va header bir xil bo'lishi kifoya
    headers = {
        "Accept": "application/json",
        "Accept-Language": "uz",
        "Content-Type": "application/json",
        "Origin": "https://eticket.railway.uz",
        "Referer": "https://eticket.railway.uz/uz/pages/trains-page",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
        "X-XSRF-TOKEN": token,
        "Cookie": f"XSRF-TOKEN={token}",
        "device-type": "BROWSER",
    }
    payload = {
        "direction": [{
            "depDate": day.strftime("%d.%m.%Y"),
            "fullday": True,
            "type": "Forward",
        }],
        "stationFrom": dep_code,
        "stationTo": arv_code,
        "detailNumPlaces": 1,
        "showWithoutPlaces": 0,
    }
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(API_URL, json=payload, headers=headers) as r:
            if r.status != 200:
                raise RuntimeError(f"railway.uz javobi: HTTP {r.status}")
            data = await r.json(content_type=None)

    trains = []
    for direction in (data.get("express") or {}).get("direction", []) or []:
        for group in direction.get("trains", []) or []:
            for t in group.get("train", []) or []:
                cars = []
                for car in (t.get("places") or {}).get("cars", []) or []:
                    price = None
                    tariffs = (car.get("tariffs") or {}).get("tariff") or []
                    if isinstance(tariffs, dict):
                        tariffs = [tariffs]
                    if tariffs:
                        try:
                            price = min(int(float(x.get("tariff", 0))) for x in tariffs)
                        except (TypeError, ValueError):
                            price = None
                    cars.append({
                        "type": car.get("typeShow") or car.get("type") or "?",
                        "free": int(car.get("freeSeats") or 0),
                        "price": price,
                    })
                trains.append({
                    "number": t.get("number", "?"),
                    "brand": t.get("brand") or t.get("type") or "",
                    "dep": (t.get("departure") or {}).get("time", ""),
                    "arv": (t.get("arrival") or {}).get("time", ""),
                    "duration": t.get("timeOnWay", ""),
                    "cars": cars,
                })
    return trains


def format_trains(direction: str, day: date, trains: list[dict]) -> str:
    a, b = DIRECTIONS[direction]
    head = f"🚆 <b>{STATIONS[a][1]} → {STATIONS[b][1]}</b>\n📅 {day.strftime('%d.%m.%Y')}\n"
    available = [t for t in trains if any(c["free"] > 0 for c in t["cars"])]
    if not available:
        return head + "\n❌ Bo'sh joy topilmadi."
    lines = [head]
    for t in available:
        lines.append(f"\n<b>{t['number']} {t['brand']}</b>")
        lines.append(f"🕐 {t['dep']} → {t['arv']}" + (f"  ({t['duration']})" if t['duration'] else ""))
        for c in t["cars"]:
            if c["free"] <= 0:
                continue
            price = f" — {c['price']:,} so'm".replace(",", " ") if c["price"] else ""
            lines.append(f"   • {c['type']}: {c['free']} ta joy{price}")
    lines.append("\n🎫 Sotib olish: eticket.railway.uz")
    return "\n".join(lines)


def total_free(trains: list[dict]) -> int:
    return sum(c["free"] for t in trains for c in t["cars"])


# ---------------- Kuzatuvlar (fayldagi saqlash) ----------------
def load_watches() -> list[dict]:
    try:
        return json.loads(WATCH_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_watches(w: list[dict]) -> None:
    WATCH_FILE.write_text(json.dumps(w, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- Klaviaturalar ----------------
def kb_directions() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Samarqand → Toshkent", callback_data="dir:SAM-TAS")],
        [InlineKeyboardButton(text="Toshkent → Samarqand", callback_data="dir:TAS-SAM")],
    ])


def kb_dates(direction: str) -> InlineKeyboardMarkup:
    today = date.today()
    names = ["Du", "Se", "Ch", "Pa", "Ju", "Sh", "Ya"]
    rows, row = [], []
    for i in range(14):
        d = today + timedelta(days=i)
        label = "Bugun" if i == 0 else "Ertaga" if i == 1 else f"{d.strftime('%d.%m')} {names[d.weekday()]}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"day:{direction}:{d.isoformat()}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_result(direction: str, day: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Yangilash", callback_data=f"day:{direction}:{day}")],
        [InlineKeyboardButton(text="🔔 Joy chiqsa xabar ber", callback_data=f"watch:{direction}:{day}")],
        [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back")],
    ])


# ---------------- Handlerlar ----------------
dp = Dispatcher()


@dp.message(CommandStart())
async def start(m: Message):
    await m.answer("Assalomu alaykum! 🚆\nQaysi yo'nalishda chipta qidiramiz?",
                   reply_markup=kb_directions())


@dp.callback_query(F.data == "back")
async def back(c: CallbackQuery):
    await c.message.edit_text("Qaysi yo'nalishda chipta qidiramiz?", reply_markup=kb_directions())
    await c.answer()


@dp.callback_query(F.data.startswith("dir:"))
async def choose_dir(c: CallbackQuery):
    direction = c.data.split(":")[1]
    a, b = DIRECTIONS[direction]
    await c.message.edit_text(f"<b>{STATIONS[a][1]} → {STATIONS[b][1]}</b>\nSanani tanlang:",
                              reply_markup=kb_dates(direction), parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data.startswith("day:"))
async def choose_day(c: CallbackQuery):
    _, direction, day_s = c.data.split(":")
    day = date.fromisoformat(day_s)
    a, b = DIRECTIONS[direction]
    await c.answer("Qidirilmoqda...")
    try:
        trains = await search_trains(STATIONS[a][0], STATIONS[b][0], day)
        text = format_trains(direction, day, trains)
    except Exception as e:
        log.exception("search failed")
        text = f"⚠️ Sayt bilan bog'lanib bo'lmadi: {e}\nBirozdan so'ng qayta urinib ko'ring."
    text += f"\n\n<i>Yangilandi: {datetime.now().strftime('%H:%M:%S')}</i>"
    await c.message.edit_text(text, parse_mode="HTML", reply_markup=kb_result(direction, day_s))


@dp.callback_query(F.data.startswith("watch:"))
async def add_watch(c: CallbackQuery):
    _, direction, day_s = c.data.split(":")
    watches = load_watches()
    item = {"chat_id": c.from_user.id, "direction": direction, "day": day_s, "last_free": 0}
    if any(w["chat_id"] == item["chat_id"] and w["direction"] == direction and w["day"] == day_s
           for w in watches):
        await c.answer("Bu sana allaqachon kuzatilmoqda", show_alert=True)
        return
    watches.append(item)
    save_watches(watches)
    await c.answer(f"🔔 Kuzatuv yoqildi! Har {CHECK_INTERVAL // 60} daqiqada tekshiraman.", show_alert=True)


@dp.message(Command("kuzat"))
async def list_watches(m: Message):
    mine = [w for w in load_watches() if w["chat_id"] == m.from_user.id]
    if not mine:
        await m.answer("Sizda faol kuzatuv yo'q. /start orqali qo'shing.")
        return
    rows = []
    for w in mine:
        a, b = DIRECTIONS[w["direction"]]
        d = date.fromisoformat(w["day"]).strftime("%d.%m")
        rows.append([InlineKeyboardButton(
            text=f"❌ {STATIONS[a][1]} → {STATIONS[b][1]}, {d}",
            callback_data=f"unwatch:{w['direction']}:{w['day']}")])
    await m.answer("Faol kuzatuvlaringiz (o'chirish uchun bosing):",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("unwatch:"))
async def remove_watch(c: CallbackQuery):
    _, direction, day_s = c.data.split(":")
    watches = [w for w in load_watches()
               if not (w["chat_id"] == c.from_user.id and w["direction"] == direction and w["day"] == day_s)]
    save_watches(watches)
    await c.answer("Kuzatuv o'chirildi")
    await c.message.delete()


# ---------------- Fon tekshiruvi ----------------
async def watcher(bot: Bot):
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        watches = load_watches()
        today = date.today()
        keep = []
        for w in watches:
            day = date.fromisoformat(w["day"])
            if day < today:
                continue  # o'tib ketgan sanalar o'chadi
            a, b = DIRECTIONS[w["direction"]]
            try:
                trains = await search_trains(STATIONS[a][0], STATIONS[b][0], day)
                free = total_free(trains)
                if free > 0 and w.get("last_free", 0) == 0:
                    await bot.send_message(
                        w["chat_id"],
                        "🔔 <b>Joy chiqdi!</b>\n\n" + format_trains(w["direction"], day, trains),
                        parse_mode="HTML", reply_markup=kb_result(w["direction"], w["day"]))
                w["last_free"] = free
            except Exception as e:
                log.warning("watch check failed: %s", e)
            keep.append(w)
            await asyncio.sleep(2)  # saytga ortiqcha yuk bermaslik uchun
        save_watches(keep)


async def main():
    bot = Bot(BOT_TOKEN)
    asyncio.create_task(watcher(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
