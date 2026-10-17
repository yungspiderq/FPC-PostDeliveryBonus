"""
Follow-up сообщение после выдачи (PostDeliveryBonus) — плагин для FunPay Cardinal v0.1.17+

🎁 Через N минут после автовыдачи отправляет покупателю вежливое follow-up
сообщение: всё ли в порядке, просьба оставить отзыв и бонус для постоянных
покупателей. Повышает число отзывов и повторные продажи.

Возможности:
 • Отправка через настраиваемую задержку после выдачи (по умолчанию 60 минут) —
   сообщение приходит не мгновенно, а когда покупатель успел проверить товар.
 • Шаблон с переменными $username (ник), $lot (лот), $order (номер заказа).
 • Очередь отложенных сообщений хранится на диске — переживает рестарт Кардинала.
 • Каждому заказу — не более одного follow-up.
 • Ошибки отправки ретраятся (до 3 попыток), «протухшие» записи (>7 дней) чистятся.
 • 🧪 Кнопка «Тест» — отправить пример сообщения себе в Telegram.
 • Сообщения отправляются штатным методом Кардинала (помечаются как сообщения
   бота — автоответчик на них не срабатывает).

Команды Telegram:
 • /bonus — открыть меню плагина.

Установка: положить этот файл в папку plugins/ и перезапустить Кардинал.
Автор: yungspider
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import TYPE_CHECKING

from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B, CallbackQuery, Message
from telebot.apihelper import ApiTelegramException

try:
    from tg_bot import CBT, static_keyboards as skb, utils as tg_utils
except Exception:
    CBT = None
    skb = None
    tg_utils = None

if TYPE_CHECKING:
    from cardinal import Cardinal

logger = logging.getLogger("FPC.PostDeliveryBonus")

# ══════════════════════════════ МЕТАДАННЫЕ ПЛАГИНА ══════════════════════════════
NAME = "PostDeliveryBonus (Follow-up After Delivery)"
VERSION = "1.0.0"
DESCRIPTION = ("Follow-up сообщение покупателю через N минут после автовыдачи: всё ли в порядке, "
               "просьба оставить отзыв, бонус постоянникам. Шаблон настраивается, очередь "
               "переживает рестарт. Команда: /bonus")
CREDITS = "yungspider"
UUID = "8634f225-06cd-4b88-a2d2-45d974ab33ae"
SETTINGS_PAGE = True

# ══════════════════════════════ НАСТРОЙКИ ПЛАГИНА ═══════════════════════════════
PLUGIN_DIR = os.path.join("storage", "plugins", "post_delivery_bonus")
SETTINGS_PATH = os.path.join(PLUGIN_DIR, "settings.json")

DEFAULT_TEMPLATE = ("Здравствуйте, $username! 👋\n"
                    "Спасибо за покупку лота «$lot»!\n"
                    "Если всё в порядке — буду очень рад вашему отзыву о заказе #$order. "
                    "Если возникли вопросы или что-то не так — напишите мне в этот чат, "
                    "обязательно помогу!\n\n"
                    "🎁 Постоянным покупателям — скидка 5% на следующий заказ. "
                    "Просто напишите «постоянный» :)")

DEFAULT_SETTINGS = {
    "enabled": True,
    "delay_minutes": 60,    # задержка после выдачи
    "template": DEFAULT_TEMPLATE,
    "pending": {},          # {order_id: {"ts": float, "chat_id": ..., "username": str, "lot": str, "tries": int}}
}
DELAY_RANGE = (5, 4320)      # минут (5 мин — 3 суток)
PRUNE_AFTER = 7 * 86400      # «протухшие» записи старше 7 дней
MAX_TRIES = 3
LOOP_SLEEP = 20

# ══════════════════════════════ CALLBACK-КОДЫ ═══════════════════════════════════
CB_HOME = "pdb:home"
CB_TOG = "pdb:tog"
CB_DELAY = "pdb:delay"
CB_TMPL = "pdb:tmpl"
CB_TEST = "pdb:test"
CB_NOOP = "pdb:noop"
ST_DELAY = "pdb:st:delay"
ST_TMPL = "pdb:st:tmpl"

_stop_event = threading.Event()
_settings_lock = threading.Lock()
_send_lock = threading.Lock()
_cardinal: "Cardinal | None" = None


# ══════════════════════════════ УТИЛИТЫ ═════════════════════════════════════════
def _clamp(value, min_v, max_v):
    return max(min_v, min(max_v, value))


def _load_settings() -> dict:
    with _settings_lock:
        data = {}
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                logger.error("PostDeliveryBonus: не удалось прочитать настройки, использую значения по умолчанию.")
                logger.debug("TRACEBACK", exc_info=True)
                data = {}
        settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        if isinstance(data, dict):
            settings.update({k: v for k, v in data.items() if k in settings})
        settings["enabled"] = bool(settings["enabled"])
        try:
            settings["delay_minutes"] = _clamp(int(settings["delay_minutes"]), *DELAY_RANGE)
        except (TypeError, ValueError):
            settings["delay_minutes"] = DEFAULT_SETTINGS["delay_minutes"]
        if not isinstance(settings["template"], str) or not settings["template"].strip():
            settings["template"] = DEFAULT_TEMPLATE
        if not isinstance(settings["pending"], dict):
            settings["pending"] = {}
        return settings


def _save_settings(settings: dict) -> None:
    with _settings_lock:
        try:
            os.makedirs(PLUGIN_DIR, exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error("PostDeliveryBonus: не удалось сохранить настройки.")
            logger.debug("TRACEBACK", exc_info=True)


def _short(text: str, limit: int = 40) -> str:
    text = str(text or "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _escape(text) -> str:
    if tg_utils:
        return tg_utils.escape(str(text))
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plugin_enabled(crd: "Cardinal") -> bool:
    try:
        return crd.plugins[UUID].enabled
    except Exception:
        return False


def _fill_template(template: str, username: str, lot: str, order_id: str) -> str:
    return (template.replace("$username", str(username or "друг"))
            .replace("$lot", str(lot or "товар"))
            .replace("$order", str(order_id or "")))


# ══════════════════════════════ ОЧЕРЕДЬ СООБЩЕНИЙ ═══════════════════════════════
def post_delivery_handler(c: "Cardinal", e, *args) -> None:
    """BIND_TO_POST_DELIVERY: ставит follow-up в очередь после успешной выдачи."""
    try:
        if not _plugin_enabled(c):
            return
        settings = _load_settings()
        if not settings["enabled"]:
            return
        if not getattr(e, "delivered", False):
            return
        order = getattr(e, "order", None)
        if order is None:
            return
        oid = str(order.id)
        if oid in settings["pending"]:
            return
        settings["pending"][oid] = {
            "ts": time.time(),
            "chat_id": order.chat_id,
            "username": str(order.buyer_username or ""),
            "lot": str(order.description or ""),
            "tries": 0,
        }
        _save_settings(settings)
        logger.debug(f"PostDeliveryBonus: follow-up по заказу #{oid} поставлен в очередь.")
    except Exception:
        logger.error("PostDeliveryBonus: ошибка в обработчике выдачи.")
        logger.debug("TRACEBACK", exc_info=True)


def _bonus_tick(crd: "Cardinal", now_ts: float | None = None) -> list:
    """Один проход очереди: отправляет созревшие follow-up'ы. Возвращает список отправленных order_id."""
    settings = _load_settings()
    now_ts = now_ts if now_ts is not None else time.time()
    delay_s = settings["delay_minutes"] * 60
    sent_ids = []
    changed = False
    for oid, entry in list(settings["pending"].items()):
        if _stop_event.is_set():
            break
        try:
            ts = float(entry.get("ts", 0))
        except (TypeError, ValueError):
            ts = 0.0
        age = now_ts - ts
        # «протухшие» записи (старше 7 дней) выбрасываем НЕ отправляя
        if age > PRUNE_AFTER:
            del settings["pending"][oid]
            changed = True
            logger.info(f"PostDeliveryBonus: запись #{oid} устарела (>7 дней) — удалена без отправки.")
            continue
        if age < delay_s:
            continue
        text = _fill_template(settings["template"], entry.get("username"),
                              entry.get("lot"), oid)
        ok = False
        with _send_lock:
            try:
                crd.account.send_message(entry.get("chat_id"), text)
                ok = True
            except Exception:
                logger.error(f"PostDeliveryBonus: не удалось отправить follow-up по заказу #{oid}.")
                logger.debug("TRACEBACK", exc_info=True)
        if ok:
            del settings["pending"][oid]
            sent_ids.append(oid)
            logger.info(f"PostDeliveryBonus: follow-up по заказу #{oid} отправлен "
                        f"(покупатель {entry.get('username')}).")
        else:
            entry["tries"] = int(entry.get("tries", 0)) + 1
            if entry["tries"] >= MAX_TRIES:
                del settings["pending"][oid]
                logger.error(f"PostDeliveryBonus: follow-up по заказу #{oid} отброшен после "
                             f"{MAX_TRIES} попыток.")
        changed = True
        time.sleep(1)
    if changed:
        _save_settings(settings)
    return sent_ids


