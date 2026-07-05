# Max bot

Демо-бот для мессенджера [Max](https://dev.max.ru/) на библиотеке
[`maxapi`](https://pypi.org/project/maxapi/) (async, в стиле aiogram).

## Что внутри

- `main.py` — бот: онбординг (`bot_started`), меню на inline-клавиатуре,
  редактирование клавиатуры по нажатию кнопки.
- `config.py` — токен из `.env` + SSL-контекст (certifi).
- `.env` — `MAX_BOT_TOKEN` (не коммитится).

## Запуск

```bash
poetry install
poetry run python main.py        # long polling
# или без poetry:
python -m venv .venv && .venv/bin/pip install maxapi python-dotenv certifi
.venv/bin/python main.py
```

Токен бота берётся через системного бота **MasterBot** внутри Max.

## Важные нюансы maxapi (на чём легко споткнуться)

1. **Старт — это событие, а не команда.** Онбординг вешается на
   `@dp.bot_started()` (несёт deep-link `payload`), а не только на
   `Command('start')`.

2. **Клавиатура — это вложение.** Собирается `InlineKeyboardBuilder` →
   `.as_markup()` и передаётся в `attachments=[...]`.

3. **`edit_message` перезаписывает ВЕСЬ набор вложений.** Если не передать
   `attachments`, библиотека отправит пустой список и сотрёт все вложения
   (и картинку, и клавиатуру). Чтобы поменять клавиатуру на сообщении с
   картинкой — передавай картинку (по `token`) ВМЕСТЕ с новой клавиатурой.

4. **`callback.answer()` НЕ меняет вложения** — внутри переиспользует старые.
   Чтобы сменить клавиатуру по нажатию: `send_callback(...)` для ack +
   отдельно `message.edit(attachments=[...])` (см. `on_callback` в main.py).

5. **SSL на macOS / python.org.** aiohttp не видит системные CA-сертификаты.
   Решено передачей `aiohttp.TCPConnector(ssl=ssl_context())` боту внутри
   `main()` (connector обязательно создавать в работающем event loop).
