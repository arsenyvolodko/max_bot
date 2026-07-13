"""
Бот мероприятия «Окружной образовательный интенсив» для мессенджера Max.

Сценарий онбординга:
  1. Пользователь жмёт «Старт» → bot_started.
  2. POST /api/users/ (get_or_create по user_id).
  3. Если город не выбран (city == null) → просим выбрать город из списка
     (GET /api/cities/).
  4. Пользователь выбирает город → POST /api/users/{user_id}/city/ →
     «Вы успешно присоединились к городу {city}».
  5. GET /api/cities/{city_id}/program/:
       • 404 → «Информация о проведении мероприятия появится позже…»;
       • 200 → меню с кнопками (Программа / Контакты / Схема проезда).

Запуск (long polling):  poetry run python main.py
"""
import config  # noqa: F401  — ПЕРВЫМ: применяет SSL-патч до импорта aiohttp/maxapi

import asyncio
import logging

import aiohttp
from maxapi import Bot, Dispatcher
from maxapi.context import State, StatesGroup
from maxapi.exceptions.max import MaxApiError
from maxapi.filters import StateFilter
from maxapi.enums.upload_type import UploadType
from maxapi.types import BotStarted, MessageCallback, MessageCreated
from maxapi.types.attachments.upload import AttachmentPayload, AttachmentUpload

import api
import media
from config import TOKEN
from keyboards import (
    back_to_menu_keyboard,
    broadcast_cancel_keyboard,
    broadcast_cities_keyboard,
    broadcast_confirm_keyboard,
    broadcast_menu_keyboard,
    change_city_keyboard,
    cities_keyboard,
    days_keyboard,
    main_menu_keyboard,
)


class Broadcast(StatesGroup):
    """FSM коллективной рассылки (только для менеджеров)."""

    waiting_message = State()  # ждём сообщение для рассылки
    confirming = State()       # показали предпросмотр, ждём подтверждение


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("max_bot")

bot = Bot(TOKEN)
# use_create_task=True: каждый апдейт обрабатывается в своей задаче, поэтому
# долгая коллективная рассылка не блокирует поллинг и приём сообщений от
# остальных пользователей.
dp = Dispatcher(use_create_task=True)

# --------------------------------------------------------------------------- #
#  Тексты сообщений                                                            #
# --------------------------------------------------------------------------- #
WELCOME_CHOOSE_CITY = (
    "Рады видеть вас среди участников Окружного образовательного интенсива!\n"
    "Укажите город вашего участия."
)
JOINED_CITY = "Вы успешно присоединились к городу {city}"
PROGRAM_LATER = (
    "Информация о проведении мероприятия в данном городе появится позже, "
    "мы обязательно вас уведомим."
)
SERVICE_UNAVAILABLE = "Сервис временно недоступен, попробуйте позже 🙏"
NO_CITIES = "Города пока не добавлены, попробуйте позже."
# Заглушка для разделов, текст которых ещё не заполнен на бэкенде.
SECTION_LATER = "Этот раздел появится позже."
# Подпись к схеме проезда по умолчанию (если map_description не задан).
MAP_CAPTION_DEFAULT = "Схема проезда:"

# --- Коллективная рассылка (менеджеры) ---
BCAST_CHOOSE_CITY = "Пользователям из какого города необходимо сделать рассылку?"
BCAST_PROMPT_CITY = (
    "Отправьте сюда любое сообщение (можно с картинкой, но не с файлом) "
    "и оно будет переслано всем пользователям из города {city}."
)
BCAST_PROMPT_ALL = (
    "Отправьте сюда любое сообщение (можно с картинкой, но не с файлом) "
    "и оно будет переслано всем пользователям."
)
BCAST_NO_FILES = (
    "Файлы пересылать нельзя. Отправьте текст и/или картинки одним сообщением."
)
BCAST_EMPTY = "Пустое сообщение пересылать нечего. Отправьте текст и/или картинку."
BCAST_NO_RECIPIENTS = "В выбранной аудитории пока нет пользователей."
BCAST_CANCELLED = "Рассылка отменена."
BCAST_DONE = "Рассылка завершена: доставлено {ok} из {total}."
# Предпросмотр-подтверждение. {audience} — «из города X» или «» (все города).
BCAST_CONFIRM = (
    "⬆️ Сообщение выше будет отправлено всем пользователям{audience}: "
    "{count} чел.\n\nПодтвердить рассылку?"
)
BCAST_STARTED = "Запускаю рассылку…"

