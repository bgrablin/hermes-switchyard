"""Show the reasoning level Switchyard sent in the Hermes TUI status bar.

The Hermes TUI status bar reads two ``session.info`` fields: ``reasoning_effort`` (the user's
``/reasoning`` level) and ``reasoning_effort_wire`` (the level the route sends). When they
differ it shows ``high→low``. Switchyard changes only the level in one request, so Hermes still
reports the user's level for both fields. This module re-emits ``session.info`` for the
foreground TUI session with ``reasoning_effort_wire`` set to the level Switchyard sent.

Rules:

- It never changes ``agent.reasoning_config``. The user's level stays the cap (#118).
- It never imports the TUI gateway. It acts only when ``tui_gateway.server`` is already
  loaded, that is, inside a Hermes TUI or Desktop gateway process. Elsewhere it does nothing.
- It emits only when the shown level changes, and never on the request thread.
- Any failure is ignored. A display update never breaks a request.

``tui_gateway.server`` is a private Hermes module. The three names used here
(``_sessions``, ``_session_info``, ``_emit``) are read with ``getattr``; if a Hermes release
renames them, this module does nothing.
"""
from __future__ import annotations

import sys
import threading
from collections import OrderedDict
from typing import Any, Callable

_SERVER_MODULE = "tui_gateway.server"
_SHOWN_LIMIT = 256


def _running_tui_server() -> Any:
    """Return the loaded TUI gateway module, or None. Never imports it."""
    server = sys.modules.get(_SERVER_MODULE)
    if server is None:
        return None
    if not all(callable(getattr(server, name, None)) for name in ("_session_info", "_emit")):
        return None
    if not isinstance(getattr(server, "_sessions", None), dict):
        return None
    return server


def _foreground_sessions(server: Any, session_id: str) -> list[tuple[str, dict, Any]]:
    """Return (TUI session id, session record, agent) for live sessions whose agent is *session_id*."""
    lock = getattr(server, "_sessions_lock", None)
    sessions = server._sessions
    if lock is not None:
        with lock:
            items = list(sessions.items())
    else:
        items = list(sessions.items())
    found = []
    for sid, session in items:
        if not isinstance(session, dict):
            continue
        agent = session.get("agent")
        if agent is None or str(getattr(agent, "session_id", "") or "") != session_id:
            continue
        found.append((str(sid), session, agent))
    return found


def publish_sent_effort(session_id: str, level: str) -> bool:
    """Re-emit ``session.info`` with the sent *level* for the TUI session running *session_id*.

    Returns True when at least one event was written. The user's own level is left in
    ``reasoning_effort``; only ``reasoning_effort_wire`` names the sent level. A session whose
    reasoning is turned off is left alone.
    """
    server = _running_tui_server()
    if server is None or not session_id or not level:
        return False
    emitted = False
    for sid, session, agent in _foreground_sessions(server, session_id):
        info = server._session_info(agent, session)
        if not isinstance(info, dict):
            continue
        user_level = str(info.get("reasoning_effort") or "")
        if not user_level or user_level == "none":
            continue  # thinking off or unset: nothing to compare against
        info = dict(info)
        info["reasoning_effort_wire"] = level
        if server._emit("session.info", sid, info) is not False:
            emitted = True
    return emitted


def _start_daemon(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="switchyard-tui-status", daemon=True).start()


class SentEffortStatus:
    """Publish the sent level when it changes, off the request thread.

    Hermes re-emits ``session.info`` with the user's level for its own reasons (a cwd or
    ``/reasoning`` change, for example). So a new turn whose level differs from the user's level
    always publishes, even when the last published level was the same.

    *publish* and *run* are seams for tests. The default *run* starts a daemon thread, so the
    request thread never waits on the TUI transport.
    """

    def __init__(
        self,
        *,
        publish: Callable[[str, str], Any] | None = None,
        run: Callable[[Callable[[], None]], Any] | None = None,
    ) -> None:
        self._publish = publish or publish_sent_effort
        self._run = run or _start_daemon
        self._lock = threading.Lock()
        # Session -> (turn, level last published).
        self._shown: OrderedDict[str, tuple[str, str]] = OrderedDict()

    def note(self, session_id: Any, requested: Any, sent: Any, turn_id: Any = None) -> None:
        """Record the level sent for one foreground request. Never raises."""
        try:
            session = str(session_id or "")
            sent_level = str(sent or "")
            user_level = str(requested or "")
            turn = str(turn_id or "")
            if not session or not sent_level or not user_level:
                return
            if _running_tui_server() is None:
                return  # not inside a TUI gateway process
            with self._lock:
                # Hermes shows the user's level until told otherwise.
                last_turn, shown = self._shown.get(session, (turn, user_level))
                new_turn = turn != last_turn
                if shown == sent_level and not (new_turn and sent_level != user_level):
                    self._shown[session] = (turn, shown)
                    return
                self._shown[session] = (turn, sent_level)
                self._shown.move_to_end(session)
                while len(self._shown) > _SHOWN_LIMIT:
                    self._shown.popitem(last=False)
            publish = self._publish

            def emit() -> None:
                try:
                    publish(session, sent_level)
                except Exception:  # noqa: BLE001 -- a display update never breaks a request
                    pass

            self._run(emit)
        except Exception:  # noqa: BLE001 -- a display update never breaks a request
            pass

    def forget(self, session_id: Any) -> None:
        """Drop the shown level for a session, for example after Hermes re-emits its own."""
        with self._lock:
            self._shown.pop(str(session_id or ""), None)
