"""Turns Toggl's project and entry lists into the protocol's rows."""
from fields import DEFAULT_COLOR, color, number
from util import short

MAX_TASKS = 20       # the last 20 distinct (description, project) pairs
ENTRIES_FETCH = 50   # newest entries asked for, in the one request
MAX_TEXT = 255       # a row stays small; the board cuts text further to fit its own buffers


def _items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]
    return []


def project_rows(data):
    """Projects in use, sorted by name. "active": false is common for projects people still log to, so it's no filter:
    only archived, deleted, template and draft ones are left out."""
    rows = []
    for p in _items(data):
        if not isinstance(p, dict):
            continue
        pid, name = number(p.get("id")), p.get("name")
        if not pid or not isinstance(name, str) or not name:
            continue
        if p.get("archived_at") or p.get("deleted_at") or p.get("is_template") is True or p.get("draft") is True:
            continue
        rows.append({"id": pid, "name": short(name, MAX_TEXT), "color": color(p.get("color"))})
    rows.sort(key=lambda r: r["name"].casefold())
    return rows


def project_object(project_id, inline, known):
    """The protocol's project object, or None for no project. Toggl's entry may carry its project, or the list may."""
    if not project_id:
        return None
    if isinstance(inline, dict) and isinstance(inline.get("name"), str):
        return {"id": project_id, "name": short(inline["name"], MAX_TEXT), "color": color(inline.get("color"))}
    for row in known:
        if row["id"] == project_id:
            return dict(row)
    return {"id": project_id, "name": "", "color": DEFAULT_COLOR}


def task_rows(data, known):
    """Recent entries, the running one too, newest first: one row per (description, project)."""
    rows, seen = [], set()
    for e in _items(data):
        if len(rows) >= MAX_TASKS:
            break
        if not isinstance(e, dict) or not e.get("start"):  # only planned
            continue
        task = e.get("task") if isinstance(e.get("task"), dict) else {}
        label = e.get("description") or task.get("name")
        if not isinstance(label, str) or not label:
            continue
        project_id = number(e.get("project_id"))
        pair = (label.casefold(), project_id)
        if pair in seen:
            continue
        seen.add(pair)
        rows.append({"desc": short(label, MAX_TEXT), "task_id": number(e.get("task_id")),
                     "project": project_object(project_id, e.get("project"), known)})
    return rows
