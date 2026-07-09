"""Inline-клавиатуры бота."""
from typing import Any

from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder


def _rows_by_two(buttons: list) -> list[list]:
    """Разложить кнопки по 2 в ряд (последняя одна, если нечётно)."""
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def cities_keyboard(cities: list[dict[str, Any]]):
    """Список городов по 2 в ряд. payload = city:<id>."""
    kb = InlineKeyboardBuilder()
    buttons = [
        CallbackButton(text=city["name"], payload=f"city:{city['id']}")
        for city in cities
    ]
    for row in _rows_by_two(buttons):
        kb.row(*row)
    return kb.as_markup()


def main_menu_keyboard(city_id: int, is_manager: bool = False):
    """Главное меню мероприятия. payload несёт city_id, чтобы не хранить состояние.

    Менеджерам (is_manager) добавляем кнопку коллективной рассылки.
    """
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="Программа", payload=f"prog:{city_id}"))
    kb.row(CallbackButton(text="Правила / FAQ", payload=f"faq:{city_id}"))
    kb.row(CallbackButton(text="Контакты организаторов", payload=f"contacts:{city_id}"))
    kb.row(CallbackButton(text="Схема проезда", payload=f"map:{city_id}"))
    kb.row(CallbackButton(text="Изменить город", payload=f"change_city:{city_id}"))
    if is_manager:
        kb.row(CallbackButton(text="📢 Коллективная рассылка", payload=f"bcast:{city_id}"))
    return kb.as_markup()


def change_city_keyboard(city_id: int, is_manager: bool = False):
    """Кнопка смены города (для экрана без программы); менеджерам — ещё рассылка."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="Изменить город", payload=f"change_city:{city_id}"))
    if is_manager:
        kb.row(CallbackButton(text="📢 Коллективная рассылка", payload=f"bcast:{city_id}"))
    return kb.as_markup()


def broadcast_cities_keyboard(cities: list[dict[str, Any]], back_city_id: int):
    """Выбор города-получателя рассылки: по кнопке на город + «Все города».

    payload: bcc:<back_city_id>:<target_city_id> для города, bcall:<back_city_id>
    для всех. back_city_id — город менеджера, чтобы вернуться в меню по «Отмене».
    """
    kb = InlineKeyboardBuilder()
    buttons = [
        CallbackButton(
            text=city["name"],
            payload=f"bcc:{back_city_id}:{city['id']}",
        )
        for city in cities
    ]
    for row in _rows_by_two(buttons):
        kb.row(*row)
    kb.row(CallbackButton(text="🌍 Все города", payload=f"bcall:{back_city_id}"))
    kb.row(CallbackButton(text="⬅️ Отмена", payload=f"bccancel:{back_city_id}"))
    return kb.as_markup()


def broadcast_cancel_keyboard(city_id: int):
    """Одна кнопка отмены рассылки (на экране ожидания сообщения)."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="⬅️ Отмена", payload=f"bccancel:{city_id}"))
    return kb.as_markup()


def broadcast_confirm_keyboard(city_id: int):
    """Подтверждение/отмена рассылки. payload: bcsend:<city_id> / bccancel:<city_id>."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(text="✅ Подтвердить", payload=f"bcsend:{city_id}"),
        CallbackButton(text="❌ Отменить", payload=f"bccancel:{city_id}"),
    )
    return kb.as_markup()


def _format_date(value: str) -> str:
    """'2026-07-13' -> '13.07.2026' (если формат другой — вернём как есть)."""
    try:
        from datetime import datetime

        return datetime.strptime(value, "%Y-%m-%d").strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return value


def days_keyboard(city_id: int, days: list[dict[str, Any]]):
    """Дни по 2 в ряд (последний один, если нечётно), затем полная программа и меню."""
    kb = InlineKeyboardBuilder()
    pair: list = []
    for i, day in enumerate(days, start=1):
        pair.append(
            CallbackButton(
                text=f"{_format_date(day['date'])}",
                payload=f"day:{city_id}:{day['id']}",
            )
        )
        if len(pair) == 2:
            kb.row(*pair)
            pair = []
    if pair:
        kb.row(*pair)
    kb.row(
        CallbackButton(
            text="Полная программа", payload=f"fullprog:{city_id}"
        )
    )
    kb.row(CallbackButton(text="⬅️ В меню", payload=f"menu:{city_id}"))
    return kb.as_markup()


def back_to_menu_keyboard(city_id: int):
    """Одна кнопка возврата в главное меню (навигация внутри одного сообщения)."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="⬅️ В меню", payload=f"menu:{city_id}"))
    return kb.as_markup()


def broadcast_menu_keyboard(city_id: int):
    """Кнопка «В меню» под сообщением рассылки.

    Payload bmenu:<city_id> — в отличие от back_to_menu_keyboard, по нажатию
    клавиатура снимается с самого сообщения рассылки и меню приходит ОТДЕЛЬНЫМ
    сообщением (само сообщение рассылки в истории остаётся).
    """
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="⬅️ В меню", payload=f"bmenu:{city_id}"))
    return kb.as_markup()
