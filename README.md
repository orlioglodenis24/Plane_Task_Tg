# Plane_Task_Tg

Plane → Telegram. 
Сообщение в Telegram, когда задача в Plane, назначенная на вас, переходит в статус «В работе».
Работает в GitHub Actions по расписанию (раз в 5 минут; GitHub может задерживать запуск), статусы между запусками хранятся в кэше Actions.

## Настройка

Settings → Secrets and variables → Actions:

**Secrets**
- `PLANE_API_KEY` — Plane: Profile → Settings → API tokens
- `TG_BOT_TOKEN` — токен бота от @BotFather (боту нужно один раз написать `/start`)
- `TG_CHAT_ID` — ваш Telegram id (узнать: написать @userinfobot)

**Variables**
- `PLANE_URL` — адрес вашего Plane, например `https://plane.example.com`
- `PLANE_WORKSPACE` — slug воркспейса (часть адреса после домена)
- `TARGET_STATES` — необязательно; названия статусов через запятую, по умолчанию `In Progress,В работе`

Первый запуск: Actions → «Plane → Telegram» → Run workflow. Придёт сообщение «подключён»,
а в логе запуска — список статусов в проектах (пригодится, если «В работе» у вас называется иначе).

## Команды бота

- `/mytask` — ваши активные задачи, сгруппированные по статусам (приоритет, срок, просрочка ⚠️)
- `/mytask all` — вместе с Done и Cancelled

Бот отвечает только на сообщения из `TG_CHAT_ID`. Отдельного сервера у бота нет,
команды читаются при очередном запуске workflow, поэтому ответ приходит через 5–15 минут.

## Локальный запуск

```bash
PLANE_URL=https://plane.example.com PLANE_WORKSPACE=my-workspace \
PLANE_API_KEY=... TG_BOT_TOKEN=... TG_CHAT_ID=... python3 notify.py
```
