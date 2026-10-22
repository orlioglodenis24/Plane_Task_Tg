# Plane_Task_Tg

Plane → Telegram. 
Сообщение в Telegram об изменениях в задачах Plane, назначенных на вас: статус, приоритет, сроки,
исполнители, метки, название, описание, новые комментарии (кроме ваших) и назначение новой задачи.
Работает в GitHub Actions по расписанию (раз в 10 минут; GitHub может задерживать запуск),
снимки задач между запусками хранятся в кэше Actions.

## Настройка

Settings → Secrets and variables → Actions:

**Secrets**
- `PLANE_API_KEY` — Plane: Profile → Settings → API tokens
- `TG_BOT_TOKEN` — токен бота от @BotFather (боту нужно один раз написать `/start`)
- `TG_CHAT_ID` — ваш Telegram id (узнать: написать @userinfobot)

**Variables**
- `PLANE_URL` — адрес вашего Plane, например `https://plane.example.com`
- `PLANE_WORKSPACE` — slug воркспейса (часть адреса после домена)
- `TARGET_STATES` — необязательно; статусы, при переходе в которые приходит полная карточка задачи
  (через запятую, `*` — любой; по умолчанию `In Progress,В работе`). Остальные изменения приходят коротким сообщением.

Первый запуск: Actions → «Plane → Telegram» → Run workflow. Придёт сообщение «подключён»,
а в логе запуска — список статусов в проектах (пригодится, если «В работе» у вас называется иначе).

## Команды бота

- `/mytask` — ваши активные задачи, сгруппированные по статусам (приоритет, срок, просрочка ⚠️)
- `/mytask all` — вместе с Done и Cancelled

Бот отвечает только на сообщения из `TG_CHAT_ID`. Отдельного сервера у бота нет,
команды читаются при очередном запуске workflow, поэтому ответ приходит через 10–20 минут.

## Локальный запуск

```bash
PLANE_URL=https://plane.example.com PLANE_WORKSPACE=my-workspace \
PLANE_API_KEY=... TG_BOT_TOKEN=... TG_CHAT_ID=... python3 notify.py
```
