#!/usr/bin/env python3
"""Plane → Telegram: сообщает, когда задача, назначенная на меня, переходит в статус «В работе».

Запускается по расписанию (GitHub Actions). Между запусками помнит статусы задач
в STATE_FILE и шлёт сообщение только при смене статуса на целевой.

Переменные окружения:
  PLANE_URL        адрес Plane, например https://plane.example.com
  PLANE_WORKSPACE  slug воркспейса (часть адреса после домена)
  PLANE_API_KEY    личный API-ключ Plane (Profile → Settings → API tokens)
  TG_BOT_TOKEN     токен бота от @BotFather
  TG_CHAT_ID       куда слать (ваш Telegram id)
  TARGET_STATES    названия статусов через запятую (по умолчанию «In Progress,В работе»)
  STATE_FILE       файл с запомненными статусами (по умолчанию state/state.json)
  PREVIEW          true — дополнительно прислать пример уведомления по последней обновлённой задаче
"""
import datetime
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
DESCRIPTION_LIMIT = 700
PRIORITIES = {"urgent": "🔴 Срочный", "high": "🟠 Высокий", "medium": "🟡 Средний", "low": "🔵 Низкий"}


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


def send_telegram(text):
    http_json(
        "POST",
        f"{TG_API}/bot{TG_TOKEN}/sendMessage",
        body={
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_state(state):
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


def label_names(issue, project_id):
    if project_id not in _labels:
        _labels[project_id] = {
            l["id"]: l.get("name", "")
            for l in optional_list(f"workspaces/{WORKSPACE}/projects/{project_id}/labels/")
        }
    names = [l.get("name", "") if isinstance(l, dict) else _labels[project_id].get(l, "")
             for l in issue.get("labels") or []]
    return [n for n in names if n]


def state_changed_by(issue, project_id):
    """Кто последним менял статус задачи (по истории задачи)."""
    try:
        activities = plane_list(
            f"workspaces/{WORKSPACE}/projects/{project_id}/issues/{issue['id']}/activities/")
    except Exception:
        return ""
    changes = [a for a in activities if a.get("field") == "state"]
    if not changes:
        return ""
    last = max(changes, key=lambda a: a.get("created_at") or "")
    actor = last.get("actor_detail") or {}
    return person_name(last.get("actor") or actor.get("id")) or actor.get("display_name", "")


def fmt_date(value):
    try:
        return datetime.date.fromisoformat(str(value)[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return ""


def description_text(issue):
    text = issue.get("description_stripped") or ""
    if not text:
        raw = issue.get("description_html") or ""
        raw = re.sub(r"(?i)<br\s*/?>|</(p|li|h\d|div)>", "\n", raw)
        text = html.unescape(re.sub(r"<[^>]+>", "", raw))
    text = re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", text)).strip()
    if len(text) > DESCRIPTION_LIMIT:
        text = text[:DESCRIPTION_LIMIT].rsplit(" ", 1)[0] + "…"
    return text


def issue_message(issue, project, states, was, title=""):
    e = html.escape
    pid = project["id"]
    key = f"{project.get('identifier', '')}-{issue.get('sequence_id', '')}"
    link = f"{PLANE_URL}/{WORKSPACE}/projects/{pid}/issues/{issue['id']}"
    state_name = (states.get(issue.get("state")) or {}).get("name", "")
    old_name = (states.get(was) or {}).get("name", "") if was else ""

    lines = [title] if title else []
    lines += [f"🛠 <b>{e(state_name)}</b> · <a href=\"{e(link)}\">{e(key)}</a>",
              f"<b>{e(issue.get('name', ''))}</b>", ""]
    lines.append(f"📁 Проект: {e(project.get('name', ''))}")
    if old_name and old_name != state_name:
        lines.append(f"🔄 Статус: {e(old_name)} → {e(state_name)}")
    who = state_changed_by(issue, pid)
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
    assignees = [n for n in (person_name(a["id"] if isinstance(a, dict) else a)
                              for a in issue.get("assignees") or []) if n]
    if assignees:
        lines.append(f"👥 Исполнители: {e(', '.join(assignees))}")
    author = person_name(issue.get("created_by"))
    if author:
        lines.append(f"✍️ Автор: {e(author)}")
    description = description_text(issue)
    if description:
        lines += ["", f"📝 {e(description)}"]
    lines += ["", f"🔗 <a href=\"{e(link)}\">Открыть в Plane</a>"]
    return "\n".join(lines)


def main():
    me = my_user_id()
    old = load_state()
    first_run = old is None
    prev = (old or {}).get("issues", {})
    current = {}
    found_state_names = set()
    to_notify = []
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
            state_id = issue.get("state")
            current[issue["id"]] = {"state": state_id, "project": pid}
            mine.append((issue, project, states))
            state = states.get(state_id) or {}
            if state.get("name", "").strip().lower() not in TARGET_STATES:
                continue
            was = prev.get(issue["id"], {}).get("state")
            if was != state_id and not first_run:
                to_notify.append((issue, project, states, was))

    failed_sends = 0
    for issue, project, states, was in to_notify:
        try:
            send_telegram(issue_message(issue, project, states, was))
        except Exception as e:  # не запоминаем новый статус — попробуем в следующий раз
            print(f"не удалось отправить в Telegram: {type(e).__name__}")
            failed_sends += 1
            if was is None:
                current.pop(issue["id"], None)
            else:
                current[issue["id"]]["state"] = was

    if first_run:
        send_telegram(
            f"✅ Plane → Telegram подключён. Слежу за задачами на вас: {len(current)}.\n"
            f"Уведомляю о статусах: {html.escape(', '.join(sorted(TARGET_STATES)))}"
        )
        print("статусы в проектах:", ", ".join(sorted(found_state_names)))

    if PREVIEW and mine:
        issue, project, states = max(mine, key=lambda m: m[0].get("updated_at") or "")
        send_telegram(issue_message(issue, project, states, None, "👀 <i>Пример уведомления</i>"))

    save_state({"user_id": me, "issues": current})
    # В лог — только счётчики: логи Actions могут быть видны всем, у кого есть доступ к репозиторию.
    print(f"задач на мне: {len(current)}, уведомлений: {len(to_notify) - failed_sends}, "
          f"ошибок отправки: {failed_sends}, пропущено проектов: {failed_projects}")
    if failed_sends:
        sys.exit(1)


if __name__ == "__main__":
    main()