def _bonus_loop(crd: "Cardinal") -> None:
    logger.info("PostDeliveryBonus: очередь follow-up сообщений запущена.")
    while not _stop_event.wait(LOOP_SLEEP):
        try:
            if not _plugin_enabled(crd) or not crd.account.is_initiated:
                continue
            if not _load_settings()["enabled"]:
                continue
            _bonus_tick(crd)
        except Exception:
            logger.error("PostDeliveryBonus: ошибка в цикле очереди.")
            logger.debug("TRACEBACK", exc_info=True)


# ══════════════════════════ TELEGRAM-ИНТЕРФЕЙС ══════════════════════════════════
def _home_text(crd: "Cardinal") -> str:
    settings = _load_settings()
    state = "ВКЛ 🟢" if settings["enabled"] else "ВЫКЛ 🔴"
    return (f"🎁 <b>{_escape(NAME)}</b> <i>v{VERSION}</i>\n\n"
            f"Через заданное время после автовыдачи покупатель получит follow-up: "
            f"всё ли в порядке, просьба оставить отзыв и бонус постоянникам.\n\n"
            f"Follow-up сообщения: <b>{state}</b>\n"
            f"⏱ Задержка после выдачи: <b>{settings['delay_minutes']}</b> мин.\n"
            f"📮 В очереди сейчас: <b>{len(settings['pending'])}</b>\n\n"
            f"<b>Шаблон</b> (переменные: <code>$username</code>, <code>$lot</code>, "
            f"<code>$order</code>):\n<code>{_escape(_short(settings['template'], 300))}</code>")


