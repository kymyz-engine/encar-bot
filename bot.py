"""Telegram bot for searching cars on encar.com."""
import asyncio
import json
import logging
import os
from html import escape
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

from encar import FUELS, Car, Encar, Filters, Option, describe, parse_range

PAGE_SIZE = 5  # listings per batch
PICK_PAGE = 16  # buttons per picker page
POLL_SECONDS = 120  # how often subscriptions are checked
SCAN_CAP = 5000  # max listings tracked per subscription
MAX_NOTIFY = 10  # max listings sent per check, the rest is summarised
SUBS_FILE = Path(os.getenv("DATA_DIR") or Path(__file__).parent) / "subs.json"

load_dotenv()  # must run before Encar() reads the proxy settings
router = Router()
encar = Encar()
filters: dict[int, Filters] = {}
picks: dict[int, tuple[str, list[Option]]] = {}  # user -> (field, options of the last list)
offsets: dict[int, int] = {}
subs: dict[int, dict] = {}  # user -> {"filters": dict, "seen": [ids]}


class Input(StatesGroup):
    price = State()
    year = State()


def reason(e: Exception) -> str:
    status = getattr(getattr(e, "response", None), "status_code", None)
    return f"HTTP {status}" if status else type(e).__name__


def flt(uid: int) -> Filters:
    return filters.setdefault(uid, Filters())


def main_menu(f: Filters, watching: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Brand", callback_data="open:brand")
    kb.button(text="Model", callback_data="open:group")
    kb.button(text="Generation", callback_data="open:gen")
    kb.button(text="Fuel", callback_data="open:fuel")
    kb.button(text="Price", callback_data="open:price")
    kb.button(text="Year", callback_data="open:year")
    kb.adjust(3, 3)
    kb.row(InlineKeyboardButton(text="Search", callback_data="go"))
    kb.row(
        InlineKeyboardButton(text="Stop watching", callback_data="unwatch")
        if watching
        else InlineKeyboardButton(text="Notify me about new listings", callback_data="watch")
    )
    kb.row(InlineKeyboardButton(text="Reset all", callback_data="reset"))
    return kb.as_markup()


def menu_text(f: Filters, uid: int) -> str:
    text = f"<b>Search parameters</b>\n\n{escape(describe(f))}"
    if uid in subs:
        text += "\n\n<b>Watching:</b>\n" + escape(describe(Filters.from_dict(subs[uid]["filters"])))
    return text


async def show_menu(target: Message | CallbackQuery, f: Filters) -> None:
    uid = target.from_user.id
    text, kb = menu_text(f, uid), main_menu(f, uid in subs)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb)
        except Exception:  # message unchanged / too old
            await target.message.answer(text, reply_markup=kb)
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb)


@router.message(Command("start", "menu"))
async def start(m: Message, state: FSMContext) -> None:
    await state.clear()
    await m.answer(
        "Hi! I search for cars on <b>encar.com</b>.\n"
        "Set the parameters and press Search."
    )
    await show_menu(m, flt(m.from_user.id))