# TODO: заменить на реальные контакты организаторов (поля в API нет).
CONTACTS_TEXT = (
    "Контакты организаторов:\n"
    "Иванцов Иван Владимирович\n"
    "Телефон: +79159784610\n"
    "Email: ivan@milkagency.ru\n"
    "Telegram: https://t.me/ivantsov_iv"
)


# --------------------------------------------------------------------------- #
#  Шаги сценария                                                               #
# --------------------------------------------------------------------------- #
async def ask_city(chat_id: int) -> None:
    """Показать приветствие и список городов для выбора."""
    cities = await api.get_cities()
    if not cities:
        await bot.send_message(chat_id=chat_id, text=NO_CITIES)
        return
    await bot.send_message(
        chat_id=chat_id,
        text=WELCOME_CHOOSE_CITY,
        attachments=[cities_keyboard(cities)],
    )


def menu_text(city_name: str) -> str:
    return f"Меню\n\nГород посещения мероприятия: {city_name}"


async def is_manager_user(user_id: int) -> bool:
    """Менеджер ли пользователь. POST /api/users/ идемпотентен (get_or_create)."""
    _status, user = await api.get_or_create_user(user_id)
    return bool(user.get("is_manager"))


async def menu_or_later_content(
    city_id: int, city_name: str, is_manager: bool = False
) -> tuple[str, list]:
    """Контент после выбора города: меню (если есть программа) или «позже».

    Возвращает (text, attachments) — caller сам решает send или edit.
    """
    status, _program = await api.get_program(city_id)
    if status == 404:
        # программы ещё нет — оставляем возможность сменить город
        return PROGRAM_LATER, [change_city_keyboard(city_id, is_manager)]
    return menu_text(city_name), [main_menu_keyboard(city_id, is_manager)]


# --------------------------------------------------------------------------- #
#  Хендлеры                                                                    #
# --------------------------------------------------------------------------- #
@dp.bot_started()
async def on_bot_started(event: BotStarted) -> None:
    """Старт диалога: регистрируем пользователя и ведём по сценарию."""
    user_id = event.user.user_id
    chat_id = event.chat_id
    try:
        status, user = await api.get_or_create_user(user_id)
        log.info("get_or_create_user(%s) -> %s", user_id, status)

        city = user.get("city")
        is_manager = bool(user.get("is_manager"))
        if not city:
            # города нет — независимо от 200/201 просим выбрать
            await ask_city(chat_id)
        else:
            # пользователь уже привязан к городу — сразу к меню/«позже»
            text, atts = await menu_or_later_content(
                city["id"], city["name"], is_manager
            )
            await bot.send_message(chat_id=chat_id, text=text, attachments=atts)
    except aiohttp.ClientError as e:
        log.error("backend error on start: %s", e)
        await bot.send_message(chat_id=chat_id, text=SERVICE_UNAVAILABLE)