def _home_kb() -> K:
    settings = _load_settings()
    kb = K(row_width=2)
    kb.add(B(f"{'🟢 Включено' if settings['enabled'] else '🔴 Выключено'}", None, CB_TOG))
    kb.add(B(f"⏱ Задержка: {settings['delay_minutes']} мин.", None, CB_DELAY))
    kb.add(B("📝 Текст сообщения", None, CB_TMPL))
    kb.add(B("🧪 Тест (отправить себе)", None, CB_TEST))
    kb.add(B("🔄 Обновить меню", None, CB_HOME))
    if CBT:
        kb.add(B("🔙 Назад к плагину", None, f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
    return kb


def _answer(bot, c: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    try:
        bot.answer_callback_query(c.id, text, show_alert=alert)
    except Exception:
        pass


def _edit_or_send(bot, c: CallbackQuery, text: str, kb: K | None) -> None:
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.id, parse_mode="HTML", reply_markup=kb)
    except ApiTelegramException as e:
        if "message is not modified" not in str(e):
            try:
                bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)
    except Exception:
        try:
            bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)


def init(crd: "Cardinal", *args) -> None:
    """Инициализация плагина (BIND_TO_PRE_INIT)."""
    global _cardinal
    _cardinal = crd
    _load_settings()

    tg = crd.telegram
    if tg is not None:
        bot = tg.bot

        def open_home(c: CallbackQuery) -> None:
            _answer(bot, c)
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def toggle_enabled(c: CallbackQuery) -> None:
            settings = _load_settings()
            settings["enabled"] = not settings["enabled"]
            _save_settings(settings)
            _answer(bot, c, f"🎁 Follow-up сообщения: {'ВКЛ' if settings['enabled'] else 'ВЫКЛ'}")
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def act_delay(c: CallbackQuery) -> None:
            s = _load_settings()
            result = bot.send_message(
                c.message.chat.id,
                f"⏱ Введите задержку follow-up сообщения после выдачи в минутах "
                f"(<code>{DELAY_RANGE[0]}</code> - <code>{DELAY_RANGE[1]}</code>).\n\n"
                f"Текущее значение: <b>{s['delay_minutes']}</b> мин.",
                parse_mode="HTML", reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, ST_DELAY)
            _answer(bot, c)

        def act_template(c: CallbackQuery) -> None:
            result = bot.send_message(
                c.message.chat.id,
                "📝 Отправьте новый текст follow-up сообщения.\n\n"
                "Переменные:\n<code>$username</code> — ник покупателя\n"
                "<code>$lot</code> — название лота\n<code>$order</code> — номер заказа",
                parse_mode="HTML", reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, ST_TMPL)
            _answer(bot, c)

        def send_test(c: CallbackQuery) -> None:
            settings = _load_settings()
            sample = _fill_template(settings["template"], "Иван", "Аккаунт Steam с играми", "TEST1234")
            try:
                bot.send_message(c.message.chat.id, f"🧪 <i>Так выглядит follow-up "
                                                    f"(на самом деле он без HTML-разметки):</i>\n\n"
                                 f"{_escape(sample)}", parse_mode="HTML")
                _answer(bot, c, "🧪 Пример отправлен выше.")
            except Exception:
                _answer(bot, c, "Не удалось отправить пример.", alert=True)

        def _state_eq(m: Message, state: str) -> bool:
            try:
                return tg.check_state(m.chat.id, m.from_user.id, state)
            except Exception:
                return False

        def edit_delay(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            try:
                value = _clamp(int(str(m.text).strip()), *DELAY_RANGE)
            except (ValueError, AttributeError):
                bot.reply_to(m, "❌ Нужно ввести число минут. Попробуйте еще раз.")
                return
            settings = _load_settings()
            settings["delay_minutes"] = value
            _save_settings(settings)
            bot.reply_to(m, f"✅ Задержка follow-up: <b>{value}</b> мин.", parse_mode="HTML")

        def edit_template(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            text = str(m.text or "").strip()
            if not text:
                bot.reply_to(m, "❌ Пустой текст не сохранен.")
                return
            settings = _load_settings()
            settings["template"] = text[:1500]
            _save_settings(settings)
            bot.reply_to(m, "📝 Текст follow-up сообщения сохранен.", parse_mode="HTML")

        def cmd_open(m: Message) -> None:
            bot.send_message(m.chat.id, _home_text(crd), parse_mode="HTML", reply_markup=_home_kb())

        def noop(c: CallbackQuery) -> None:
            _answer(bot, c)

        tg.cbq_handler(open_home, lambda c: c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
        tg.cbq_handler(open_home, lambda c: c.data == CB_HOME)
        tg.cbq_handler(toggle_enabled, lambda c: c.data == CB_TOG)
        tg.cbq_handler(act_delay, lambda c: c.data == CB_DELAY)
        tg.cbq_handler(act_template, lambda c: c.data == CB_TMPL)
        tg.cbq_handler(send_test, lambda c: c.data == CB_TEST)
        tg.cbq_handler(noop, lambda c: c.data == CB_NOOP)
        tg.msg_handler(edit_delay, func=lambda m: _state_eq(m, ST_DELAY))
        tg.msg_handler(edit_template, func=lambda m: _state_eq(m, ST_TMPL))
        tg.msg_handler(cmd_open, commands=["bonus"])

        try:
            crd.add_telegram_commands(UUID, [("bonus", "Follow-up сообщения после выдачи.", True)])
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)

        logger.info(f"PostDeliveryBonus v{VERSION}: Telegram-меню зарегистрировано (команда /bonus).")
    else:
        logger.info(f"PostDeliveryBonus v{VERSION}: Telegram выключен — очередь работает в фоне.")

    threading.Thread(target=_bonus_loop, args=(crd,), daemon=True).start()


def on_delete(crd: "Cardinal", c=None) -> None:
    """Вызывается при удалении плагина через ПУ (BIND_TO_DELETE)."""
    _stop_event.set()


# ══════════════════════════ ПРИВЯЗКА К СОБЫТИЯМ КАРДИНАЛА ═══════════════════════
BIND_TO_PRE_INIT = [init]
BIND_TO_POST_DELIVERY = [post_delivery_handler]
BIND_TO_DELETE = on_delete
