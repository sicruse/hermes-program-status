"""hermes-program-status: report a Hermes CLI session's live status with OSC 7501.

OSC 7501 is the Program Status Protocol: a program writes
``ESC ] 7501 ; state=<state> BEL`` to its terminal, and a terminal or
multiplexer that understands it (herdr-core, the engine under the Herdr
desktop and phone apps) shows the program as an agent with that status.

Why this plugin has two halves
------------------------------
In a classic ``hermes`` CLI session the process that owns the terminal is
often not the process that runs the turn. When Hermes runs its gateway, the
CLI is a viewer and every turn, tool call, approval and clarify question runs
in the gateway, which has no controlling terminal. Hook callbacks fire there,
so they cannot simply write to ``/dev/tty``.

* Front-end half (runs at plugin load in the CLI process, which owns the
  tty): if the terminal is one that consumes OSC 7501, it records
  "this tty belongs to a live Hermes CLI" in a small registry file and
  reports ``idle``.
* Agent half (hook callbacks, usually in the gateway): for each event it
  finds the tty of the event's session through Hermes's own per-terminal
  session file (``<HERMES_HOME>/terminal-sessions/tty-*``) and the registry,
  then writes one OSC 7501 sequence to that tty device.

Sessions with no registered terminal (messaging platforms, cron, other
terminals) find nothing and the plugin does nothing.

The plugin only uses documented plugin hooks and the public
``hermes_constants`` module. It patches nothing in Hermes.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import threading
import time
from pathlib import Path

PLUGIN_NAME = "hermes-program-status"

# Optional settings, read from the environment.
#   HERMES_PROGRAM_STATUS_APP       app name sent with each report ("hermes"; "" sends none)
#   HERMES_PROGRAM_STATUS_TERMINALS "herdr" (default): only herdr-core terminals
#                                   "any": every terminal (only for terminals that ignore
#                                   unknown OSC sequences)
#   HERMES_PROGRAM_STATUS_REGISTRY  registry directory (default <hermes root>/plugin-data/<plugin>)
#   HERMES_PROGRAM_STATUS_HOMES     os.pathsep-separated Hermes homes to search for session files
#   HERMES_PROGRAM_STATUS_LOG       path of an optional JSON-lines trace, for debugging
APP = os.environ.get("HERMES_PROGRAM_STATUS_APP", "hermes")
TERMINALS = os.environ.get("HERMES_PROGRAM_STATUS_TERMINALS", "herdr").strip().lower()
LOG = os.environ.get("HERMES_PROGRAM_STATUS_LOG", "")

STATES = ("idle", "working", "done", "blocked", "error")

_HIT_TTL = 30.0  # seconds to remember a session's tty
_MISS_TTL = 3.0  # seconds to remember that a session has none
_cache: dict[str, tuple[str | None, float]] = {}


def _root() -> Path:
    try:
        from hermes_constants import get_default_hermes_root

        return Path(get_default_hermes_root())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _registry() -> Path:
    override = os.environ.get("HERMES_PROGRAM_STATUS_REGISTRY")
    return Path(override) if override else _root() / "plugin-data" / PLUGIN_NAME


def _homes() -> list[Path]:
    """Hermes homes whose terminal-session files may name the session."""
    override = os.environ.get("HERMES_PROGRAM_STATUS_HOMES")
    if override:
        return [Path(p) for p in override.split(os.pathsep) if p]
    root = _root()
    homes = [root, *sorted((root / "profiles").glob("*"))]
    try:
        from hermes_constants import get_hermes_home

        homes.insert(0, Path(get_hermes_home()))
    except Exception:
        pass
    return homes


def _sanitize(tty: str) -> str:
    """Same rule Hermes uses for terminal-session file names (/dev/pts/3 -> dev-pts-3)."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", tty.strip().strip("/"))[:120]


def _trace(**record) -> None:
    if not LOG:
        return
    try:
        record.update(t=round(time.time(), 2), pid=os.getpid())
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except Exception:
        pass


def sequence(state: str, app: str = APP) -> bytes:
    """One OSC 7501 report, BEL-terminated."""
    if state not in STATES:
        raise ValueError(f"unknown state: {state}")
    suffix = f":app={app}" if app else ""
    return f"\x1b]7501;state={state}{suffix}\x07".encode("ascii")


def _write_tty(tty: str, state: str) -> bool:
    """One small non-blocking write. Never raises and never blocks the agent."""
    try:
        fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        os.write(fd, sequence(state))
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _session_in_crumb(home: Path, stem: str, session_id: str) -> bool:
    try:
        crumb = json.loads((home / "terminal-sessions" / f"tty-{stem}").read_text())
    except (OSError, ValueError):
        return False
    return isinstance(crumb, dict) and crumb.get("session_id") == session_id