@dp.message_callback()
async def on_callback(event: MessageCallback, context) -> None:
    """Обработка нажатий кнопок."""
    payload = event.callback.payload or ""
    chat_id = event.message.recipient.chat_id
    user_id = event.callback.user.user_id
    callback_id = event.callback.callback_id

    async def ack(toast: str | None = None) -> None:
        # Max не поддерживает пустой ответ на callback: нужен notification или
        # message. Без тоста просто не отвечаем — обратной связью служит
        # следующее за этим сообщение.
        if toast is None:
            return
        await bot.send_callback(callback_id=callback_id, notification=toast)

    try:
        # --- выбор города: редактируем сообщение в меню, подтверждаем тостом ---
        if payload.startswith("city:"):
            city_id = int(payload.split(":", 1)[1])
            user = await api.join_city(user_id, city_id)
            city_name = user["city"]["name"]
            is_manager = bool(user.get("is_manager"))
            await ack(JOINED_CITY.format(city=city_name))
            text, atts = await menu_or_later_content(city_id, city_name, is_manager)
            await event.message.edit(text=text, attachments=atts)
            return

        # --- коллективная рассылка (только менеджеры): выбор аудитории ---
        if payload.startswith("bcast:"):
            own_city_id = int(payload.split(":", 1)[1])
            await ack()
            cities = await api.get_cities()
            if not cities:
                await event.message.edit(
                    text=NO_CITIES,
                    attachments=[broadcast_cancel_keyboard(own_city_id)],
                )
                return
            await event.message.edit(
                text=BCAST_CHOOSE_CITY,
                attachments=[broadcast_cities_keyboard(cities, own_city_id)],
            )
            return

        # --- рассылка: выбран конкретный город-получатель ---
        if payload.startswith("bcc:"):
            _, own_city_id, target_city_id = payload.split(":")
            await ack()
            await start_broadcast(
                event, context, int(own_city_id), int(target_city_id)
            )
            return

        # --- рассылка: все города ---
        if payload.startswith("bcall:"):
            own_city_id = int(payload.split(":", 1)[1])
            await ack()
            await start_broadcast(event, context, own_city_id, None)
            return

        # --- рассылка: подтверждение, запуск отправки ---
        if payload.startswith("bcsend:"):
            await ack(BCAST_STARTED)
            # убираем кнопки у сводки, чтобы не нажали повторно.
            # attachments=[] обязателен: при None шорткат edit подставит
            # текущие вложения сообщения (включая клавиатуру) обратно.
            try:
                await event.message.edit(text="Рассылка запущена…", attachments=[])
            except MaxApiError:
                pass
            await do_broadcast(chat_id, context)
            return

        # --- рассылка: отмена, возврат в меню ---
        if payload.startswith("bccancel:"):
            own_city_id = int(payload.split(":", 1)[1])
            await context.clear()
            await ack(BCAST_CANCELLED)
            city = await api.get_city(own_city_id)
            text, atts = await menu_or_later_content(
                own_city_id, city["name"] if city else "—", is_manager=True
            )
            await event.message.edit(text=text, attachments=atts)
            return

        # --- кнопка «В меню» под сообщением рассылки ---
        # снимаем клавиатуру с самого сообщения (сохраняя текст и картинки),
        # затем присылаем меню отдельным сообщением.
        if payload.startswith("bmenu:"):
            await ack()
            msg_text, image_tokens, _ = _extract_broadcast_content(event.message)
            try:
                await event.message.edit(
                    text=msg_text,
                    attachments=_images_to_attachments(image_tokens) or [],
                )
            except MaxApiError as e:
                log.warning("bmenu: failed to strip keyboard: %s", e)
            await send_menu_to_user(chat_id, user_id)
            return

        # --- навигация по меню ---
        if payload.startswith(
            (
                "prog:",
                "fullprog:",
                "contacts:",
                "map:",
                "faq:",
                "day:",
                "menu:",
                "change_city:",
            )
        ):
            await ack()
            await handle_menu(event, payload)
            return

        await ack("Не понял эту кнопку 🤔")
    except aiohttp.ClientError as e:
        log.error("backend error on callback %r: %s", payload, e)
        await ack()
        await bot.send_message(chat_id=chat_id, text=SERVICE_UNAVAILABLE)


async def start_broadcast(
    event: MessageCallback,
    context,
    own_city_id: int,
    target_city_id: int | None,
) -> None:
    """Перевести менеджера в режим ожидания сообщения для рассылки.

    target_city_id = None → рассылка во все города.
    own_city_id запоминаем, чтобы вернуть менеджера в его меню после отправки.
    """
    if target_city_id is None:
        prompt = BCAST_PROMPT_ALL
        target_city_name = None
    else:
        city = await api.get_city(target_city_id)
        target_city_name = city["name"] if city else "—"
        prompt = BCAST_PROMPT_CITY.format(city=target_city_name)

    await context.set_state(Broadcast.waiting_message)
    await context.update_data(
        target_city_id=target_city_id,
        target_city_name=target_city_name,
        own_city_id=own_city_id,
        # id менеджера — чтобы не рассылать сообщение ему самому (он уже видел
        # предпросмотр), и mid prompt-сообщения — чтобы снять с него кнопку
        # «Отмена» после того, как менеджер прислал текст рассылки.
        manager_id=event.callback.user.user_id,
        prompt_mid=event.message.body.mid,
        prompt_text=prompt,
    )
    await event.message.edit(
        text=prompt, attachments=[broadcast_cancel_keyboard(own_city_id)]
    )


