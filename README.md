# Plane → Telegram

Сообщение в Telegram, когда задача в Plane, назначенная на вас, переходит в статус «В работе».
Работает в GitHub Actions по расписанию (раз в 10 минут), статусы между запусками хранятся в кэше Actions.

## Настройка

Settings → Secrets and variables → Actions:

**Secrets**
- `PLANE_API_KEY` — Plane: Profile → Settings → API tokens
- `TG_BOT_TOKEN` — токен бота от @BotFather (боту нужно один раз написать `/start`)
- `TG_CHAT_ID` — ваш Telegram id (узнать: написать @userinfobot)

**Variables**
- `PLANE_URL` — `https://plane.itcomp.space`
- `PLANE_WORKSPACE` — `itcomp`
- `TARGET_STATES` — необязательно; названия статусов через запятую, по умолчанию `In Progress,В работе`

Первый запуск: Actions → «Plane → Telegram» → Run workflow. Придёт сообщение «подключён»,
а в логе запуска — список статусов в проектах (пригодится, если «В работе» у вас называется иначе).

## Локальный запуск

```bash
PLANE_URL=https://plane.itcomp.space PLANE_WORKSPACE=itcomp \
PLANE_API_KEY=... TG_BOT_TOKEN=... TG_CHAT_ID=... python3 notify.py
```