def _tty_for(session_id: str) -> str | None:
    """The registered tty showing ``session_id``, or None."""
    hit = _cache.get(session_id)
    if hit and hit[1] > time.monotonic():
        return hit[0]
    tty = None
    try:
        for reg in sorted(_registry().glob("*.json")):
            try:
                entry = json.loads(reg.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(entry, dict) or not _alive(entry.get("pid")):
                continue
            if entry.get("pid") == os.getpid():
                # The agent runs in the same process as the terminal (no gateway).
                tty = entry.get("tty")
                break
            if any(_session_in_crumb(home, reg.stem, session_id) for home in _homes()):
                tty = entry.get("tty")
                break
    except Exception:
        tty = None
    _cache[session_id] = (tty, time.monotonic() + (_HIT_TTL if tty else _MISS_TTL))
    return tty


def _emit(hook: str, state: str, kwargs: dict) -> None:
    session_id = kwargs.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    tty = _tty_for(session_id)
    if not tty:
        return
    ok = _write_tty(tty, state)
    _trace(role="agent", hook=hook, state=state, tty=tty, ok=ok)


def _on_approval_request(**kwargs) -> None:
    # "smart" approvals are decided by an auxiliary model, so nobody is waiting on a person.
    if kwargs.get("surface") != "smart":
        _emit("pre_approval_request", "blocked", kwargs)


def _on_session_end(**kwargs) -> None:
    _turn_ended(kwargs.get("session_id"))
    if kwargs.get("failed"):
        _emit("on_session_end", "error", kwargs)
    elif kwargs.get("interrupted"):
        _emit("on_session_end", "idle", kwargs)


# Background subagents. Top-level delegate_task runs children in the background: the parent's
# turn ends while they work, and their results re-enter later as a new parent turn. The session
# stays "working" until then, as Claude Code does with background agents. All of these hooks
# fire in the process that runs the parent agent; subagent_stop fires on a worker thread.
_SETTLE = 5.0  # seconds to wait for the results turn after the last child stops
_lock = threading.Lock()
_children: dict[str, set] = {}  # parent session id -> running child session ids
_turns: set = set()  # session ids with a turn in flight
_timers: dict[str, threading.Timer] = {}  # parent session id -> pending "done" fallback


def _cancel_settle(session_id) -> None:
    timer = _timers.pop(session_id, None)
    if timer:
        timer.cancel()


def _turn_started(session_id) -> None:
    with _lock:
        _turns.add(session_id)
        _cancel_settle(session_id)


def _turn_ended(session_id) -> None:
    with _lock:
        _turns.discard(session_id)


def _on_pre_llm_call(**kwargs) -> None:
    _turn_started(kwargs.get("session_id"))
    _emit("pre_llm_call", "working", kwargs)


def _on_post_llm_call(**kwargs) -> None:
    session_id = kwargs.get("session_id")
    _turn_ended(session_id)
    with _lock:
        waiting = bool(_children.get(session_id))
    _emit("post_llm_call", "working" if waiting else "done", kwargs)


def _on_subagent_start(**kwargs) -> None:
    parent, child = kwargs.get("parent_session_id"), kwargs.get("child_session_id")
    if not parent or not child:
        return
    with _lock:
        _children.setdefault(parent, set()).add(child)
        _cancel_settle(parent)
    _emit("subagent_start", "working", {"session_id": parent})


def _settled(parent: str) -> None:
    """No results turn followed the last child (e.g. /stop): stop showing the session as working."""
    with _lock:
        _timers.pop(parent, None)
        if _children.get(parent) or parent in _turns:
            return
    _emit("subagent_settle", "done", {"session_id": parent})


def _on_subagent_stop(**kwargs) -> None:
    parent = kwargs.get("parent_session_id")
    with _lock:
        running = _children.get(parent)
        if running is None:
            return
        running.discard(kwargs.get("child_session_id"))
        if running:
            return
        del _children[parent]
        if parent in _turns:
            return  # the parent's own turn reports its end
        # The results turn normally starts right away and reports "working" itself; reporting
        # "done" first would flash a finished notification in Herdr.
        _cancel_settle(parent)
        timer = threading.Timer(_SETTLE, _settled, args=(parent,))
        timer.daemon = True
        _timers[parent] = timer
        timer.start()


HOOKS = {
    "pre_llm_call": _on_pre_llm_call,
    "post_llm_call": _on_post_llm_call,
    "subagent_start": _on_subagent_start,
    "subagent_stop": _on_subagent_stop,
    "on_session_end": _on_session_end,
    "pre_approval_request": _on_approval_request,
    "post_approval_response": lambda **kw: _emit("post_approval_response", "working", kw),
    "on_human_input_request": lambda **kw: _emit("on_human_input_request", "blocked", kw),
    "on_human_input_resolved": lambda **kw: _emit("on_human_input_resolved", "working", kw),
}


def _consumes_osc7501() -> bool:
    """True when this process's terminal is one we should report to."""
    if os.environ.get("HERDR_PANE_ID"):
        # A Herdr TUI pane. Herdr TUI detects Hermes on its own; stay out of its way,
        # even if the TUI server itself was started inside a herdr-core terminal.
        return False
    if TERMINALS == "any":
        return True
    return bool(os.environ.get("HC_TERMINAL_ID"))


def _attach_frontend() -> None:
    """Front-end half: register this process's tty and report idle."""
    if not _consumes_osc7501():
        return
    try:
        tty = os.ttyname(1)
    except OSError:
        return
    registry = _registry()
    registry.mkdir(parents=True, exist_ok=True)
    path = registry / f"{_sanitize(tty)}.json"
    path.write_text(json.dumps({"tty": tty, "pid": os.getpid(), "ts": time.time()}))
    atexit.register(lambda: path.unlink(missing_ok=True))
    _write_tty(tty, "idle")
    _trace(role="frontend", state="idle", tty=tty)


def register(ctx) -> None:
    try:
        _attach_frontend()
    except Exception:
        pass
    for name, callback in HOOKS.items():
        ctx.register_hook(name, callback)