def _extract_broadcast_content(message) -> tuple[str | None, list[str], bool]:
    """Разобрать сообщение менеджера: (text, image_tokens, has_forbidden).

    has_forbidden=True, если есть любое вложение кроме картинки (файл, видео…).
    Вложение "share" (автопревью ссылки, которое Max подставляет к тексту)
    игнорируем: при рассылке текста получателям превью сгенерируется заново.
    """
    body = message.body
    text = (body.text if body else None) or None
    attachments = (body.attachments if body else None) or []

    image_tokens: list[str] = []
    has_forbidden = False
    for att in attachments:
        att_type = att.type.value if hasattr(att.type, "value") else att.type
        if att_type == "image":
            token = getattr(att.payload, "token", None)
            if token:
                image_tokens.append(token)
        elif att_type == "share":
            # автопревью ссылки — не файл, рассылать можно
            continue
        else:
            has_forbidden = True
    return text, image_tokens, has_forbidden


async def _show_menu_again(chat_id: int, own_city_id: int) -> None:
    """Прислать менеджеру его меню новым сообщением (после рассылки/пустой аудитории)."""
    city = await api.get_city(own_city_id)
    text, atts = await menu_or_later_content(
        own_city_id, city["name"] if city else "—", is_manager=True
    )
    await bot.send_message(chat_id=chat_id, text=text, attachments=atts)


async def send_menu_to_user(chat_id: int, user_id: int) -> None:
    """Прислать пользователю его меню новым сообщением.

    Город и роль берём из бэкенда (get_or_create_user идемпотентен). Если город
    ещё не выбран — предлагаем выбрать. Используется для кнопки «В меню» под
    рассылкой и для ответа меню на любое сообщение пользователя.
    """
    _status, user = await api.get_or_create_user(user_id)
    city = user.get("city")
    if not city:
        await ask_city(chat_id)
        return
    text, atts = await menu_or_later_content(
        city["id"], city["name"], bool(user.get("is_manager"))
    )
    await bot.send_message(chat_id=chat_id, text=text, attachments=atts)


def _without_manager(recipients: list[dict], manager_id) -> list[dict]:
    """Убрать из списка получателей самого менеджера (по user_id)."""
    if manager_id is None:
        return recipients
    return [u for u in recipients if u.get("user_id") != manager_id]


def _images_to_attachments(image_tokens: list[str]):
    """Список токенов картинок → attachments для send_message (или None)."""
    return [
        AttachmentUpload(
            type=UploadType.IMAGE, payload=AttachmentPayload(token=t)
        )
        for t in image_tokens
    ] or None


