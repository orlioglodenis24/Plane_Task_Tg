# Plane_Task_Tg

Plane → Telegram. 
Сообщение в Telegram об изменениях в задачах Plane, назначенных на вас: статус, приоритет, сроки,
исполнители, метки, название, описание, новые комментарии (кроме ваших) и назначение новой задачи.
Работает на Vercel: Vercel Cron раз в 30 минут проходит по задачам (`api/cron.py`, расписание
в `vercel.json`), снимки задач между запусками хранятся в Upstash Redis. Команды боту приходят
вебхуком (`api/telegram.py`), поэтому бот отвечает сразу.

## Настройка на Vercel

1. `vercel link` — создать или выбрать проект.
2. Vercel → проект → Storage → Create Database → Upstash Redis → подключить к проекту
   (добавит `KV_REST_API_URL` и `KV_REST_API_TOKEN`).
3. Переменные окружения (Production):
   - `PLANE_URL` — адрес вашего Plane, например `https://plane.example.com`
   - `PLANE_WORKSPACE` — slug воркспейса (часть адреса после домена)
   - `PLANE_API_KEY` — Plane: Profile → Settings → API tokens
   - `TG_BOT_TOKEN` — токен бота от @BotFather (боту нужно один раз написать `/start`)
   - `TG_CHAT_ID` — ваш Telegram id (узнать: написать @userinfobot)
   - `CRON_SECRET` — любая случайная строка, ею Vercel подписывает запуски cron
   - `TARGET_STATES` — необязательно; статусы, при переходе в которые приходит полная карточка задачи
     (через запятую, `*` — любой; по умолчанию `In Progress,В работе`). Остальные изменения приходят коротким сообщением.
   - `DIGEST` — необязательно; `true` — каждые 30 минут присылать сводку «Мои задачи», даже без изменений
4. `vercel --prod`. Первый запуск cron пришлёт «подключён» и сам включит вебхук Telegram;
   чтобы не ждать — Vercel → проект → Settings → Cron Jobs → Run.

## Команды бота

- `/mytask` — ваши активные задачи, сгруппированные по статусам (приоритет, срок, просрочка ⚠️)
- `/mytask all` — вместе с Done и Cancelled

Бот отвечает только на сообщения из `TG_CHAT_ID`.

## Локальный запуск

```bash
PLANE_URL=https://plane.example.com PLANE_WORKSPACE=my-workspace \
PLANE_API_KEY=... TG_BOT_TOKEN=... TG_CHAT_ID=... python3 notify.py
```