@router.callback_query(F.data == "reset")
async def reset(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    filters[c.from_user.id] = Filters()
    await show_menu(c, flt(c.from_user.id))


@router.callback_query(F.data == "menu")
async def menu(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show_menu(c, flt(c.from_user.id))


# ---------- pickers (brand / model / generation / fuel) ----------

LEVELS = {"brand": "Manufacturer", "group": "ModelGroup", "gen": "Model"}
TITLES = {"brand": "a brand", "group": "a model", "gen": "a generation", "fuel": "a fuel type"}


def picker_kb(field: str, options: list[Option], page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    chunk = options[page * PICK_PAGE : (page + 1) * PICK_PAGE]
    for i, o in enumerate(chunk, start=page * PICK_PAGE):
        text = f"{o.label} ({o.count})" if o.count else o.label
        kb.button(text=text[:60], callback_data=f"pick:{i}")
    kb.adjust(2)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="Prev", callback_data=f"page:{page - 1}"))
    if (page + 1) * PICK_PAGE < len(options):
        nav.append(InlineKeyboardButton(text="Next", callback_data=f"page:{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.row(
        InlineKeyboardButton(text="Any", callback_data=f"clear:{field}"),
        InlineKeyboardButton(text="Back", callback_data="menu"),
    )
    return kb.as_markup()


@router.callback_query(F.data.startswith("open:"))
async def open_field(c: CallbackQuery, state: FSMContext) -> None:
    field = c.data.split(":")[1]
    uid, f = c.from_user.id, flt(c.from_user.id)
    await state.clear()

    if field in ("price", "year"):
        await state.set_state(Input.price if field == "price" else Input.year)
        hint = (
            "Enter a price range in 만원 (10,000 ₩), e.g. <code>1000-3000</code>, "
            "<code>-2500</code> (up to) or <code>1500-</code> (from)."
            if field == "price"
            else "Enter a production year range, e.g. <code>2019-2022</code>, "
            "<code>-2020</code> (up to) or <code>2018-</code> (from)."
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="Any", callback_data=f"clear:{field}")
        kb.button(text="Back", callback_data="menu")
        await c.message.edit_text(hint, reply_markup=kb.as_markup())
        return await c.answer()

    if field == "fuel":
        options = [Option(ru, f"FuelType.{ev}.", ev) for ev, ru in FUELS]
    else:
        need = {"group": f.brand, "gen": f.group}.get(field, True)
        if not need:
            return await c.answer(
                "Pick " + ("a brand" if field == "group" else "a model") + " first", show_alert=True
            )
        await c.answer("Loading the list…")
        try:
            options = await encar.facets(f, LEVELS[field])
        except Exception as e:
            logging.exception("facets")
            return await c.message.answer(f"Encar is not responding right now, please try again. ({reason(e)})")
    picks[uid] = (field, options)
    await c.message.edit_text(
        f"Choose {TITLES[field]}:" if options else "No options found.",
        reply_markup=picker_kb(field, options, 0),
    )
    await c.answer()


@router.callback_query(F.data.startswith("page:"))
async def change_page(c: CallbackQuery) -> None:
    field, options = picks.get(c.from_user.id, (None, []))
    if not field:
        return await c.answer("This list is outdated, open it again", show_alert=True)
    await c.message.edit_reply_markup(reply_markup=picker_kb(field, options, int(c.data.split(":")[1])))
    await c.answer()


@router.callback_query(F.data.startswith("pick:"))
async def pick(c: CallbackQuery) -> None:
    uid, f = c.from_user.id, flt(c.from_user.id)
    field, options = picks.get(uid, (None, []))
    idx = int(c.data.split(":")[1])
    if not field or idx >= len(options):
        return await c.answer("This list is outdated, open it again", show_alert=True)
    setattr(f, field, options[idx])
    _reset_children(f, field)
    await show_menu(c, f)


def _reset_children(f: Filters, field: str) -> None:
    if field == "brand":
        f.group = f.gen = None
    elif field == "group":
        f.gen = None


@router.callback_query(F.data.startswith("clear:"))
async def clear(c: CallbackQuery, state: FSMContext) -> None:
    field, f = c.data.split(":")[1], flt(c.from_user.id)
    await state.clear()
    if field in ("price", "year"):
        setattr(f, field, (None, None))
    else:
        setattr(f, field, None)
        _reset_children(f, field)
    await show_menu(c, f)


# ---------- price / year input ----------

@router.message(Input.price)
async def got_price(m: Message, state: FSMContext) -> None:
    try:
        flt(m.from_user.id).price = parse_range(m.text or "", 0, 10**6)
    except ValueError:
        return await m.answer("Could not parse that. Example: <code>1000-3000</code>")
    await state.clear()
    await show_menu(m, flt(m.from_user.id))


@router.message(Input.year)
async def got_year(m: Message, state: FSMContext) -> None:
    try:
        flt(m.from_user.id).year = parse_range(m.text or "", 1950, 2100)
    except ValueError:
        return await m.answer("Could not parse that. Example: <code>2019-2022</code>")
    await state.clear()
    await show_menu(m, flt(m.from_user.id))


# ---------- search ----------

def n(x: int) -> str:
    return f"{x:,}".replace(",", " ")


def fmt(car: Car) -> str:
    return (
        f"<b>{escape(car.title)}</b>\n"
        f"📅 Year/month: {car.year_month}\n"
        f"⛽ Fuel: {escape(car.fuel)}\n"
        f"🛣 Mileage: {n(car.mileage)} km\n"
        f"💥 Insurance claims: {car.accidents}\n"
        f"⚙️ Trim: {escape(car.trim) or '—'}\n"
        f"💰 Price: <b>{n(car.price)} 만원</b> (₩{n(car.price * 10000)})\n"
        f"🕒 Listed: {car.published or '—'}\n"
        f'🔗 <a href="{car.url}">Open on Encar</a>'
    )


def more_kb(has_more: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if has_more:
        kb.button(text="Show more", callback_data="more")
    kb.button(text="Parameters", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


async def send_car(bot: Bot, chat_id: int, car: Car) -> None:
    text = fmt(car)
    try:
        if car.photo:
            await bot.send_photo(chat_id, car.photo, caption=text)
        else:
            await bot.send_message(chat_id, text)
    except TelegramForbiddenError:
        raise
    except Exception:  # photo unavailable, send as text
        await bot.send_message(chat_id, text)


async def send_page(msg: Message, uid: int) -> None:
    f, offset = flt(uid), offsets.get(uid, 0)
    try:
        total, cars = await encar.page(f, offset, PAGE_SIZE)
    except Exception as e:
        logging.exception("search")
        return await msg.answer(f"Encar is not responding right now, please try again. ({reason(e)})")
    if not cars:
        return await msg.answer("Nothing found. Change the parameters.", reply_markup=more_kb(False))
    for car in cars:
        await send_car(msg.bot, msg.chat.id, car)
    offsets[uid] = offset + len(cars)
    left = total - offsets[uid]
    await msg.answer(
        f"Showing {offsets[uid]} of {total}.", reply_markup=more_kb(left > 0)
    )


@router.callback_query(F.data == "go")
async def go(c: CallbackQuery) -> None:
    uid = c.from_user.id
    offsets[uid] = 0
    await c.answer("Searching…")
    await c.message.bot.send_chat_action(c.message.chat.id, "typing")
    await send_page(c.message, uid)


@router.callback_query(F.data == "more")
async def more(c: CallbackQuery) -> None:
    await c.answer("Loading…")
    await c.message.edit_reply_markup(reply_markup=None)
    await send_page(c.message, c.from_user.id)


# ---------- new-listing notifications ----------

def save_subs() -> None:
    SUBS_FILE.write_text(json.dumps({str(u): v for u, v in subs.items()}), encoding="utf8")


def load_subs() -> None:
    if SUBS_FILE.exists():
        subs.update({int(u): v for u, v in json.loads(SUBS_FILE.read_text(encoding="utf8")).items()})


@router.callback_query(F.data == "watch")
async def watch(c: CallbackQuery) -> None:
    uid, f = c.from_user.id, flt(c.from_user.id)
    await c.answer("Subscribing…")
    try:
        total, rows = await encar.scan(f, SCAN_CAP)
    except Exception as e:
        logging.exception("watch")
        return await c.message.answer(f"Encar is not responding right now, please try again. ({reason(e)})")
    subs[uid] = {"filters": f.to_dict(), "seen": list(rows)}
    save_subs()
    note = f"Watching started. {total} current listings are ignored, I will message you about new ones."
    if total > SCAN_CAP:
        note += f"\nThe filter is broad ({total} listings), only the first {SCAN_CAP} are tracked. Narrow it down for better coverage."
    await c.message.answer(note)
    await show_menu(c, f)


@router.callback_query(F.data == "unwatch")
async def unwatch(c: CallbackQuery) -> None:
    subs.pop(c.from_user.id, None)
    save_subs()
    await show_menu(c, flt(c.from_user.id))


async def check_sub(bot: Bot, uid: int) -> None:
    sub = subs[uid]
    _, rows = await encar.scan(Filters.from_dict(sub["filters"]), SCAN_CAP)
    seen = set(sub["seen"])
    new = [r for i, r in rows.items() if i not in seen]
    if not new:
        return
    cars = await encar.cars(new[:MAX_NOTIFY])
    await bot.send_message(uid, f"New listings: {len(new)}")
    for car in cars:
        await send_car(bot, uid, car)
    if len(new) > MAX_NOTIFY:
        await bot.send_message(uid, f"...and {len(new) - MAX_NOTIFY} more. Use Search to see them all.")
    sub["seen"] = list(seen | set(rows))[-20000:]  # only after delivery, so nothing is lost on errors
    save_subs()


async def monitor(bot: Bot) -> None:
    while True:
        await asyncio.sleep(POLL_SECONDS)
        for uid in list(subs):
            try:
                await check_sub(bot, uid)
            except TelegramForbiddenError:  # user blocked the bot
                subs.pop(uid, None)
                save_subs()
            except Exception:
                logging.exception("monitor %s", uid)


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Set BOT_TOKEN in .env (token from @BotFather)")
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    load_subs()
    watcher = asyncio.create_task(monitor(bot))
    try:
        await dp.start_polling(bot)
    finally:
        watcher.cancel()
        await encar.close()


if __name__ == "__main__":
    asyncio.run(main())