@dp.message_created(StateFilter(Broadcast.waiting_message, Broadcast.confirming))
async def on_broadcast_message(event: MessageCreated, context) -> None:
    """Сообщение менеджера в режиме рассылки → предпросмотр + подтверждение.

    Текст и картинки разрешены, файлы — нет. Здесь ничего не рассылаем:
    показываем менеджеру, как будет выглядеть сообщение, сколько получателей,
    и просим подтвердить. Принимаем сообщение и в состоянии confirming —
    тогда менеджер может переотправить (заменить) сообщение до подтверждения.
    """
    message = event.message
    chat_id = message.recipient.chat_id
    data = await context.get_data()
    target_city_id = data.get("target_city_id")
    target_city_name = data.get("target_city_name")
    own_city_id = data.get("own_city_id")
    manager_id = data.get("manager_id")

    text, image_tokens, has_forbidden = _extract_broadcast_content(message)

    if has_forbidden:
        await bot.send_message(
            chat_id=chat_id,
            text=BCAST_NO_FILES,
            attachments=[broadcast_cancel_keyboard(own_city_id)],
        )
        return
    if not text and not image_tokens:
        await bot.send_message(
            chat_id=chat_id,
            text=BCAST_EMPTY,
            attachments=[broadcast_cancel_keyboard(own_city_id)],
        )
        return
    if text and len(text) >= 4000:
        await bot.send_message(
            chat_id=chat_id,
            text="Текст слишком длинный (до 4000 символов). Сократите и пришлите снова.",
            attachments=[broadcast_cancel_keyboard(own_city_id)],
        )
        return

    try:
        recipients = await api.list_users(target_city_id)
    except aiohttp.ClientError as e:
        log.error("broadcast: list_users(%s) failed: %s", target_city_id, e)
        await bot.send_message(chat_id=chat_id, text=SERVICE_UNAVAILABLE)
        return

    # самого менеджера из аудитории исключаем: он уже видит предпросмотр,
    # дублировать ему рассылку в тот же чат не нужно.
    recipients = _without_manager(recipients, manager_id)
    count = len(recipients)
    if count == 0:
        await context.clear()
        await bot.send_message(chat_id=chat_id, text=BCAST_NO_RECIPIENTS)
        await _show_menu_again(chat_id, own_city_id)
        return

    # менеджер прислал корректное сообщение — снимаем кнопку «Отмена» с
    # prompt-сообщения, чтобы не осталось «висящей» отмены над предпросмотром.
    prompt_mid = data.get("prompt_mid")
    if prompt_mid:
        try:
            await bot.edit_message(
                message_id=prompt_mid,
                text=data.get("prompt_text") or BCAST_PROMPT_ALL,
                attachments=[],
            )
        except MaxApiError as e:
            log.warning("failed to strip prompt cancel button: %s", e)

    # Запоминаем подготовленное сообщение и переходим к подтверждению.
    await context.update_data(bcast_text=text, bcast_image_tokens=image_tokens)
    await context.set_state(Broadcast.confirming)

    # 1) показываем менеджеру само сообщение (как его увидят получатели)
    await bot.send_message(
        chat_id=chat_id, text=text, attachments=_images_to_attachments(image_tokens)
    )
    # 2) сводка + кнопки подтверждения
    audience = "" if target_city_name is None else f" из города {target_city_name}"
    await bot.send_message(
        chat_id=chat_id,
        text=BCAST_CONFIRM.format(audience=audience, count=count),
        attachments=[broadcast_confirm_keyboard(own_city_id)],
    )


@dp.message_created()
async def on_any_message(event: MessageCreated) -> None:
    """Любое сообщение пользователя → присылаем меню.

    Зарегистрирован ПОСЛЕ on_broadcast_message: диспетчер выполняет первый
    подходящий хендлер, а хендлер рассылки со StateFilter перехватывает ввод
    менеджера, когда тот в режиме рассылки. Во всех остальных случаях сюда
    попадает любое сообщение — отвечаем меню.
    """
    chat_id, user_id = event.get_ids()
    if chat_id is None or user_id is None:
        return
    try:
        await send_menu_to_user(chat_id, user_id)
    except aiohttp.ClientError as e:
        log.error("backend error on message from %s: %s", user_id, e)
        await bot.send_message(chat_id=chat_id, text=SERVICE_UNAVAILABLE)


# chat_id рассылок, выполняющихся прямо сейчас — защита от повторного клика
# «Подтвердить» (use_create_task=True → колбэки обрабатываются параллельно).
_broadcasts_in_flight: set[int] = set()


async def do_broadcast(chat_id: int, context) -> None:
    """Фактическая рассылка после подтверждения (вызывается из callback).

    Бежит по получателям; ошибка отправки одному пользователю логируется и
    НЕ прерывает рассылку остальным. По завершении — отчёт и возврат в меню.
    """
    if chat_id in _broadcasts_in_flight:
        return  # уже идёт рассылка для этого менеджера — игнорируем повтор
    _broadcasts_in_flight.add(chat_id)
    try:
        await _do_broadcast_inner(chat_id, context)
    finally:
        _broadcasts_in_flight.discard(chat_id)


