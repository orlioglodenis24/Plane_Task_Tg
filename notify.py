#!/usr/bin/env python3
"""Plane → Telegram: сообщает об изменениях в задачах, назначенных на меня.

Запускается по расписанию (Vercel Cron или GitHub Actions). Между запусками помнит снимок
каждой задачи (в Redis или STATE_FILE) и шлёт сообщение, если изменились статус, приоритет, сроки, исполнители,
метки, название или описание, появились чужие комментарии или на меня назначили задачу.
На Vercel команды боту (/mytask) приходят вебхуком в api/telegram.py и отвечаются сразу,
при запуске без Vercel — читаются через getUpdates при очередном запуске.

Переменные окружения:
  PLANE_URL        адрес Plane, например https://plane.example.com
  PLANE_WORKSPACE  slug воркспейса (часть адреса после домена)
  PLANE_API_KEY    личный API-ключ Plane (Profile → Settings → API tokens)
  TG_BOT_TOKEN     токен бота от @BotFather
  TG_CHAT_ID       куда слать (ваш Telegram id)
  TARGET_STATES    статусы, при переходе в которые присылать полную карточку задачи
                   (через запятую, * — любой; по умолчанию «In Progress,В работе»).
                   Остальные изменения приходят коротким сообщением.
  STATE_FILE       файл со снимками задач (по умолчанию state/state.json)
  KV_REST_API_URL, KV_REST_API_TOKEN
                   Upstash Redis (Vercel → Storage); если заданы, снимки хранятся там, а не в файле
  DIGEST           true — каждый запуск присылать сводку «Мои задачи», даже без изменений
  PREVIEW          true — дополнительно прислать пример уведомления по последней обновлённой задаче
"""
import datetime
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PLANE_URL = os.environ["PLANE_URL"].rstrip("/")
WORKSPACE = os.environ["PLANE_WORKSPACE"]
API_KEY = os.environ["PLANE_API_KEY"]
TG_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]
TG_API = os.environ.get("TELEGRAM_API", "https://api.telegram.org").rstrip("/")
TARGET_STATES = {
    s.strip().lower()
    for s in (os.environ.get("TARGET_STATES") or "In Progress,В работе").split(",")
    if s.strip()
}
STATE_FILE = os.environ.get("STATE_FILE", "state/state.json")
PREVIEW = os.environ.get("PREVIEW", "").lower() == "true"
DIGEST = os.environ.get("DIGEST", "").lower() == "true"
REDIS_URL = (os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL") or "").rstrip("/")
REDIS_TOKEN = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN") or ""
REDIS_KEY = "plane-tg:state"
# На Vercel команды приходят вебхуком: адрес и секрет вебхука выводим из окружения и токена бота.
VERCEL_HOST = os.environ.get("VERCEL_PROJECT_PRODUCTION_URL", "")
WEBHOOK_URL = f"https://{VERCEL_HOST}/api/telegram" if VERCEL_HOST else ""
WEBHOOK_SECRET = hashlib.sha256(f"webhook:{TG_TOKEN}".encode()).hexdigest()
DESCRIPTION_LIMIT = 700
COMMENT_LIMIT = 300
COMMENTS_SHOWN = 3
PRIORITIES = {"urgent": "🔴 Срочный", "high": "🟠 Высокий", "medium": "🟡 Средний", "low": "🔵 Низкий"}
PRIORITY_ORDER = ["urgent", "high", "medium", "low"]
STATE_GROUP_ORDER = ["started", "unstarted", "backlog", "completed", "cancelled"]
CLOSED_GROUPS = {"completed", "cancelled"}
BOT_COMMANDS = [{"command": "mytask", "description": "Мои задачи по статусам"}]
HELP_TEXT = (
    "/mytask — мои активные задачи по статусам\n"
    "/mytask all — вместе с Done и Cancelled\n\n"
    "Уведомления об изменениях в ваших задачах приходят сами. "
    "Бот проверяет Plane раз в 30 минут."
)
TG_LIMIT = 4000


def http_json(method, url, headers=None, body=None):
    """Запрос с повтором при 429/5xx. Возвращает разобранный JSON."""
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    for attempt in range(5):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = int(e.headers.get("Retry-After") or 0) or 2 ** attempt * 5
                time.sleep(min(wait, 60))
                continue
            raise
        except urllib.error.URLError:
            if attempt == 4:
                raise
            time.sleep(2 ** attempt * 5)
    raise RuntimeError(f"{method} {url}: не удалось после повторов")


def plane_get(path, **params):
    url = f"{PLANE_URL}/api/v1/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return http_json("GET", url, {"X-API-Key": API_KEY})


def plane_list(path, **extra):
    """Все элементы списка с учётом курсорной пагинации Plane."""
    items, cursor = [], None
    while True:
        params = {"per_page": 100, **extra}
        if cursor:
            params["cursor"] = cursor
        page = plane_get(path, **params)
        if isinstance(page, list):  # старые версии отдают список без пагинации
            return page
        items.extend(page.get("results", []))
        if not page.get("next_page_results"):
            return items
        cursor = page["next_cursor"]


def my_user_id():
    if os.environ.get("PLANE_USER_ID"):
        return os.environ["PLANE_USER_ID"]
    return plane_get("users/me/")["id"]


def tg_call(method, **body):
    return http_json("POST", f"{TG_API}/bot{TG_TOKEN}/{method}", body=body)


def send_telegram(text):
    tg_call("sendMessage", chat_id=TG_CHAT_ID, text=text, parse_mode="HTML",
            disable_web_page_preview=True)


def send_long(text):
    """Отправить текст несколькими сообщениями, если он не влезает в лимит Telegram."""
    chunk = ""
    for line in text.split("\n"):
        if chunk and len(chunk) + len(line) + 1 > TG_LIMIT:
            send_telegram(chunk)
            chunk = ""
        chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        send_telegram(chunk)


def pending_commands(offset):
    """Команды, которые я написал боту с прошлого запуска: ([(команда, аргументы)], новый offset)."""
    params = {"timeout": 0, "allowed_updates": ["message"]}
    if offset:
        params["offset"] = offset
    commands = []
    for update in tg_call("getUpdates", **params).get("result", []):
        offset = max(offset or 0, update["update_id"] + 1)
        command = parse_command(update)
        if command:
            commands.append(command)
    return commands, offset


def redis(*command):
    return http_json("POST", REDIS_URL, {"Authorization": f"Bearer {REDIS_TOKEN}"},
                     list(command))["result"]


def load_state():
    if REDIS_URL:
        raw = redis("GET", REDIS_KEY)
        return json.loads(raw) if raw else None
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_state(state):
    if REDIS_URL:
        redis("SET", REDIS_KEY, json.dumps(state, ensure_ascii=False))
        return
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def assignee_ids(issue):
    return {a["id"] if isinstance(a, dict) else a for a in issue.get("assignees") or []}


# Справочники для текста уведомления. Без них уведомление всё равно уйдёт, только короче.
_people = None
_labels = {}


def optional_list(path):
    try:
        return plane_list(path)
    except urllib.error.HTTPError:
        return []


def person_name(user_id):
    global _people
    if _people is None:
        _people = {}
        for m in optional_list(f"workspaces/{WORKSPACE}/members/"):
            user = m["member"] if isinstance(m.get("member"), dict) else m
            full = f"{user.get('first_name') or ''} {user.get('last_name') or ''}".strip()
            _people[user.get("id")] = full or user.get("display_name") or user.get("email") or ""
    return _people.get(user_id, "")


def label_ids(issue):
    return {l["id"] if isinstance(l, dict) else l for l in issue.get("labels") or []}


def label_name(project_id, label_id):
    if project_id not in _labels:
        _labels[project_id] = {
            l["id"]: l.get("name", "")
            for l in optional_list(f"workspaces/{WORKSPACE}/projects/{project_id}/labels/")
        }
    return _labels[project_id].get(label_id, "")


def label_names(issue, project_id):
    names = [l.get("name", "") if isinstance(l, dict) else label_name(project_id, l)
             for l in issue.get("labels") or []]
    return [n for n in names if n]


def user_id(value):
    return value["id"] if isinstance(value, dict) else value


def last_actor(issue, project_id, fields):
    """Кто последним менял эти поля задачи (по истории задачи)."""
    try:
        activities = plane_list(
            f"workspaces/{WORKSPACE}/projects/{project_id}/issues/{issue['id']}/activities/")
    except Exception:
        return ""
    changes = [a for a in activities if a.get("field") in fields]
    if not changes:
        return ""
    last = max(changes, key=lambda a: a.get("created_at") or "")
    actor = last.get("actor_detail") or {}
    return person_name(user_id(last.get("actor")) or actor.get("id")) or actor.get("display_name", "")


def fmt_date(value):
    try:
        return datetime.date.fromisoformat(str(value)[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return ""


def clean_text(stripped, raw_html, limit):
    text = stripped or ""
    if not text:
        raw = re.sub(r"(?i)<br\s*/?>|</(p|li|h\d|div)>", "\n", raw_html or "")
        text = html.unescape(re.sub(r"<[^>]+>", "", raw))
    text = re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", text)).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def description_text(issue, limit=DESCRIPTION_LIMIT):
    return clean_text(issue.get("description_stripped"), issue.get("description_html"), limit)


def issue_link(issue, project):
    key = f"{project.get('identifier', '')}-{issue.get('sequence_id', '')}"
    return key, f"{PLANE_URL}/{WORKSPACE}/projects/{project['id']}/issues/{issue['id']}"


def issue_message(issue, project, states, was, title="", extra=()):
    e = html.escape
    pid = project["id"]
    key, link = issue_link(issue, project)
    state_name = (states.get(issue.get("state")) or {}).get("name", "")
    old_name = (states.get(was) or {}).get("name", "") if was else ""

    lines = [title] if title else []
    lines += [f"🛠 <b>{e(state_name)}</b> · <a href=\"{e(link)}\">{e(key)}</a>",
              f"<b>{e(issue.get('name', ''))}</b>", ""]
    lines.append(f"📁 Проект: {e(project.get('name', ''))}")
    if old_name and old_name != state_name:
        lines.append(f"🔄 Статус: {e(old_name)} → {e(state_name)}")
        who = last_actor(issue, pid, {"state"})
        if who:
            lines.append(f"👤 Перевёл: {e(who)}")
    if PRIORITIES.get(issue.get("priority")):
        lines.append(f"⚡️ Приоритет: {PRIORITIES[issue['priority']]}")
    start, due = fmt_date(issue.get("start_date")), fmt_date(issue.get("target_date"))
    if due:
        lines.append(f"📅 Срок: {due}" + (f" (начало {start})" if start else ""))
    elif start:
        lines.append(f"📅 Начало: {start}")
    labels = label_names(issue, pid)
    if labels:
        lines.append(f"🏷 Метки: {e(', '.join(labels))}")
    assignees = [n for n in (person_name(user_id(a)) for a in issue.get("assignees") or []) if n]
    if assignees:
        lines.append(f"👥 Исполнители: {e(', '.join(assignees))}")
    author = person_name(issue.get("created_by"))
    if author:
        lines.append(f"✍️ Автор: {e(author)}")
    if extra:
        lines += ["", *extra]
    description = description_text(issue)
    if description:
        lines += ["", f"📝 {e(description)}"]
    lines += ["", f"🔗 <a href=\"{e(link)}\">Открыть в Plane</a>"]
    return "\n".join(lines)


def snapshot(issue, project_id, comment_at):
    """То, что сравниваем между запусками. description — хэш, чтобы не хранить текст."""
    return {
        "project": project_id,
        "state": issue.get("state"),
        "priority": issue.get("priority") or "none",
        "start_date": str(issue.get("start_date") or "")[:10],
        "target_date": str(issue.get("target_date") or "")[:10],
        "assignees": sorted(assignee_ids(issue)),
        "labels": sorted(label_ids(issue)),
        "name": issue.get("name", ""),
        "description": hashlib.md5(description_text(issue, None).encode()).hexdigest(),
        "comment_at": comment_at,
    }


def new_comments(issue, project_id, since, me):
    """Чужие комментарии новее since и время последнего комментария. None — API недоступен."""
    try:
        comments = plane_list(
            f"workspaces/{WORKSPACE}/projects/{project_id}/issues/{issue['id']}/comments/")
    except urllib.error.HTTPError:
        return None
    latest = max((c.get("created_at") or "" for c in comments), default="")
    if since is None:  # первый раз видим задачу — запоминаем, но не шлём старые комментарии
        return [], latest
    fresh = [c for c in comments
             if (c.get("created_at") or "") > since and user_id(c.get("actor")) != me]
    return sorted(fresh, key=lambda c: c.get("created_at") or ""), latest


def issue_changes(old, new, states, project_id):
    """Что изменилось: [(поле, строка для сообщения)]. Поля, которых нет в старом снимке, не сравниваем."""
    e = html.escape

    def changed(field):
        return field in old and old[field] != new[field]

    def state_name(state_id):
        return (states.get(state_id) or {}).get("name", "—")

    def diff_names(field, name_of):
        added = [name_of(x) or "?" for x in new[field] if x not in old[field]]
        removed = [name_of(x) or "?" for x in old[field] if x not in new[field]]
        return ", ".join([f"+{n}" for n in added] + [f"−{n}" for n in removed])

    out = []
    if changed("state"):
        out.append(("state", f"🔄 Статус: {e(state_name(old['state']))} → {e(state_name(new['state']))}"))
    if changed("priority"):
        names = {**PRIORITIES, "none": "⚪️ Без приоритета"}
        out.append(("priority", f"⚡️ Приоритет: {names.get(old['priority'], old['priority'])} → "
                                f"{names.get(new['priority'], new['priority'])}"))
    if changed("target_date"):
        out.append(("target_date", f"📅 Срок: {fmt_date(old['target_date']) or '—'} → "
                                   f"{fmt_date(new['target_date']) or '—'}"))
    if changed("start_date"):
        out.append(("start_date", f"📅 Начало: {fmt_date(old['start_date']) or '—'} → "
                                  f"{fmt_date(new['start_date']) or '—'}"))
    if changed("assignees"):
        out.append(("assignees", f"👥 Исполнители: {e(diff_names('assignees', person_name))}"))
    if changed("labels"):
        out.append(("labels", f"🏷 Метки: {e(diff_names('labels', lambda l: label_name(project_id, l)))}"))
    if changed("name"):
        out.append(("name", f"✏️ Название: «{e(old['name'])}» → «{e(new['name'])}»"))
    if changed("description"):
        out.append(("description", "📝 Описание изменено"))
    return out


def comment_lines(comments):
    e = html.escape
    lines = []
    for c in comments[:COMMENTS_SHOWN]:
        name = person_name(user_id(c.get("actor"))) or (c.get("actor_detail") or {}).get("display_name", "")
        text = clean_text(c.get("comment_stripped"), c.get("comment_html"), COMMENT_LIMIT)
        lines.append(f"💬 <b>{e(name or 'Комментарий')}</b>: {e(text)}")
    if len(comments) > COMMENTS_SHOWN:
        lines.append(f"💬 …и ещё {len(comments) - COMMENTS_SHOWN}")
    return lines


def update_message(issue, project, states, old, changes, comments):
    if old is None:
        return issue_message(issue, project, states, None, "📌 <b>На вас назначена задача</b>")
    fields = {f for f, _ in changes}
    state_name = (states.get(issue.get("state")) or {}).get("name", "")
    if "state" in fields and ("*" in TARGET_STATES or state_name.strip().lower() in TARGET_STATES):
        extra = [line for f, line in changes if f != "state"] + comment_lines(comments)
        return issue_message(issue, project, states, old["state"], extra=extra)

    e = html.escape
    key, link = issue_link(issue, project)
    lines = [f"✏️ <b>Обновление</b> · <a href=\"{e(link)}\">{e(key)}</a>",
             f"<b>{e(issue.get('name', ''))}</b>", "",
             f"📁 Проект: {e(project.get('name', ''))}",
             f"🛠 Статус: {e(state_name)}"]
    if fields:
        who = last_actor(issue, project["id"], fields)
        if who:
            lines.append(f"👤 Изменил: {e(who)}")
        lines += ["", "<b>Что изменилось:</b>", *(line for _, line in changes)]
        if "description" in fields:
            description = description_text(issue, COMMENT_LIMIT)
            if description:
                lines.append(f"<i>{e(description)}</i>")
    if comments:
        lines += ["", *comment_lines(comments)]
    lines += ["", f"🔗 <a href=\"{e(link)}\">Открыть в Plane</a>"]
    return "\n".join(lines)


def tasks_message(mine, show_all):
    e = html.escape
    today = datetime.date.today().isoformat()
    groups = {}
    for issue, project, states in mine:
        state = states.get(issue.get("state")) or {}
        groups.setdefault((state.get("group", ""), state.get("name", "Без статуса")), []).append(
            (issue, project))

    def group_rank(item):
        (group, name), _ = item
        rank = STATE_GROUP_ORDER.index(group) if group in STATE_GROUP_ORDER else 1
        return rank, name.lower()

    def issue_rank(item):
        issue = item[0]
        p = issue.get("priority")
        return (PRIORITY_ORDER.index(p) if p in PRIORITY_ORDER else len(PRIORITY_ORDER),
                issue.get("target_date") or "9999")

    active = sum(len(v) for (g, _), v in groups.items() if g not in CLOSED_GROUPS)
    lines = [f"📋 <b>Мои задачи</b> — активных: {active}"]
    hidden = []
    for (group, name), items in sorted(groups.items(), key=group_rank):
        if group in CLOSED_GROUPS and not show_all:
            hidden.append(f"{name}: {len(items)}")
            continue
        lines += ["", f"<b>{e(name)}</b> ({len(items)})"]
        for issue, project in sorted(items, key=issue_rank):
            key = f"{project.get('identifier', '')}-{issue.get('sequence_id', '')}"
            link = f"{PLANE_URL}/{WORKSPACE}/projects/{project['id']}/issues/{issue['id']}"
            title = issue.get("name", "")
            if len(title) > 80:
                title = title[:80].rsplit(" ", 1)[0] + "…"
            icon = PRIORITIES.get(issue.get("priority"), "⚪️").split()[0]
            line = f"{icon} <a href=\"{e(link)}\">{e(key)}</a> {e(title)}"
            due = fmt_date(issue.get("target_date"))
            if due:
                overdue = group not in CLOSED_GROUPS and str(issue["target_date"])[:10] < today
                line += f" · до {due}" + (" ⚠️" if overdue else "")
            lines.append(line)
    if not mine:
        lines.append("\nНа вас нет задач.")
    if hidden:
        lines += ["", f"Скрыто: {e(', '.join(hidden))}. Показать все: /mytask all"]
    return "\n".join(lines)


def answer_command(command, args, mine):
    if command == "/mytask":
        send_long(tasks_message(mine, "all" in args))
    else:
        send_telegram(HELP_TEXT)


def parse_command(update):
    """(команда, аргументы) из сообщения владельца, иначе None."""
    msg = update.get("message") or {}
    if str((msg.get("chat") or {}).get("id")) != str(TG_CHAT_ID):
        return None  # бота может найти кто угодно — отвечаем только владельцу
    words = (msg.get("text") or "").split()
    if words and words[0].startswith("/"):
        return words[0].split("@")[0].lower(), [w.lower() for w in words[1:]]
    return None


def setup_bot(state):
    """Меню команд и вебхук — только когда они поменялись."""
    if state.get("bot_commands") != BOT_COMMANDS:
        try:
            tg_call("setMyCommands", commands=BOT_COMMANDS)
            state["bot_commands"] = BOT_COMMANDS
        except Exception as e:
            print(f"не удалось задать меню команд: {type(e).__name__}")
    if WEBHOOK_URL and state.get("webhook") != WEBHOOK_URL:
        try:
            tg_call("setWebhook", url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET,
                    allowed_updates=["message"])
            state["webhook"] = WEBHOOK_URL
        except Exception as e:
            print(f"не удалось включить вебхук: {type(e).__name__}")


def answer_commands(state, mine):
    """Ответить на команды, накопившиеся с прошлого запуска. Возвращает число отвеченных команд."""
    setup_bot(state)
    if state.get("webhook"):
        return 0  # команды приходят вебхуком, getUpdates при включённом вебхуке не работает
    try:
        commands, offset = pending_commands(state.get("tg_offset"))
        for command, args in commands:
            answer_command(command, args, mine)
    except Exception as e:  # offset не сдвигаем — ответим в следующий раз
        print(f"не удалось ответить на команды: {type(e).__name__}")
        return 0
    state["tg_offset"] = offset
    return len(commands)


def my_tasks():
    """Задачи на мне: [(задача, проект, статусы проекта)] — для /mytask из вебхука."""
    me = my_user_id()
    mine = []
    for project in plane_list(f"workspaces/{WORKSPACE}/projects/"):
        if project.get("archived_at"):
            continue
        base = f"workspaces/{WORKSPACE}/projects/{project['id']}"
        try:
            states = {s["id"]: s for s in plane_list(f"{base}/states/")}
            issues = plane_list(f"{base}/issues/")
        except urllib.error.HTTPError:
            continue
        mine += [(i, project, states) for i in issues if me in assignee_ids(i)]
    return mine


def main():
    """Один проход. Возвращает число неотправленных уведомлений."""
    me = my_user_id()
    old_state = load_state()
    first_run = old_state is None
    prev = (old_state or {}).get("issues", {})
    current = {}
    found_state_names = set()
    events = []
    mine = []
    failed_projects = 0

    for project in plane_list(f"workspaces/{WORKSPACE}/projects/"):
        if project.get("archived_at"):
            continue
        pid = project["id"]
        base = f"workspaces/{WORKSPACE}/projects/{pid}"
        try:
            states = {s["id"]: s for s in plane_list(f"{base}/states/")}
            issues = plane_list(f"{base}/issues/")
        except urllib.error.HTTPError as e:
            # Нет доступа к проекту — оставляем его задачи как были, чтобы не было ложных уведомлений.
            print(f"проект пропущен: HTTP {e.code}")
            failed_projects += 1
            current.update({k: v for k, v in prev.items() if v.get("project") == pid})
            continue
        found_state_names.update(s["name"] for s in states.values())

        for issue in issues:
            if me not in assignee_ids(issue):
                continue
            old = prev.get(issue["id"])
            since = (old or {}).get("comment_at")
            fresh = []
            # Комментарии закрытых задач не запрашиваем — экономим лимит API Plane.
            if (states.get(issue.get("state")) or {}).get("group") not in CLOSED_GROUPS:
                found = new_comments(issue, pid, since, me)
                if found is not None:
                    fresh, since = found
            current[issue["id"]] = snapshot(issue, pid, since)
            mine.append((issue, project, states))
            if first_run:
                continue
            changes = issue_changes(old, current[issue["id"]], states, pid) if old else []
            if old is None or changes or fresh:
                events.append((issue, project, states, old, changes, fresh))

    failed_sends = 0
    for issue, project, states, old, changes, fresh in events:
        try:
            send_long(update_message(issue, project, states, old, changes, fresh))
        except Exception as e:  # не запоминаем изменения — попробуем в следующий раз
            print(f"не удалось отправить в Telegram: {type(e).__name__}")
            failed_sends += 1
            if old is None:
                current.pop(issue["id"], None)
            else:
                current[issue["id"]] = old

    if first_run:
        send_telegram(
            f"✅ Plane → Telegram подключён. Слежу за задачами на вас: {len(current)}.\n"
            "Пришлю сообщение, если изменится статус, приоритет, сроки, исполнители, метки, "
            "название или описание, появится комментарий или на вас назначат задачу."
        )
        print("статусы в проектах:", ", ".join(sorted(found_state_names)))

    if DIGEST and not first_run:
        try:
            send_long(tasks_message(mine, False))
        except Exception as e:
            print(f"не удалось отправить сводку: {type(e).__name__}")

    if PREVIEW and mine:
        issue, project, states = max(mine, key=lambda m: m[0].get("updated_at") or "")
        send_telegram(issue_message(issue, project, states, None, "👀 <i>Пример уведомления</i>"))

    new_state = {**(old_state or {}), "user_id": me, "issues": current}
    answered = answer_commands(new_state, mine)
    save_state(new_state)
    # В лог — только счётчики: логи Actions могут быть видны всем, у кого есть доступ к репозиторию.
    print(f"задач на мне: {len(current)}, уведомлений: {len(events) - failed_sends}, "
          f"ошибок отправки: {failed_sends}, пропущено проектов: {failed_projects}, "
          f"команд: {answered}")
    return failed_sends


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
