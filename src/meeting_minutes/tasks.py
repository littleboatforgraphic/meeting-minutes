"""Task tracking: built-in tracker (default) plus pluggable sinks for external task tools / ERP modules."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from .company import CompanyConfig
from .db import Meeting, Person, Task, utcnow
from .notify import Dispatcher, Message

STATUSES = ("open", "in_progress", "done", "cancelled")


class TaskSink(Protocol):
    """Pushes tasks to an external tool. The internal tracker always keeps its own copy."""

    def push(self, task: Task, owner: Person | None) -> str | None: ...

    def update_status(self, task: Task) -> None: ...


class InternalSink:
    def push(self, task: Task, owner: Person | None) -> str | None:
        return None

    def update_status(self, task: Task) -> None:
        return None


# Future connectors (M365 Planner/To Do, Google Tasks, ClickUp, Trello, ERP modules) register here.
SINKS: dict[str, type] = {"internal": InternalSink, "none": InternalSink}


def get_sink(company: CompanyConfig) -> TaskSink:
    try:
        return SINKS[company.tasks.sink]()
    except KeyError:
        raise ValueError(f"task sink '{company.tasks.sink}' is not available; known: {sorted(SINKS)}") from None


def can_edit(task: Task, user: Person) -> bool:
    if user.is_admin or task.owner_id == user.id:
        return True
    return bool(task.meeting and task.meeting.chair_id == user.id)


def set_status(db: Session, company: CompanyConfig, task: Task, status: str) -> Task:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    task.status = status
    task.completed_at = utcnow() if status == "done" else None
    get_sink(company).update_status(task)
    db.flush()
    return task


def task_line(t: Task) -> str:
    due = f" (due {t.due_date:%d %b %Y})" if t.due_date else " (no deadline stated)"
    return f"#{t.id} {t.title}{due}"


def task_buttons(tasks: list[Task]) -> list[tuple[str, str]]:
    return [(f"Mark #{t.id} done", f"task:{t.id}:done") for t in tasks if t.status in ("open", "in_progress")]


def run_reminders(db: Session, company: CompanyConfig, dispatcher: Dispatcher, today: date, app_url: str) -> int:
    """Remind owners before deadlines and nag on overdue tasks. Returns the number of reminders sent."""
    cfg = company.tasks.reminders
    if not cfg.enabled:
        return 0
    due: dict[int, list[Task]] = {}
    rows = db.scalars(select(Task).where(Task.status.in_(("open", "in_progress")), Task.owner_id.is_not(None),
                                         Task.due_date.is_not(None))).all()
    for t in rows:
        if t.last_reminded_on == today:
            continue
        days_left = (t.due_date - today).days
        if days_left in cfg.days_before or (
                days_left < 0 and (t.last_reminded_on is None or (today - t.last_reminded_on).days >= cfg.overdue_every_days)):
            due.setdefault(t.owner_id, []).append(t)
    sent = 0
    for owner_id, tasks in due.items():
        owner = db.get(Person, owner_id)
        if not owner or not owner.active:
            continue
        lines = []
        for t in sorted(tasks, key=lambda x: x.due_date):
            days_left = (t.due_date - today).days
            state = "OVERDUE" if days_left < 0 else ("due today" if days_left == 0 else f"due in {days_left} day(s)")
            lines.append(f"- {task_line(t)}: {state}")
        msg = Message(subject=f"Task reminder ({len(tasks)})", text="\n".join(lines) +
                      "\n\nReply 'done <number>' or use the buttons when finished.",
                      link=f"{app_url}/#/tasks", link_label="My tasks", buttons=task_buttons(tasks))
        if dispatcher.send(db, owner, msg, "reminder"):
            for t in tasks:
                t.last_reminded_on = today
            sent += 1
    db.flush()
    return sent


def meeting_tasks(db: Session, meeting: Meeting) -> list[Task]:
    return list(db.scalars(select(Task).where(Task.meeting_id == meeting.id).order_by(Task.id)))