async def _do_broadcast_inner(chat_id: int, context) -> None:
    data = await context.get_data()
    target_city_id = data.get("target_city_id")
    own_city_id = data.get("own_city_id")
    manager_id = data.get("manager_id")
    text = data.get("bcast_text")
    image_tokens = data.get("bcast_image_tokens") or []

    # подготовленного сообщения нет (например, повторный клик) — тихо выходим
    if not text and not image_tokens:
        await context.clear()
        return

    try:
        recipients = await api.list_users(target_city_id)
    except aiohttp.ClientError as e:
        log.error("broadcast: list_users(%s) failed: %s", target_city_id, e)
        await bot.send_message(chat_id=chat_id, text=SERVICE_UNAVAILABLE)
        return

    # менеджера-отправителя в аудиторию не включаем (см. on_broadcast_message)
    recipients = _without_manager(recipients, manager_id)
    if not recipients:
        await context.clear()
        await bot.send_message(chat_id=chat_id, text=BCAST_NO_RECIPIENTS)
        await _show_menu_again(chat_id, own_city_id)
        return

    # общие вложения (картинки) шлём всем; кнопку «В меню» добавляем на каждого
    # получателя отдельно — payload несёт его город.
    base_attachments = _images_to_attachments(image_tokens) or []

    ok = 0
    for u in recipients:
        uid = u["user_id"]
        user_city = u.get("city")
        attachments = list(base_attachments)
        # без города меню собрать нельзя — тогда шлём сообщение без кнопки
        if user_city:
            attachments.append(broadcast_menu_keyboard(user_city["id"]))
        try:
            await bot.send_message(
                user_id=uid, text=text, attachments=attachments or None
            )
            ok += 1
        except Exception as e:  # noqa: BLE001 — один сбой не должен рвать рассылку
            log.warning("broadcast to %s failed: %s", uid, e)
        # лёгкая пауза, чтобы не упереться в лимиты при большой аудитории
        await asyncio.sleep(0.05)

    await context.clear()
    log.info(
        "broadcast done: city=%s delivered %d/%d",
        target_city_id, ok, len(recipients),
    )
    await bot.send_message(
        chat_id=chat_id, text=BCAST_DONE.format(ok=ok, total=len(recipients))
    )
    await _show_menu_again(chat_id, own_city_id)


async def handle_menu(event: MessageCallback, payload: str) -> None:
    """Навигация по меню: всё живёт в одном сообщении, перерисовываем через edit."""
    message = event.message
    chat_id = message.recipient.chat_id
    parts = payload.split(":")
    section = parts[0]
    city_id = int(parts[1])

    # возврат в главное меню (показываем выбранный город в тексте)
    if section == "menu":
        city = await api.get_city(city_id)
        city_name = city["name"] if city else "—"
        is_manager = await is_manager_user(event.callback.user.user_id)
        await message.edit(
            text=menu_text(city_name),
            attachments=[main_menu_keyboard(city_id, is_manager)],
        )
        return

    # смена города: показываем список городов в том же сообщении
    if section == "change_city":
        cities = await api.get_cities()
        if not cities:
            await message.edit(
                text=NO_CITIES, attachments=[back_to_menu_keyboard(city_id)]
            )
            return
        await message.edit(
            text="Выберите город посещения мероприятия:",
            attachments=[cities_keyboard(cities)],
        )
        return

    # контакты не зависят от программы
    if section == "contacts":
        await message.edit(
            text=CONTACTS_TEXT, attachments=[back_to_menu_keyboard(city_id)]
        )
        return

    status, program = await api.get_program(city_id)
    if status == 404 or program is None:
        await message.edit(
            text=PROGRAM_LATER, attachments=[back_to_menu_keyboard(city_id)]
        )
        return

    # Программа: кнопки «Полная программа» + по дням
    if section == "prog":
        await message.edit(
            text="Программа:",
            attachments=[days_keyboard(city_id, program.get("days", []))],
        )

    # Полная программа: удаляем сообщение, шлём файл/картинку + текст, затем меню
    elif section == "fullprog":
        is_manager = await is_manager_user(event.callback.user.user_id)
        await message.delete()
        await deliver_and_menu(
            chat_id,
            city_id,
            file_urls=[program.get("schedule_file")],
            text=program.get("schedule_text"),
            is_manager=is_manager,
        )

    # Конкретный день: несколько картинок (schedule_files) + текст дня, затем меню
    elif section == "day":
        day_id = int(parts[2])
        day = next((d for d in program.get("days", []) if d["id"] == day_id), None)
        is_manager = await is_manager_user(event.callback.user.user_id)
        await message.delete()
        if day is None:
            await deliver_and_menu(
                chat_id, city_id, file_urls=None, text="День не найден.",
                is_manager=is_manager,
            )
        else:
            files = sorted(
                day.get("schedule_files", []),
                key=lambda f: f.get("order", 0),
            )
            await deliver_and_menu(
                chat_id,
                city_id,
                file_urls=[f.get("file") for f in files],
                text=day.get("schedule_text"),
                is_manager=is_manager,
            )

    # Схема проезда: картинкой через кеш токенов (без перезагрузки при повторе).
    # Подпись берём из map_description, иначе — дефолтная «Схема проезда:».
    elif section == "map":
        map_url = program.get("map_schema")
        caption = (program.get("map_description") or "").strip() or MAP_CAPTION_DEFAULT
        if not map_url:
            await message.edit(
                text="Схема проезда появится позже.",
                attachments=[back_to_menu_keyboard(city_id)],
            )
            return
        await edit_with_media(
            message,
            url=map_url,
            filename="map.png",
            text=caption,
            keyboard=back_to_menu_keyboard(city_id),
        )

    # Правила / FAQ: текст с бэкенда, иначе заглушка. Только кнопка возврата в меню.
    elif section == "faq":
        text = (program.get("faq") or "").strip() or SECTION_LATER
        await message.edit(
            text=text, attachments=[back_to_menu_keyboard(city_id)]
        )


async def edit_with_media(message, url: str, filename: str, text, keyboard) -> None:
    """Отредактировать сообщение, прикрепив media по токену из кеша.

    Если Max отверг токен (протух) — инвалидируем кеш и перезаливаем один раз.
    """
    att = await media.get_attachment(bot, url, filename=filename)
    try:
        await message.edit(text=text, attachments=[att, keyboard])
    except MaxApiError as e:
        log.warning("media token rejected (%s), re-uploading: %s", url, e)
        media.invalidate(url)
        att = await media.get_attachment(bot, url, filename=filename, force=True)
        await message.edit(text=text, attachments=[att, keyboard])


async def send_with_media_multi(chat_id: int, urls: list[str], text) -> None:
    """Отправить одно сообщение с несколькими media (картинками) и текстом.

    Токены переиспользуются из кеша. Если Max отвергнет любой из токенов —
    инвалидируем весь набор и перезаливаем один раз.
    """
    async def build(force: bool) -> list:
        return [
            await media.get_attachment(bot, url, filename="Программа", force=force)
            for url in urls
        ]

    atts = await build(force=False)
    try:
        await bot.send_message(chat_id=chat_id, text=text, attachments=atts)
    except MaxApiError as e:
        log.warning("media token rejected (%s), re-uploading: %s", urls, e)
        for url in urls:
            media.invalidate(url)
        atts = await build(force=True)
        await bot.send_message(chat_id=chat_id, text=text, attachments=atts)


async def deliver_and_menu(
    chat_id: int, city_id: int, file_urls, text, is_manager: bool = False
) -> None:
    """Прислать контент (одна/несколько картинок + текст) и затем меню.

    Используется для «Полной программы» и для конкретного дня. file_urls —
    список URL картинок (может быть пустым или содержать один/несколько файлов).
    """
    text = (text or "").strip()
    urls = [u for u in (file_urls or []) if u]
    if urls:
        await send_with_media_multi(chat_id, urls, text=text or None)
    else:
        await bot.send_message(
            chat_id=chat_id, text=text or "Контент появится позже."
        )
    city = await api.get_city(city_id)
    city_name = city["name"] if city else "—"
    await bot.send_message(
        chat_id=chat_id,
        text=menu_text(city_name),
        attachments=[main_menu_keyboard(city_id, is_manager)],
    )


# --------------------------------------------------------------------------- #
#  Точка входа                                                                 #
# --------------------------------------------------------------------------- #
async def main() -> None:
    log.info("Бот запускается (long polling)…")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.close_session()
        await api.close_session()


if __name__ == "__main__":
    asyncio.run(main())
