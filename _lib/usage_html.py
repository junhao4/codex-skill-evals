"""Interactive HTML dashboard renderer for the project usage report.

Renders the `summary` dict produced by `build_project_usage` into a
self-contained, single-file HTML explorer modelled on the
`prototype-e-interactive-explorer` design: two top-level modes (Real Project
Work / Harness Eval Calls), each with an overview page plus click-through
drill-down detail views toggled client-side via a `.hidden` class.

The renderer is pure: it reads only the summary dict and emits a string. All
numbers come from the same summary consumed by `render_usage_markdown`, so the
HTML and Markdown reports never disagree.
"""

from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from _lib.pricing import PRICING_AS_OF, PRICING_SOURCE
from _lib.pricing import format_cost as _format_cost


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _cost(value: Any, *, zero_ok: bool = True) -> str:
    if value is None:
        return "$0.0000" if zero_ok else "n/a"
    return _format_cost(value)


def _num(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def _slug(*parts: Any) -> str:
    raw = "-".join(str(p) for p in parts if p not in (None, ""))
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in raw).strip("-")


def _ts(value: Any) -> str:
    text = str(value or "")
    return text[:19].replace("T", " ") if text else "—"


def _cost_meter(value: Any, maximum: float, *, color: str = "blue", zero_ok: bool = True) -> str:
    """A compact cost bar that makes relative spend visible in dense tables."""
    label = _cost(value, zero_ok=zero_ok)
    width = min(100.0, max(0.0, float(value or 0) / maximum * 100)) if maximum else 0.0
    return (
        f"<div class='table-cost'><strong>{_esc(label)}</strong>"
        f"<span><i class='meter-{color}' style='width:{width:.1f}%'></i></span></div>"
    )


# --------------------------------------------------------------------------- #
# Real Project Work                                                           #
# --------------------------------------------------------------------------- #

def _real_skill_rows(summary: dict[str, Any]) -> str:
    rows: list[str] = []
    max_cost = max((float(sk.get("est_cost") or 0) for sk in summary["skills"]), default=0.0)
    for sk in summary["skills"]:
        tok = sk["tokens"]
        triggered = sk["sessions_triggered"]
        cost = (
            _cost(sk["est_cost"])
            if sk["priced_count"] > 0
            else ("$0.0000" if triggered == 0 else "n/a")
        )
        status = "installed" if sk["installed"] else "external"
        unused = (
            " <span class='quiet-status'>unused</span>"
            if sk["installed"] and triggered == 0
            else ""
        )
        clickable = "clickable-row" if triggered > 0 else ""
        onclick = (
            f"onclick=\"openSkillDetail('{_slug('real-skill', sk['skill'])}')\""
            if triggered > 0
            else ""
        )
        rows.append(
            f"<tr class='{clickable}' {onclick}>"
            f"<td><div class='row-title'>{_esc(sk['skill'])}</div>"
            f"<span class='row-meta'><i class='status-dot {'green' if sk['installed'] else 'slate'}'></i>{status}</span>{unused}</td>"
            f"<td class='num'>{triggered}</td>"
            f"<td class='num'>{sk['explicit_count']} / {sk['implicit_count']}</td>"
            f"<td class='num'>{_num(tok['uncached_input_tokens'])}</td>"
            f"<td class='num'>{_num(tok['cached_input_tokens'])}</td>"
            f"<td class='num'>{_num(tok['billed_output_tokens'])}</td>"
            f"<td class='num'>{_cost_meter(sk.get('est_cost'), max_cost) if sk['priced_count'] > 0 else _esc(cost)}</td>"
            f"</tr>"
        )
    if not rows:
        rows.append(
            "<tr><td colspan='7' class='empty'>No skills discovered in "
            "<code>.agents/skills/</code> or session traces.</td></tr>"
        )
    return "".join(rows)


def _real_subagent_rows(summary: dict[str, Any]) -> str:
    rows: list[str] = []
    max_cost = max((float(ag.get("est_cost") or 0) for ag in summary["subagents"]), default=0.0)
    for ag in summary["subagents"]:
        tok = ag["tokens"]
        cost = (
            _cost(ag["est_cost"])
            if ag["priced_count"] > 0
            else ("$0.0000" if ag["spawns"] == 0 else "n/a")
        )
        src = f"<span class='row-meta'><i class='status-dot {'violet' if ag['configured'] else 'slate'}'></i>{'configured' if ag['configured'] else 'built-in / ad-hoc'}</span>"
        rows.append(
            f"<tr>"
            f"<td><div class='row-title'>{_esc(ag['role'])}</div>{src}</td>"
            f"<td class='num'>{ag['spawns']}</td>"
            f"<td class='num'>{ag['sessions_used']}</td>"
            f"<td class='num'>{ag['command_count']} ({ag['failed_command_count']})</td>"
            f"<td class='num'>{_num(tok['uncached_input_tokens'])}</td>"
            f"<td class='num'>{_num(tok['billed_output_tokens'])}</td>"
            f"<td class='num'>{_cost_meter(ag.get('est_cost'), max_cost, color='violet') if ag['priced_count'] > 0 else _esc(cost)}</td>"
            f"</tr>"
        )
    if not rows:
        rows.append(
            "<tr><td colspan='7' class='empty'>No subagent profiles or spawns "
            "recorded.</td></tr>"
        )
    return "".join(rows)


def _real_session_rows(summary: dict[str, Any]) -> str:
    rows: list[str] = []
    for idx, sess in enumerate(summary["sessions"]):
        tok = sess["token_usage"]
        model = sess["model"] or "unknown"
        model_label = (
            f"{model} ({sess['reasoning_effort']})"
            if sess.get("reasoning_effort")
            else model
        )
        skills = sess["skills_used"]
        badges = (
            " ".join(f"<span class='row-meta skill-tag'><i class='status-dot blue'></i>{_esc(s)}</span>" for s in skills)
            if skills else "<span class='muted'>—</span>"
        )
        detail_id = f"real-sess-{idx}"
        rows.append(
            f"<tr class='clickable-row' onclick=\"openSessionDetail('{detail_id}')\">"
            f"<td><code>{_esc(_ts(sess['started_at']))}</code></td>"
            f"<td><code>{_esc(str(sess['thread_id'])[:12])}</code></td>"
            f"<td><code>{_esc(model_label)}</code></td>"
            f"<td class='num'>{sess['turn_count']}</td>"
            f"<td>{badges}</td>"
            f"<td class='num'>{sess['subagent_spawn_count']}</td>"
            f"<td class='num'>{_num(tok['billed_output_tokens'])}</td>"
            f"<td class='num'><strong>{_esc(_cost(sess['est_cost']))}</strong></td>"
            f"<td class='prompt-cell' title='{_esc(sess['first_prompt'])}'>"
            f"{_esc(sess['first_prompt'] or '—')}</td>"
            f"</tr>"
        )
    if not rows:
        rows.append(
            "<tr><td colspan='9' class='empty'>No real project work sessions "
            "recorded yet.</td></tr>"
        )
    return "".join(rows)


def _command_timeline(commands: list[dict[str, Any]]) -> str:
    """Render an ordered command execution trace (command text + exit outcome)."""
    if not commands:
        return "<p class='muted'>No commands recorded in this session's trace.</p>"

    def _parse_ts(value: Any):
        if not isinstance(value, str):
            return None
        from datetime import datetime, timezone

        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            return None

    base_ts = next((_parse_ts(c.get("timestamp")) for c in commands if _parse_ts(c.get("timestamp"))), None)
    steps: list[str] = []
    for i, c in enumerate(commands):
        exit_code = c.get("exit_code")
        ok = exit_code == 0 or (exit_code is None and c.get("status") == "completed")
        badge_cls = "badge-green" if ok else "badge-amber"
        badge = c.get("status") or "done" if exit_code is None else f"exit {exit_code}"
        # Relative timing vs the first command.
        rel = ""
        ts = _parse_ts(c.get("timestamp"))
        if base_ts and ts:
            delta = (ts - base_ts).total_seconds()
            rel = "+0.0s" if i == 0 and delta == 0 else f"+{delta:.1f}s"
        time_cell = f"<span class='trace-time'>{_esc(rel)}</span>" if rel else ""
        cmd_text = _esc(c.get("command") or "—")
        steps.append(
            f"<div class='trace-step'>{time_cell}<div class='trace-dot'></div>"
            f"<div style='flex:1'><div class='trace-step-title'>"
            f"<span class='badge {badge_cls}'>{_esc(badge)}</span></div>"
            f"<div class='trace-code'>{cmd_text}</div></div></div>"
        )
    return f"<div class='trace-timeline'>{''.join(steps)}</div>"


def _parse_trace_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _pack_timeline_lanes(actors: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Reserve main's lane; reuse child lanes only for disjoint known spans."""
    columns = [[actors[0]]]
    for actor in sorted(actors[1:], key=lambda item: (item["_s"], item["_e"])):
        known = actor["_start"] is not None and actor["_end"] is not None
        lane = next((i for i in range(1, len(columns)) if known and all(
            other["_start"] is not None and other["_end"] is not None and
            (actor["_s"] >= other["_e"] or actor["_e"] <= other["_s"])
            for other in columns[i]
        )), None)
        if lane is None:
            lane = len(columns)
            columns.append([])
        columns[lane].append(actor)
        actor["_column"] = lane
    return columns


def _timeline_bar_geometry(start: float, end: float, duration: float) -> tuple[float, float]:
    """Keep even minimum-height markers inside the fixed map."""
    top = min(99.0, max(0.0, start / duration * 100))
    height = min(100.0 - top, max(1.0, (end - start) / duration * 100))
    return top, height


def _timeline_component(tree: dict[str, Any] | None, prefix: str) -> str:
    """Render prototype AA's vertical Gantt and linked, readable event stream."""
    if not isinstance(tree, dict):
        return "<p class='muted'>No retained activity tree for this session.</p>"
    actors = [dict(tree, actor_id=f"{prefix}-main", is_main=True)]
    for index, child in enumerate(tree.get("children") or []):
        actors.append(dict(child, actor_id=f"{prefix}-agent-{index}", is_main=False))
    for actor in actors:
        actor["_start"] = _parse_trace_time(actor.get("start_ts"))
        actor["_end"] = _parse_trace_time(actor.get("end_ts"))
    for actor in actors:
        if actor["_start"] and actor["_end"] and actor["_end"] < actor["_start"]:
            actor["_start"] = actor["_end"] = None
    timed = [a for a in actors if a["_start"] and a["_end"]]
    if not timed:
        return "<p class='muted'>No recorded timestamps are available for this activity.</p>"
    origin = min(a["_start"] for a in timed)
    finish = max(a["_end"] for a in timed)
    duration = max((finish - origin).total_seconds(), 1.0)
    for actor in actors:
        start = actor["_start"]
        end = actor["_end"]
        actor["_s"] = max(0.0, (start - origin).total_seconds()) if start else 0.0
        actor["_e"] = max(actor["_s"] + 0.2, (end - origin).total_seconds()) if end else actor["_s"] + 0.2
        actor["_column"] = 0
    # The map has a fixed time scale; trace content flows in normal document order.
    canvas_h = 600
    columns = _pack_timeline_lanes(actors)

    colors = ("c-main", "c-a", "c-b", "c-c3", "c-c4")
    heads: list[str] = []
    lanes: list[str] = []
    max_columns = max(1, len(columns))
    for lane_idx, members in enumerate(columns):
        members.sort(key=lambda actor: actor["_s"])
        first = members[0]
        color = colors[min(lane_idx, len(colors)-1)]
        label = "main" if lane_idx == 0 else (first.get("agent_nickname") or first.get("agent_role") or "subagent")
        more = f" <span class='lane-reuse'>+{len(members)-1}</span>" if len(members) > 1 else ""
        role = "worker" if lane_idx == 0 else f"lane {lane_idx} · {len(members)} actor(s)"
        heads.append(
            f"<div class='u-col-head {color}'><div class='u-col-icon'>{'▸' if lane_idx == 0 else '◉'}</div>"
            f"<div class='u-col-name'>{_esc(label)}{more}</div><div class='u-col-role'>{_esc(role)}</div></div>"
        )
        lane_parts = ["<div class='u-lane'>"]
        for actor in members:
            start, end = actor["_s"], actor["_e"]
            top, height = _timeline_bar_geometry(start, end, duration)
            actor_color = colors[min(actor["_column"], len(colors)-1)]
            if not actor.get("is_main") and (not actor["_start"] or not actor["_end"]):
                lane_parts.append(
                    f"<button class='u-block {actor_color} missing' data-actor='{actor['actor_id']}' aria-pressed='false' onclick=\"openTimelineActor('{actor['actor_id']}')\" style='top:0%;height:8%' title='Trace not captured'>"
                    f"<span class='u-bar-label'><i aria-hidden='true'>◉</i><span>{_esc(actor.get('agent_nickname') or actor.get('agent_role') or 'subagent')}</span></span>"
                    "<small>trace not captured</small></button>"
                )
            elif not actor.get("is_main"):
                lane_parts.append(
                    f"<div class='u-life {actor_color}' style='top:{top:.3f}%;height:{height:.3f}%'></div>"
                    f"<button class='u-block {actor_color}' data-actor='{actor['actor_id']}' aria-pressed='false' onclick=\"openTimelineActor('{actor['actor_id']}')\" "
                    f"style='top:{top:.3f}%;height:{height:.3f}%' title='{_esc(label)} · {end-start:.1f}s'>"
                    f"<span class='u-bar-label'><i aria-hidden='true'>◉</i><span>{_esc(actor.get('agent_nickname') or actor.get('agent_role') or 'subagent')}</span></span>"
                    f"<small>{end-start:.1f}s · {_num((actor.get('token_usage') or {}).get('billed_output_tokens', 0))} billed out</small></button>"
                )
            else:
                spawn_times = [
                    (ev.get("thread_ids") or [], (_parse_trace_time(ev.get("ts")) - origin).total_seconds())
                    for ev in actor.get("events") or []
                    if ev.get("kind") == "spawn" and _parse_trace_time(ev.get("ts"))
                ]
                child_ends = {
                    str(child.get("thread_id")): (child["_end"] - origin).total_seconds()
                    for child in actors[1:] if child.get("thread_id") and child.get("_end")
                }
                wait_events = [
                    ev for ev in actor.get("events") or []
                    if ev.get("kind") == "wait" and _parse_trace_time(ev.get("ts"))
                ]
                wait_intervals: list[tuple[float, float]] = []
                if spawn_times:
                    first_spawn = min(t for _ids, t in spawn_times)
                    spawned_ids = {str(tid) for ids, _t in spawn_times for tid in ids}
                    if spawned_ids and spawned_ids.issubset(child_ends):
                        wait_end = max(child_ends[tid] for tid in spawned_ids)
                    else:
                        wait_end = max((_parse_trace_time(ev["ts"]) - origin).total_seconds() for ev in wait_events) if wait_events else first_spawn
                    if wait_end > first_spawn:
                        wait_intervals.append((first_spawn, wait_end))
                spans: list[tuple[float, float, str]] = []
                cursor = start
                for block_start, block_end in wait_intervals:
                    if block_start > cursor:
                        spans.append((cursor, block_start, "main work"))
                    spans.append((block_start, block_end, "waiting / fan-out"))
                    cursor = max(cursor, block_end)
                spans.append((cursor, end, "main work"))
                for a_s, a_e, title in spans:
                    a_s, a_e = max(start, a_s), min(end, a_e)
                    if a_e <= a_s:
                        continue
                    y, h = _timeline_bar_geometry(a_s, a_e, duration)
                    wait_cls = " wait" if "waiting" in title else ""
                    lane_parts.append(
                        f"<button class='u-block {actor_color}{wait_cls}' data-actor='{actor['actor_id']}' aria-pressed='false' onclick=\"openTimelineActor('{actor['actor_id']}')\" "
                        f"style='top:{y:.3f}%;height:{h:.3f}%' title='{_esc(title)} · {a_e-a_s:.1f}s'>"
                        f"<span class='u-bar-label'><i aria-hidden='true'>▸</i><span>main · {_esc('waiting / fan-out' if wait_cls else 'work')}</span></span><small>{a_e-a_s:.1f}s</small></button>"
                    )
        lane_parts.append("</div>")
        lanes.append("".join(lane_parts))

    tick_step = max(5, int(duration / 8 / 5) * 5)
    ticks = [f"<div class='u-tick' style='top:{sec/duration*100:.3f}%'>{sec:.0f}s</div>" for sec in range(0, int(duration)+1, tick_step)]
    grid = [f"<div class='u-grid' style='top:{sec/duration*100:.3f}%'></div>" for sec in range(tick_step, int(duration), tick_step)]
    trace_sections: list[str] = []
    for index, actor in enumerate(actors):
        actor_color = colors[min(actor["_column"], len(colors)-1)]
        rows: list[str] = []
        actor_events = list(actor.get("events") or [])
        if actor.get("final_message") and not any(ev.get("kind") == "ret" for ev in actor_events):
            actor_events.append({"kind": "ret", "text": actor["final_message"], "ts": actor.get("end_ts")})
        actor_events.sort(key=lambda ev: _parse_trace_time(ev.get("ts")) or finish)
        for ev in actor_events:
            when = _parse_trace_time(ev.get("ts"))
            # Recorded event timestamps and actor spans share one absolute clock.
            sec = (when - origin).total_seconds() if when else None
            position = min(99.7, max(0.0, sec / duration * 100)) if sec is not None else None
            kind = str(ev.get("kind") or "event")
            raw = ev.get("command") or ev.get("text") or ev.get("prompt") or ev.get("tool") or ""
            label = {"cmd": "command", "reason": "recorded reasoning summary", "ret": "final answer" if actor.get("is_main") else "returns to main"}.get(kind, kind)
            exit_code = ev.get("exit_code")
            exit_html = f"<span class='u-exit{' failed' if exit_code else ''}'>exit {_esc(exit_code)}</span>" if exit_code is not None else ""
            body = f"<pre class='u-terminal'>{_esc(raw)}</pre>" if kind == "cmd" else f"<div class='u-event-text'>{_esc(raw)}</div>"
            output = ev.get("output")
            if output:
                body += f"<pre class='u-captured'>{_esc(output)}</pre>"
            time_label = f"+{sec:.1f}s" if sec is not None else "—"
            attrs = f" data-position='{position:.4f}' data-time='{sec:.1f}' tabindex='0' role='button' onclick='selectTimelineEvent(this)' onkeydown=\"if(event.key==='Enter'||event.key===' '){{event.preventDefault();selectTimelineEvent(this)}}\"" if sec is not None else ""
            rows.append(
                f"<div class='u-event {actor_color} kind-{_slug(kind)}'{attrs}>"
                f"<time class='u-event-time' title='{_esc(ev.get('ts'))}'>{time_label}</time><div class='u-event-content'>"
                f"<div class='u-event-head'>{_esc(label)}{exit_html}</div>{body}</div></div>"
            )
        if not rows:
            rows.append("<p class='muted'>Trace not captured for this agent.</p>")
        name = actor.get("agent_nickname") or ("main" if actor.get("is_main") else actor.get("agent_role") or "subagent")
        prompt = actor.get("spawn_prompt") or actor.get("first_prompt")
        prompt_html = f"<div class='u-prompt'>{_esc(prompt)}</div>" if prompt else ""
        trace_sections.append(
            f"<section class='u-trace-agent {actor_color}{' hidden' if index else ''}' id='{actor['actor_id']}'>"
            f"<header class='u-trace-head'><span class='u-avatar' aria-hidden='true'>{'▸' if actor.get('is_main') else '◉'}</span><div class='u-agent-title'><b>{_esc(name)}</b><small>{_esc(actor.get('agent_role') or actor.get('model') or '')} · {len(actor_events)} events</small></div>"
            f"<button class='u-close' aria-label='Close trace' onclick=\"closeTimeline(this.closest('.u-stage'))\">×</button></header>"
            f"{prompt_html}<div class='u-event-meta'>Events are shown in time order. Select a row to locate its timestamp on the agent map.</div>"
            f"<div class='u-event-list' role='region' aria-label='Execution Trace'>{''.join(rows)}</div></section>"
        )
    return (
        f"<div class='u-card'><header class='u-card-head'><div><b>Agent timeline + trace</b>"
        f"<small>Select a bar to open that agent’s readable event stream; select an event to mark its time on the map.</small></div>"
        f"<span id='{prefix}-hint' class='u-hint'>overview · full width</span></header>"
        f"<div id='{prefix}-stage' class='u-stage{' solo' if max_columns == 1 else ''}' style='--canvas-h:{canvas_h}px;--ncols:{max_columns}'>"
        f"<div class='u-gantt'><div class='u-heads'>{''.join(heads)}</div><div class='u-plot'>"
        f"<div class='u-axis'>{''.join(ticks)}</div><div class='u-lanes'>{''.join(grid)}{''.join(lanes)}</div>"
        f"<div class='u-time-cursor' hidden><span></span></div></div></div>"
        f"<div class='u-trace'>{''.join(trace_sections)}</div></div>"
        f"<div class='u-foot'>Time flows down in the map. The trace is ordered by timestamp for readability; selecting an event marks its exact time in the map.</div></div>"
    )


def _filesystem_diagram(profile: Any) -> str:
    """Render recorded path grants as Z's folder tree without inventing access."""
    filesystem = profile.get("file_system", profile) if isinstance(profile, dict) else {}
    entries = filesystem.get("entries") if isinstance(filesystem, dict) else None
    if not isinstance(entries, list):
        return "<p class='muted'>Filesystem path grants were not recorded.</p>"
    grants: list[tuple[str, str]] = []
    access_labels = {"read": "READ", "write": "READ · WRITE", "read_write": "READ · WRITE", "none": "NO ACCESS", "deny": "NO ACCESS"}
    special = {"root": "/", "slash_tmp": "/tmp", "tmpdir": "$TMPDIR"}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if isinstance(path, dict):
            if path.get("type") == "special":
                value = path.get("value")
                kind = value.get("kind") if isinstance(value, dict) else value
                path = special.get(kind) if isinstance(kind, str) else None
            else:
                path = path.get("path")
        access = entry.get("access")
        if isinstance(path, str) and path and isinstance(access, str):
            grant = (path, access)
            if grant not in grants:
                grants.append(grant)
    if not grants:
        return "<p class='muted'>No interpretable filesystem path grants. See the recorded policy below.</p>"
    rows = []
    for path, access in sorted(grants, key=lambda grant: (not grant[0].startswith("/"), grant[0].count("/"), grant[0])):
        ancestors = {p for p, _ in grants if p != path and path.startswith(p.rstrip("/") + "/")}
        parent = max(ancestors, key=len) if ancestors else None
        depth = min(len(ancestors), 4)
        label = path[len(parent):].lstrip("/") if parent and parent != "/" else path
        if path == "/":
            label = "/ (filesystem)"
        elif len(label) > 45 and "/" in label:
            label = "…/" + "/".join(label.rstrip("/").split("/")[-2:])
        mode = "rw" if access in ("write", "read_write") else "ro" if access == "read" else "unknown"
        rows.append(
            f"<div class='z-fs-row' style='--depth:{depth}' data-path='{_esc(path)}'>"
            f"<span class='z-folder {mode}' aria-hidden='true'></span>"
            f"<span class='z-fs-path' title='{_esc(path)}'>{_esc(label)}</span>"
            f"<span class='z-access {mode}'>{_esc(access_labels.get(access, access.upper()))}</span></div>"
        )
    return "".join(rows)


def _context_component(tree: dict[str, Any] | None, prefix: str) -> str:
    """Render prototype Z's per-agent context inventory from the same activity tree."""
    if not isinstance(tree, dict):
        return "<p class='muted'>No context inventory was captured for this session.</p>"
    agents = [dict(tree, context_id=f"{prefix}-ctx-main", main=True)]
    agents.extend(dict(a, context_id=f"{prefix}-ctx-{i}", main=False) for i, a in enumerate(tree.get("children") or []))
    tabs: list[str] = []
    views: list[str] = []
    for index, agent in enumerate(agents):
        aid = agent["context_id"]
        name = agent.get("agent_nickname") or ("main" if agent["main"] else agent.get("agent_role") or "subagent")
        color = ("a-main", "a-a", "a-b", "a-c3", "a-c4")[min(index, 4)]
        tabs.append(f"<button class='z-tab {color}{' active' if index == 0 else ''}' data-context='{aid}' aria-pressed='{'true' if index == 0 else 'false'}' onclick=\"selectContextAgent('{prefix}','{aid}')\">{_esc(name)}</button>")
        usage = agent.get("token_usage") or (tree.get("token_usage") if agent["main"] else {}) or {}
        used = int(usage.get("input_tokens", 0) or 0)
        cached = int(usage.get("cached_input_tokens", 0) or 0)
        window = agent.get("context_window")
        pieces = agent.get("composition") or []
        last_input = agent.get("latest_input_tokens")
        snapshot_known = type(last_input) is int and last_input >= 0
        fill = min(100.0, last_input / window * 100) if snapshot_known and type(window) is int and window > 0 else None
        segs = f"<div class='z-seg fill-input' style='height:{fill:.3f}%'></div>" if fill is not None else ""
        water = f"<div class='z-water' style='bottom:{fill:.3f}%'></div>" if fill is not None else ""
        fill_label = f"{fill:.1f}% of window" if fill is not None else "Input snapshot unavailable"
        input_caption = f"{_num(last_input)} input tokens" if snapshot_known else "Last model input was not recorded"
        filecards = []
        for f in agent.get("files_read") or []:
            missing = f.get("status") == "missing"
            cls = " miss" if missing else ""
            label = f.get("name") or f.get("path") or ("Directory listing" if f.get("op") == "list_files" else "File read · name not recorded")
            filecards.append(f"<div class='z-file{cls}' title='{_esc(f.get('path'))}'><div class='z-doc' aria-hidden='true'><span class='{'z-doc-x' if missing else 'z-doc-lines'}'>{'×' if missing else ''}</span></div><b>{_esc(label)}</b><small>{'not found' if missing else '≈'+_num(int(f.get('char_len',0) or 0)//4)+' tok'}</small></div>")
        tools = []
        for tool in agent.get("tools_used") or []:
            count = int(tool.get("count", 0) or 0)
            pips = "".join("<i></i>" for _ in range(min(count, 12)))
            tools.append(f"<div class='z-tool'><b>{_esc(tool.get('name'))}</b><span>{pips}</span><small>×{count}</small></div>")
        profile = agent.get("permission_profile")
        sandbox = agent.get("sandbox_policy")
        permission_view = _filesystem_diagram(profile)
        sandbox_text = sandbox.get("type", "not recorded") if isinstance(sandbox, dict) else sandbox or "not recorded"
        policy_details = f"<details class='z-policy'><summary>Recorded policy</summary><pre>{_esc(json.dumps({'permissions': profile, 'sandbox': sandbox}, ensure_ascii=False, indent=2))}</pre></details>" if profile is not None or sandbox is not None else ""
        network = agent.get("network")
        net_label = "ON" if network is True else "OFF" if network is False else "not recorded"
        slash_html = "<span class='z-slash'></span>" if network is False else ""
        approval = agent.get("approval_policy") or "not recorded"
        tiles = (
            f"<div><b>{_esc(agent.get('model') or 'not recorded')}</b><small>model · {_esc(agent.get('reasoning_effort') or 'default effort')}</small></div>"
            f"<div><b>{_esc(agent.get('depth') if agent.get('depth') is not None else ('root' if agent['main'] else 'not recorded'))}</b><small>agent depth</small></div>"
            f"<div><b>{len(agent.get('files_read') or [])}</b><small>file operations</small></div>"
            f"<div><b>{sum(int(t.get('count',0) or 0) for t in agent.get('tools_used') or [])}</b><small>recorded tool calls</small></div>"
        )
        source_names = {"base": "Base instructions", "skills": "Skill and developer instructions", "environment": "Environment", "task": "Task prompts", "output": "Assistant responses"}
        source_rows = []
        for piece in pieces:
            kind = piece.get("segment") or "unknown"
            if kind == "file":
                path = piece.get("path") or ""
                label = piece.get("name") or str(path).rstrip("/").split("/")[-1] or "File read · name not recorded"
            else:
                path = ""
                label = source_names.get(kind, str(kind).replace("_", " ").capitalize())
            source_rows.append(
                f"<div class='z-break-row'><i class='z-swatch src-{_slug(kind)}' aria-hidden='true'></i>"
                f"<b title='{_esc(path)}'>{_esc(label)}</b><span>≈{_num(piece.get('token_estimate', 0))} tokens</span></div>"
            )
        composition_rows = "".join(source_rows) or "<p class='muted'>Source text was not retained.</p>"
        fcards = "".join(filecards) or "<p class='muted'>No parsed read/list operations recorded.</p>"
        toolrows = "".join(tools) or "<p class='muted'>No tool calls recorded.</p>"
        views.append(
            f"<section class='z-agent{' hidden' if index else ''}' id='{aid}'>"
            f"<div class='z-layout'><aside class='z-tank-column'><h4>{_esc(name)} · last input</h4>"
            f"<div class='z-tank'>{segs}{water}</div><b class='z-fill'>{fill_label}</b>"
            f"<small>{input_caption} · capacity {_num(window) if window else 'not recorded'}</small><p class='z-note'>Last recorded model input, including cached tokens.</p></aside>"
            f"<div class='z-sections'><section class='z-card'><h4>Recorded context sources</h4>{composition_rows}"
            f"<p class='z-note'>Estimated from source text retained across this rollout (about 4 characters per token). This breakdown is not a snapshot of one model call.</p></section>"
            f"<section class='z-card'><h4>At a glance</h4><div class='z-tiles'>{tiles}</div></section>"
            f"<section class='z-card'><h4>Where it could read and write</h4><div class='z-sandbox'><div class='z-fsmap'>{permission_view}</div>"
            f"<div class='z-network{' off' if network is False else ' unknown' if network is not True else ''}'><div class='z-globe' aria-hidden='true'>{slash_html}</div><b>Network {net_label}</b></div></div>"
            f"<p class='z-policy-note'>Sandbox: {_esc(sandbox_text)} · Approval: {_esc(approval)}</p>{policy_details}</section>"
            f"<section class='z-card'><h4>Files it pulled into context</h4><div class='z-files'>{fcards}</div><p class='z-note'>Exact paths and names come from Codex parsed read/list operations. Token sizes are estimates based on retained command output.</p></section>"
            f"<section class='z-card'><h4>Tools used</h4><div class='z-tools'>{toolrows}</div><p class='z-note'>Shows tools observed in the trace. The rollout does not record a positive tool allowlist.</p></section>"
            f"<section class='z-card'><h4>Input reuse</h4><div class='z-cache'><i style='width:{(cached/used*100 if used else 0):.2f}%'></i><b style='width:{(100-cached/used*100 if used else 0):.2f}%'></b></div>"
            f"<div class='z-cache-label'>cached {_num(cached)} · fresh {_num(max(0,used-cached))}</div></section>"
            f"<section class='z-card'><details class='z-base'><summary>Base instructions · as recorded, not interpreted</summary><pre>{_esc(agent.get('base_instructions_text') or 'Not captured.')}</pre></details></section>"
            f"</div></div></section>"
        )
    return f"<div class='z-inventory'><h3>Agent context inventory</h3><nav class='z-tabs'>{''.join(tabs)}</nav>{''.join(views)}</div>"


def _detail_tabs(prefix: str, overview: str, timeline: str, context: str, grading: str = "") -> str:
    tabs = [("overview", "Overview"), ("timeline", "Timeline"), ("context", "Context")]
    if grading:
        tabs.append(("grading", "Grading"))
    buttons = "".join(
        f"<button class='detail-tab{' active' if idx == 0 else ''}' onclick=\"selectDetailTab('{prefix}','{key}')\">{label}</button>"
        for idx, (key, label) in enumerate(tabs)
    )
    panels = {"overview": overview, "timeline": timeline, "context": context, "grading": grading}
    content = "".join(
        f"<section class='detail-tab-panel{' hidden' if idx else ''}' id='{prefix}-panel-{key}'>{panels[key]}</section>"
        for idx, (key, _label) in enumerate(tabs)
    )
    return f"<nav class='detail-tabs'>{buttons}</nav>{content}"


def _real_session_details(summary: dict[str, Any]) -> str:
    views: list[str] = []
    for idx, sess in enumerate(summary["sessions"]):
        tok = sess["token_usage"]
        detail_id = f"real-sess-{idx}"
        skills = ", ".join(sess["skills_used"]) or "—"
        turns_sub = f"{sess['command_count']} cmds"
        overview = (
            f"<div class='kpi-grid'>"
            f"{_kpi('Billed output', _num(tok['billed_output_tokens']), 'tokens')}"
            f"{_kpi('Uncached input', _num(tok['uncached_input_tokens']), 'tokens')}"
            f"{_kpi('Cached input', _num(tok['cached_input_tokens']), 'tokens')}"
            f"{_kpi('Turns', str(sess['turn_count']), turns_sub)}"
            "</div>"
        )
        timeline = _timeline_component(sess.get("activity_tree"), f"{detail_id}-timeline")
        if not sess.get("activity_tree"):
            timeline += _command_timeline(sess.get("commands") or [])
        context = _context_component(sess.get("activity_tree"), f"{detail_id}-context")
        views.append(
            f"<div id='{detail_id}' class='detail-view hidden'>"
            f"<button class='crumb-link' onclick='backToRealOverview()'>← Online eval</button>"
            f"<div class='card'><div class='card-header'><div>"
            f"<div class='card-title'>Session <code>{_esc(str(sess['thread_id'])[:16])}</code></div>"
            f"<div class='card-subtitle'>{_esc(_ts(sess['started_at']))} · "
            f"{_esc(sess['model'] or 'unknown')} · skills: {_esc(skills)}</div></div>"
            f"<span class='cost-total'>{_esc(_cost(sess['est_cost']))}</span></div></div>"
            f"{_detail_tabs(detail_id, overview, timeline, context)}"
            "</div>"
        )
    return "".join(views)


def _real_skill_details(summary: dict[str, Any]) -> str:
    """One detail view per triggered skill, listing the sessions that used it."""
    views: list[str] = []
    sessions = summary["sessions"]
    for sk in summary["skills"]:
        if sk["sessions_triggered"] <= 0:
            continue
        name = sk["skill"]
        vid = _slug("real-skill", name)
        tok = sk["tokens"]
        rows: list[str] = []
        for idx, sess in enumerate(sessions):
            if name not in sess.get("skills_used", []):
                continue
            mode = (sess.get("skill_invocations") or {}).get(name, "")
            mode_badge = (
                f"<span class='badge badge-blue'>{_esc(mode)}</span>" if mode else ""
            )
            rows.append(
                f"<tr class='clickable-row' onclick=\"openSessionDetail('real-sess-{idx}')\">"
                f"<td><code>{_esc(_ts(sess['started_at']))}</code></td>"
                f"<td><code>{_esc(str(sess['thread_id'])[:12])}</code></td>"
                f"<td>{mode_badge}</td>"
                f"<td class='num'>{sess['turn_count']}</td>"
                f"<td class='num'>{_num(sess['token_usage']['billed_output_tokens'])}</td>"
                f"<td class='num'>{_esc(_cost(sess['est_cost']))}</td>"
                f"<td class='prompt-cell' title='{_esc(sess['first_prompt'])}'>"
                f"{_esc(sess['first_prompt'] or '—')}</td></tr>"
            )
        body = (
            "<table><thead><tr><th>Started (UTC)</th><th>Thread</th><th>Mode</th>"
            "<th class='num'>Turns</th><th class='num'>Billed Out</th>"
            "<th class='num'>Est. Cost</th><th>First Prompt</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
            if rows
            else "<p class='muted'>No sessions recorded for this skill.</p>"
        )
        cost_cell = _cost(sk["est_cost"]) if sk["priced_count"] > 0 else "n/a"
        trig_sub = f"{sk['explicit_count']} explicit · {sk['implicit_count']} implicit"
        views.append(
            f"<div id='{vid}' class='detail-view hidden'>"
            f"<button class='crumb-link' onclick='backToRealOverview()'>← Online eval</button>"
            f"<div class='card'><div class='card-header'><div>"
            f"<div class='card-title'>Skill: <code>{_esc(name)}</code></div>"
            f"<div class='card-subtitle'>Sessions that triggered this skill — "
            f"click a row for its full trace</div></div>"
            f"<span class='badge badge-slate'>{_esc(cost_cell)}</span></div>"
            f"<div class='kpi-grid'>"
            f"{_kpi('Sessions triggered', str(sk['sessions_triggered']), trig_sub)}"
            f"{_kpi('Billed output', _num(tok['billed_output_tokens']), 'tokens')}"
            f"{_kpi('Uncached input', _num(tok['uncached_input_tokens']), 'tokens')}"
            f"{_kpi('Est. cost', cost_cell, 'API-equiv.')}"
            f"</div>{body}</div></div>"
        )
    return "".join(views)


# --------------------------------------------------------------------------- #
# Harness Eval Calls                                                          #
# --------------------------------------------------------------------------- #

def _group_eval_by_experiment(
    harness_evals: dict[str, Any]
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for sess in harness_evals["sessions"]:
        key = (sess["eval_skill"], sess["experiment"])
        grouped[key].append(sess)
    return grouped


def _eval_skill_rows(harness_evals: dict[str, Any]) -> str:
    rows: list[str] = []
    # Per-skill latest-experiment outcome, derived from the trial sessions.
    from _lib.usage import summarize_experiment_outcome

    latest_by_skill: dict[str, dict[str, Any]] = {}
    sessions_by_skill: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for s in harness_evals.get("sessions", []):
        sessions_by_skill.setdefault(s["eval_skill"], {}).setdefault(
            s["experiment"], []
        ).append(s)
    for skill, exps in sessions_by_skill.items():
        latest_exp = sorted(exps)[-1]  # experiments sort lexically; newest last
        latest_by_skill[skill] = {
            "experiment": latest_exp,
            "outcome": summarize_experiment_outcome(exps[latest_exp]),
        }

    max_cost = max((float(es.get("est_cost") or 0) for es in harness_evals["by_skill"]), default=0.0)
    for es in harness_evals["by_skill"]:
        wtok, jtok = es["worker_tokens"], es["judge_tokens"]
        latest = latest_by_skill.get(es["skill"])
        if latest:
            oc = latest["outcome"]
            ocls = {"PASS": "green", "FAIL": "rose", "UNKNOWN": "slate"}.get(oc["outcome"], "slate")
            score_txt = f"{oc['mean_score']}/100" if oc["mean_score"] is not None else "—"
            outcome_cell = (
                f"<div class='outcome-score'><i class='status-dot {ocls}'></i><b>{_esc(score_txt)}</b><small>{_esc(oc['outcome'])}</small></div>"
                f"<div class='row-meta'>{_esc(latest['experiment'])}</div>"
            )
        else:
            outcome_cell = "<span class='muted'>—</span>"
        rows.append(
            f"<tr>"
            f"<td><div class='row-title'>{_esc(es['skill'])}</div></td>"
            f"<td class='num'><span class='count-value'>{es['experiment_count']}</span></td>"
            f"<td>{outcome_cell}</td>"
            f"<td class='num'>{es['worker_trials']} "
            f"<span class='muted'>({es['with_skill_trials']}w/{es['baseline_trials']}b)</span></td>"
            f"<td class='num'>{es['subagent_spawns']}</td>"
            f"<td class='num'>{es['judge_calls']}</td>"
            f"<td class='num'>{_num(wtok['billed_output_tokens'])}</td>"
            f"<td class='num'>{_num(jtok['billed_output_tokens'])}</td>"
            f"<td class='num'>{_cost_meter(es['worker_est_cost'], max_cost, zero_ok=es['priced_count'] > 0)}</td>"
            f"<td class='num'>{_cost_meter(es['judge_est_cost'], max_cost, color='amber', zero_ok=es['priced_count'] > 0)}</td>"
            f"<td class='num'>{_cost_meter(es['est_cost'], max_cost, zero_ok=es['priced_count'] > 0)}</td>"
            f"</tr>"
        )
    if not rows:
        rows.append(
            "<tr><td colspan='11' class='empty'>No harness eval runs recorded "
            "under <code>&lt;skill&gt;/runs/</code>.</td></tr>"
        )
    return "".join(rows)


def _experiment_history_rows(harness_evals: dict[str, Any]) -> str:
    grouped = _group_eval_by_experiment(harness_evals)
    rows: list[str] = []
    max_cost = max((sum(float(t.get("est_cost") or 0) for t in trials) for trials in grouped.values()), default=0.0)
    for (skill, exp), trials in sorted(grouped.items(), reverse=True):
        worker = sum(1 for t in trials if t["configuration"] in ("with-skill", "baseline"))
        judges = sum(t.get("judge_call_count", 0) for t in trials)
        billed = sum(t["token_usage"]["billed_output_tokens"] for t in trials)
        costs = [t["est_cost"] for t in trials if t["est_cost"] is not None]
        total_cost = round(sum(costs), 6) if costs else None
        models = sorted({t["model"] or "unknown" for t in trials})
        model_label = models[0] + ("…" if len(models) > 1 else "")
        from _lib.usage import summarize_experiment_outcome
        outcome = summarize_experiment_outcome(trials)["outcome"]
        indicator = {"PASS": "green", "FAIL": "rose", "UNKNOWN": "slate"}.get(outcome, "slate")
        vid = _slug("eval-exp", skill, exp)
        rows.append(
            f"<tr class='clickable-row' onclick=\"openEvalExperiment('{vid}')\">"
            f"<td><code>{_esc(skill)}</code></td>"
            f"<td><code>{_esc(exp)}</code></td>"
            f"<td><code>{_esc(model_label)}</code></td>"
            f"<td><span class='outcome-inline'><i class='status-dot {indicator}'></i>{_esc(outcome)}</span></td>"
            f"<td class='num'>{worker}</td>"
            f"<td class='num'>{judges}</td>"
            f"<td class='num'>{_num(billed)}</td>"
            f"<td class='num'>{_cost_meter(total_cost, max_cost, zero_ok=bool(costs))}</td>"
            f"</tr>"
        )
    if not rows:
        rows.append(
            "<tr><td colspan='8' class='empty'>No experiments recorded yet.</td></tr>"
        )
    return "".join(rows)


def _trial_rows_for_experiment(trials: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for t in sorted(trials, key=lambda x: (x["test_case"], x["configuration"], x["trial"])):
        tok = t["token_usage"]
        cfg = t["configuration"]
        cfg_cls = {"with-skill": "badge-green", "baseline": "badge-blue"}.get(
            cfg, "badge-slate"
        )
        model = t["model"] or "unknown"
        if t.get("reasoning_effort"):
            model = f"{model} ({t['reasoning_effort']})"
        vid = _slug(
            "eval-trial", t["eval_skill"], t["experiment"], t["test_case"],
            t["configuration"], t["trial"],
        )
        rows.append(
            f"<tr class='clickable-row' onclick=\"openEvalTrial('{vid}')\">"
            f"<td><code>{_esc(t['test_case'])}</code></td>"
            f"<td><span class='badge {cfg_cls}'>{_esc(cfg)}</span></td>"
            f"<td><code>{_esc(t['trial'])}</code></td>"
            f"<td><code>{_esc(model)}</code></td>"
            f"<td class='num'>{t['turn_count']}</td>"
            f"<td class='num'>{t['subagent_spawn_count']}</td>"
            f"<td class='num'>{t.get('judge_call_count', 0)}</td>"
            f"<td class='num'>{_num(tok['billed_output_tokens'])}</td>"
            f"<td class='num'><strong>{_esc(_cost(t['est_cost']))}</strong></td>"
            f"</tr>"
        )
    return "".join(rows)


def _eval_experiment_details(harness_evals: dict[str, Any]) -> str:
    grouped = _group_eval_by_experiment(harness_evals)
    views: list[str] = []
    for (skill, exp), trials in sorted(grouped.items(), reverse=True):
        vid = _slug("eval-exp", skill, exp)
        worker = sum(1 for t in trials if t["configuration"] in ("with-skill", "baseline"))
        judges = sum(t.get("judge_call_count", 0) for t in trials)
        subs = sum(t["subagent_spawn_count"] for t in trials)
        billed = sum(t["token_usage"]["billed_output_tokens"] for t in trials)
        costs = [t["est_cost"] for t in trials if t["est_cost"] is not None]
        total_cost = round(sum(costs), 6) if costs else None
        from _lib.usage import summarize_experiment_outcome

        oc = summarize_experiment_outcome(trials)
        outcome_cls = {
            "PASS": "badge-green",
            "FAIL": "badge-amber",
            "UNKNOWN": "badge-slate",
        }.get(oc["outcome"], "badge-slate")
        score_txt = (
            f"{oc['mean_score']}/100" if oc["mean_score"] is not None else "—"
        )
        det_sem = []
        if oc["mean_det"] is not None:
            det_sem.append(f"Det {oc['mean_det']}")
        if oc["mean_sem"] is not None:
            det_sem.append(f"Sem {oc['mean_sem']}")
        det_sem_txt = " · ".join(det_sem) or "no grades"
        outcome_value = (
            f"<span class='badge {outcome_cls}'>{_esc(oc['outcome'])}</span> {score_txt}"
        )
        outcome_sub = (
            f"{oc['trials_passed']}/{oc['trials_total']} trials passed · {det_sem_txt}"
        )
        billed_sub = f"{_num(billed)} billed out"
        # Setup card: model / reasoning effort / trials-per-cell (from trials)
        models = sorted({t["model"] or "unknown" for t in trials})
        efforts = sorted({t.get("reasoning_effort") or "default" for t in trials})
        model_val = models[0] + ("…" if len(models) > 1 else "")
        setup_sub = f"effort: {', '.join(efforts)} · {len(trials)} trial(s)"
        worker_sub = f"{subs} subagents · {judges} judges"
        views.append(
            f"<div id='{vid}' class='detail-view hidden'>"
            f"<button class='crumb-link' onclick='backToEvalOverview()'>← Offline eval</button>"
            f"<div class='card'><div class='card-header'><div>"
            f"<div class='card-title'>{_esc(skill)} / {_esc(exp)}</div>"
            f"<div class='card-subtitle'>Click a trial to open its trace &amp; grading</div>"
            f"</div><div class='card-head-actions'>"
            f"<a class='btn-link' href='../{_esc(skill)}/runs/{_esc(exp)}/report.md' target='_blank' rel='noopener'>Open raw report.md ↗</a>"
            f"<span class='badge badge-slate'>{_esc(_cost(total_cost, zero_ok=bool(costs)))}</span>"
            f"</div></div>"
            f"<div class='kpi-grid'>"
            f"{_kpi('Outcome & score', outcome_value, outcome_sub)}"
            f"{_kpi('Worker trials', str(worker), worker_sub)}"
            f"{_kpi('Est. cost', _cost(total_cost, zero_ok=bool(costs)), billed_sub)}"
            f"{_kpi('Setup', _esc(model_val), setup_sub)}"
            f"</div>"
            f"{_comparison_table(trials)}"
            f"<h4>Trials</h4>"
            f"<table><thead><tr><th>Test case</th><th>Config</th><th>Trial</th>"
            f"<th>Model</th><th class='num'>Turns</th><th class='num'>Subagents</th>"
            f"<th class='num'>Judges</th><th class='num'>Billed out</th>"
            f"<th class='num'>Est. cost</th></tr></thead>"
            f"<tbody>{_trial_rows_for_experiment(trials)}</tbody></table>"
            f"</div></div>"
        )
    return "".join(views)


def _grade_check_rows(checks: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for c in checks:
        passed = c.get("pass")
        badge = (
            "<span class='badge badge-green'>pass</span>"
            if passed
            else "<span class='badge badge-amber'>fail</span>"
        )
        rows.append(
            f"<tr><td><code>{_esc(c.get('id'))}</code></td>"
            f"<td>{badge}</td>"
            f"<td class='num'>{_esc(c.get('points'))}</td>"
            f"<td class='notes-cell'>{_esc(c.get('notes') or '')}</td></tr>"
        )
    return "".join(rows)


def _grade_panel(title: str, grade: dict[str, Any] | None, *, extra: str = "") -> str:
    if not grade:
        return (
            f"<div class='grade-panel'><div class='grade-panel-head'>{_esc(title)}"
            f"</div><p class='muted'>Not graded.</p></div>"
        )
    verdict = grade.get("verdict") or ("pass" if grade.get("overall_pass") else "—")
    vcls = {"pass": "badge-green", "partial": "badge-amber", "fail": "badge-amber"}.get(
        verdict, "badge-slate"
    )
    checks = grade.get("checks") or []
    body = (
        "<table><thead><tr><th>Check</th><th>Pass</th><th class='num'>Pts</th>"
        "<th>Notes</th></tr></thead>"
        f"<tbody>{_grade_check_rows(checks)}</tbody></table>"
        if checks
        else "<p class='muted'>No checks recorded.</p>"
    )
    return (
        f"<div class='grade-panel'><div class='grade-panel-head'>{_esc(title)}"
        f"<span class='grade-score'>{_esc(grade.get('score'))}/100 "
        f"<span class='badge {vcls}'>{_esc(verdict)}</span></span></div>"
        f"{extra}{body}</div>"
    )


def _verdict_badge(verdict: str) -> str:
    cls = {
        "improved": "badge-green",
        "regressed": "badge-amber",
        "unchanged": "badge-slate",
    }.get(verdict, "badge-slate")
    return f"<span class='badge {cls}'>{_esc(verdict)}</span>"


def _cmp_cell(cell: dict[str, Any] | None, key: str, *, cost: bool = False) -> str:
    if not cell or cell.get(key) is None:
        return "<span class='muted'>—</span>"
    val = cell[key]
    if cost:
        return _cost(val)
    if isinstance(val, (int, float)) and float(val).is_integer():
        return _num(val)
    return _esc(val)


def _comparison_table(trials: list[dict[str, Any]]) -> str:
    """Render the baseline vs with-skill comparison per test case."""
    from _lib.usage import derive_experiment_comparison

    comparison = derive_experiment_comparison(trials)
    if not comparison:
        return ""
    rows: list[str] = []
    for c in comparison:
        base, skill = c["baseline"], c["with_skill"]
        rows.append(
            f"<tr><td rowspan='3'><code>{_esc(c['test_case'])}</code></td>"
            f"<td>Mean score</td>"
            f"<td class='num'>{_cmp_cell(base, 'mean_score')}</td>"
            f"<td class='num'>{_cmp_cell(skill, 'mean_score')}</td>"
            f"<td class='num'>{_esc(c['score_delta'])}</td>"
            f"<td>{_verdict_badge(c['score_verdict'])}</td></tr>"
            f"<tr><td>Billed output</td>"
            f"<td class='num'>{_cmp_cell(base, 'mean_billed_output')}</td>"
            f"<td class='num'>{_cmp_cell(skill, 'mean_billed_output')}</td>"
            f"<td class='num'>{_esc(c['billed_output_delta'])}</td>"
            f"<td>{_verdict_badge(c['billed_output_verdict'])}</td></tr>"
            f"<tr><td>Est. cost</td>"
            f"<td class='num'>{_cmp_cell(base, 'mean_cost', cost=True)}</td>"
            f"<td class='num'>{_cmp_cell(skill, 'mean_cost', cost=True)}</td>"
            f"<td class='num'>{_esc(c['cost_delta'])}</td>"
            f"<td>{_verdict_badge(c['cost_verdict'])}</td></tr>"
        )
    return (
        "<h4>Baseline vs With-Skill</h4>"
        "<p class='muted grade-meta'>Means over the experiment's trials. Score is "
        "higher-is-better; billed output and cost are lower-is-better.</p>"
        "<table><thead><tr><th>Test case</th><th>Axis</th>"
        "<th class='num'>Baseline</th><th class='num'>With-skill</th>"
        "<th class='num'>Delta</th><th>Verdict</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _eval_trial_details(harness_evals: dict[str, Any]) -> str:
    views: list[str] = []
    for t in harness_evals["sessions"]:
        vid = _slug(
            "eval-trial", t["eval_skill"], t["experiment"], t["test_case"],
            t["configuration"], t["trial"],
        )
        exp_vid = _slug("eval-exp", t["eval_skill"], t["experiment"])
        ptok = t["parent_token_usage"]
        stok = t["subagent_token_usage"]
        jtok = t["judge_tokens"]
        tok = t["token_usage"]

        cfg = t["configuration"]
        cfg_cls = {"with-skill": "badge-green", "baseline": "badge-blue"}.get(cfg, "badge-slate")

        # Grading: deterministic + semantic side-by-side (prototype-E design)
        grades = t.get("grades") or {"deterministic": None, "semantic": None}
        sem = grades.get("semantic")
        sem_extra = ""
        if sem:
            dj = sem.get("deciding_judges")
            ab = sem.get("abstaining_judges")
            if dj is not None:
                ab_txt = f", {ab} abstaining" if ab else ""
                quorum_val = sem.get("quorum")
                quorum_txt = f", quorum {quorum_val}" if quorum_val is not None else ""
                sem_extra = (
                    f"<p class='muted grade-meta'>{dj} deciding judge(s)"
                    f"{ab_txt}{quorum_txt}</p>"
                )
        judge_blocks = ""
        for j in (sem or {}).get("judges", []):
            jv = j.get("verdict") or "—"
            jcls = {"pass": "badge-green", "partial": "badge-amber", "fail": "badge-amber"}.get(
                jv, "badge-slate"
            )
            criterion_cards = ""
            for c in j.get("checks") or []:
                c_pass = c.get("pass")
                c_cls = "badge-green" if c_pass else "badge-amber"
                c_badge = "pass" if c_pass else "fail"
                criterion_cards += (
                    f"<div class='criterion-card'>"
                    f"<div class='criterion-head'>"
                    f"<span class='badge {c_cls}'>{c_badge} · {_esc(c.get('points'))} pts</span> "
                    f"<code>{_esc(c.get('id'))}</code></div>"
                    f"<div class='criterion-notes'>{_esc(c.get('notes') or '—')}</div>"
                    f"</div>"
                )
            if not criterion_cards:
                criterion_cards = "<p class='muted'>No criteria recorded.</p>"
            judge_blocks += (
                f"<div class='judge-block'><div class='judge-head'>Judge "
                f"{_esc(j.get('judge'))} <span class='badge {jcls}'>{_esc(jv)}</span> "
                f"<span class='grade-score'>{_esc(j.get('score'))}/100</span></div>"
                f"{criterion_cards}</div>"
            )
        grade_section = (
            "<div class='grade-grid'>"
            f"{_grade_panel('Deterministic', grades.get('deterministic'))}"
            f"{_grade_panel('Semantic (consensus)', sem, extra=sem_extra)}"
            "</div>"
        )
        if judge_blocks:
            grade_section += f"<h4>Per-judge detail</h4>{judge_blocks}"

        tree = t.get("activity_tree")
        start_dt = _parse_trace_time(t.get("start_ts"))
        end_dt = _parse_trace_time(t.get("end_ts"))
        wall_seconds = max(0.0, (end_dt - start_dt).total_seconds()) if start_dt and end_dt else None
        wall_label = f"{wall_seconds:.1f}s" if wall_seconds is not None else "—"
        det_grade = grades.get("deterministic") or {}
        sem_grade = grades.get("semantic") or {}
        det_score = det_grade.get("score")
        sem_score = sem_grade.get("score")
        display_score = sem_score if sem_score is not None else det_score
        det_pass = det_grade.get("overall_pass", str(det_grade.get("verdict", "")).lower() == "pass")
        sem_pass = sem_grade.get("overall_pass", str(sem_grade.get("verdict", "")).lower() == "pass") if sem_grade else True
        has_grade = bool(det_grade or sem_grade)
        any_fail = bool(det_grade and not det_pass) or bool(sem_grade and not sem_pass)
        outcome = "FAIL" if any_fail else "PASS" if has_grade else "UNSCORED"
        score_label = f"{outcome} · {display_score}/100" if display_score is not None else outcome
        score_parts = []
        if det_score is not None:
            score_parts.append(f"Det {det_score}")
        if sem_score is not None:
            score_parts.append(f"Sem {sem_score}")
        if not sem_grade:
            score_parts.append("semantic judge not run")
        sub_count = int(t.get("subagent_spawn_count", 0) or 0)
        judge_count = int(t.get("judge_call_count", 0) or 0)
        command_sub = f"{t['failed_command_count']} failed · {t['turn_count']} turns"
        delegation_sub = f"{judge_count} judge calls"
        overview_kpis = (
            f"<div class='kpi-grid'>"
            f"{_kpi('Outcome', _esc(score_label), ' · '.join(score_parts))}"
            f"{_kpi('Wall clock', wall_label, 'recorded trace duration')}"
            f"{_kpi('Commands', str(t['command_count']), command_sub)}"
            f"{_kpi('Delegation', str(sub_count), delegation_sub)}"
            f"</div>"
        )
        output_parts = [
            (int(ptok.get("billed_output_tokens", 0) or 0), "var(--primary)"),
            (int(stok.get("billed_output_tokens", 0) or 0), "var(--indigo)"),
            (int(jtok.get("billed_output_tokens", 0) or 0), "var(--amber)"),
        ]
        output_bar = _mini_bar(output_parts)
        usage_summary = (
            f"<div class='overview-usage card'><div class='overview-usage-head'>"
            f"<div><div class='card-title'>Usage at a glance</div><div class='card-subtitle'>Billed output across worker, subagents, and judges</div></div>"
            f"<strong>{_num(tok['billed_output_tokens'])} tokens</strong></div>{output_bar}"
            f"<div class='usage-legend'><span><i style='background:var(--primary)'></i>Worker {_num(ptok['billed_output_tokens'])}</span>"
            f"<span><i style='background:var(--indigo)'></i>Subagents {_num(stok['billed_output_tokens'])}</span>"
            f"<span><i style='background:var(--amber)'></i>Judges {_num(jtok['billed_output_tokens'])}</span>"
            f"<span class='usage-total'>Estimated cost {_esc(_cost(t['est_cost']))}</span></div></div>"
        )
        overview = overview_kpis + usage_summary
        timeline = _timeline_component(tree, f"{vid}-timeline")
        if not tree:
            timeline += _command_timeline(t.get("commands") or [])
        context = _context_component(tree, f"{vid}-context")

        views.append(
            f"<div id='{vid}' class='detail-view hidden'>"
            f"<button class='crumb-link' onclick=\"openEvalExperiment('{exp_vid}')\">"
            f"← {_esc(t['eval_skill'])} / {_esc(t['experiment'])}</button>"
            f"<div class='card'><div class='card-header'><div>"
            f"<div class='card-title'>{_esc(t['test_case'])} "
            f"<span class='badge {cfg_cls}'>{_esc(cfg)}</span> / {_esc(t['trial'])}</div>"
            f"<div class='card-subtitle'>{_esc(_ts(t['started_at']))} · "
            f"{_esc(t['model'] or 'unknown')}</div></div></div></div>"
            f"{_detail_tabs(vid, overview, timeline, context, grade_section)}"
            "</div>"
        )
    return "".join(views)


# --------------------------------------------------------------------------- #
# Shared chrome                                                               #
# --------------------------------------------------------------------------- #

def _mini_bar(segments: list[tuple[float, str]]) -> str:
    """Render a proportional mini bar from (value, css-color) segments."""
    total = sum(max(v, 0) for v, _ in segments)
    if total <= 0:
        return ""
    parts = "".join(
        f"<div style='width:{max(v, 0) / total * 100:.1f}%;background:{color};'></div>"
        for v, color in segments
    )
    return f"<div class='mini-bar'>{parts}</div>"


def _kpi(label: str, value: str, sub: str = "", bar: str = "") -> str:
    sub_html = f"<div class='kpi-sub'>{sub}</div>" if sub else ""
    return (
        f"<div class='kpi-card'><div class='kpi-label'>{_esc(label)}</div>"
        f"<div class='kpi-value'>{value}</div>{sub_html}{bar}</div>"
    )


def _latest_banner(label: str, meta: str, prompt: str, action: str) -> str:
    return (
        f"<div class='latest-banner'><div class='latest-left'>"
        f"<div class='pulse-dot'></div><div>"
        f"<div class='latest-meta'><span class='latest-tag'>{label}</span>{meta}</div>"
        f"<div class='latest-prompt'>{prompt}</div></div></div>{action}</div>"
    )


def render_usage_html(summary: dict[str, Any]) -> str:
    """Render the interactive two-mode usage explorer (prototype-E layout)."""
    totals = summary["totals"]
    tok = totals["tokens"]
    harness_evals = summary.get("harness_evals") or {
        "totals": {
            "worker_session_count": 0, "subagent_spawn_count": 0,
            "judge_call_count": 0, "total_call_count": 0,
            "tokens": {"uncached_input_tokens": 0, "cached_input_tokens": 0,
                       "billed_output_tokens": 0, "input_tokens": 0},
            "cache_hit_pct": 0.0, "est_cost": None,
        },
        "by_skill": [], "subagents": [], "models": [], "sessions": [],
    }
    etotals = harness_evals["totals"]
    etok = etotals["tokens"]
    combined = summary.get("combined_totals") or {}

    project_name = _esc(summary["project_name"])
    project_root = _esc(summary["project_root"])
    generated_at = _esc(_ts(summary["generated_at"]) + " UTC")

    real_cost = _cost(totals["est_cost"], zero_ok=totals["session_count"] > 0)
    eval_cost = _cost(etotals["est_cost"], zero_ok=etotals["total_call_count"] > 0)
    real_cost_bar = _mini_bar([
        (totals.get("parent_est_cost") or 0.0, "var(--primary)"),
        (totals.get("subagent_est_cost") or 0.0, "var(--indigo)"),
    ])
    eval_cost_bar = _mini_bar([
        (etotals.get("worker_est_cost") or 0.0, "var(--primary)"),
        (etotals.get("judge_est_cost") or 0.0, "var(--amber)"),
    ])
    combined_cost = _cost(
        combined.get("est_cost"),
        zero_ok=(totals["session_count"] + etotals["total_call_count"]) > 0,
    )

    # Latest-call spotlights
    real_sessions = summary["sessions"]
    if real_sessions:
        s0 = real_sessions[0]
        real_banner = _latest_banner(
            "LATEST CALL",
            f"<span>{_esc(_ts(s0['started_at']))}</span><span>·</span>"
            f"<span class='mono'>{_esc(_cost(s0['est_cost']))}</span>"
            f"<span>({_num(s0['token_usage']['billed_output_tokens'])} billed out)</span>",
            _esc(s0["first_prompt"] or "—"),
            "<button class='btn btn-primary' onclick=\"openSessionDetail('real-sess-0')\">Inspect Call &amp; Trace →</button>",
        )
    else:
        real_banner = (
            "<div class='latest-banner empty-banner'>No real project work sessions "
            "recorded yet.</div>"
        )

    eval_sessions = harness_evals["sessions"]
    if eval_sessions:
        e0 = eval_sessions[0]
        e0_vid = _slug("eval-exp", e0["eval_skill"], e0["experiment"])
        # Outcome of the experiment the banner points at.
        from _lib.usage import summarize_experiment_outcome as _soutcome

        e0_trials = [
            s
            for s in eval_sessions
            if s["eval_skill"] == e0["eval_skill"] and s["experiment"] == e0["experiment"]
        ]
        e0_oc = _soutcome(e0_trials)
        e0_ocls = {"PASS": "badge-green", "FAIL": "badge-amber", "UNKNOWN": "badge-slate"}.get(
            e0_oc["outcome"], "badge-slate"
        )
        e0_score = f"{e0_oc['mean_score']}/100" if e0_oc["mean_score"] is not None else ""
        eval_banner = _latest_banner(
            "LATEST EVAL",
            f"<span class='badge {e0_ocls}'>{_esc(e0_oc['outcome'])} {e0_score}</span>"
            f"<span>{_esc(e0['eval_skill'])} / {_esc(e0['experiment'])}</span><span>·</span>"
            f"<span class='mono'>{_esc(_cost(e0['est_cost']))}</span>"
            f"<span>({_esc(e0['configuration'])})</span>",
            _esc(e0["first_prompt"] or f"{e0['test_case']} / {e0['trial']}"),
            f"<button class='btn btn-primary' onclick=\"openEvalExperiment('{e0_vid}')\">Open Experiment →</button>",
        )
    else:
        eval_banner = (
            "<div class='latest-banner empty-banner'>No harness eval runs recorded "
            "yet.</div>"
        )

    real_tab_pill = f"{real_cost} · {totals['session_count']} sessions"
    eval_tab_pill = (
        f"{eval_cost} · {len({(s['eval_skill'], s['experiment']) for s in eval_sessions})} exps"
        f" · {etotals['worker_session_count']} trials"
    )

    cost_note = (
        f"Est. API cost uses public OpenAI list prices as of {_esc(PRICING_AS_OF)} "
        f"({_esc(PRICING_SOURCE)}) and is not actual subscription or credit spend."
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Codex Usage — {project_name}</title>
<style>{_STYLE}</style>
</head>
<body>
<header class="topbar">
  <div class="brand">
    <span class="brand-title">Codex Zero-Token Telemetry</span>
    <span class="repo-badge">{project_name}</span>
  </div>
  <div class="mode-tabs">
    <button class="mode-tab active" id="tab-real" onclick="switchMode('real')">
      Online eval <span class="tab-pill">{_esc(real_tab_pill)}</span>
    </button>
    <button class="mode-tab" id="tab-eval" onclick="switchMode('eval')">
      Offline eval <span class="tab-pill">{_esc(eval_tab_pill)}</span>
    </button>
  </div>
  <div class="updated">Updated {generated_at}</div>
</header>

<main class="container">
  <div class="root-meta">Project root: <code>{project_root}</code> ·
    Combined est. cost: <strong>{_esc(combined_cost)}</strong>
    (real {_esc(real_cost)} · eval {_esc(eval_cost)})</div>

  <!-- ============================ MODE: REAL ============================ -->
  <div id="mode-real">
    <div id="real-overview">
      <div class="note-box"><strong>Online eval · telemetry only.</strong> Real-session traces are collected and inspected here; scoring is not wired yet.</div>
      {real_banner}
      <div class="kpi-grid">
        {_kpi("Total Est. Cost", real_cost, f"{totals['session_count']} sessions · {totals['subagent_spawn_count']} subagents", bar=real_cost_bar)}
        {_kpi("Billed Output", _num(tok['billed_output_tokens']), f"{totals['cache_hit_pct']}% cache hit")}
        {_kpi("Uncached Input", _num(tok['uncached_input_tokens']), f"{_num(tok['cached_input_tokens'])} cached")}
        {_kpi("Turns / Commands", str(totals['turn_count']), f"{totals['command_count']} cmds ({totals['failed_command_count']} failed)")}
      </div>
      <div class="card">
        <div class="card-header"><div>
          <div class="card-title">Cost &amp; Health by Skill</div>
          <div class="card-subtitle">Click a triggered skill to inspect it</div>
        </div><span class="badge badge-slate">{len(summary['skills'])} skills</span></div>
        <table><thead><tr><th>Skill</th><th class="num">Triggers</th>
          <th class="num">Explicit / Implicit</th><th class="num">Uncached In</th>
          <th class="num">Cached In</th><th class="num">Billed Out</th>
          <th class="num">Total Cost</th></tr></thead>
          <tbody>{_real_skill_rows(summary)}</tbody></table>
      </div>
      <div class="card">
        <div class="card-header"><div>
          <div class="card-title">Subagents (Online eval)</div>
          <div class="card-subtitle">Spawned by real project sessions</div>
        </div><span class="badge badge-slate">{totals['subagent_spawn_count']} spawns</span></div>
        <table><thead><tr><th>Agent Role</th><th class="num">Spawns</th>
          <th class="num">Sessions Used</th><th class="num">Cmds (fail)</th>
          <th class="num">Uncached In</th><th class="num">Billed Out</th>
          <th class="num">Est. Cost</th></tr></thead>
          <tbody>{_real_subagent_rows(summary)}</tbody></table>
      </div>
      <div class="card">
        <div class="card-header"><div>
          <div class="card-title">Session History</div>
          <div class="card-subtitle">Click a session to open its trace</div>
        </div><span class="badge badge-slate">{len(summary['sessions'])} sessions</span></div>
        <table><thead><tr><th>Started (UTC)</th><th>Thread</th><th>Model</th>
          <th class="num">Turns</th><th>Skills</th><th class="num">Subagents</th>
          <th class="num">Billed Out</th><th class="num">Est. Cost</th>
          <th>First Prompt</th></tr></thead>
          <tbody>{_real_session_rows(summary)}</tbody></table>
      </div>
    </div>
    {_real_skill_details(summary)}
    {_real_session_details(summary)}
  </div>

  <!-- ============================ MODE: EVAL ============================ -->
  <div id="mode-eval" class="hidden">
    <div id="eval-overview">
      {eval_banner}
      <div class="kpi-grid">
        {_kpi("Eval Est. Cost", eval_cost, f"{etotals['total_call_count']} total calls", bar=eval_cost_bar)}
        {_kpi("Worker Trials", str(etotals['worker_session_count']), f"{etotals['subagent_spawn_count']} subagents")}
        {_kpi("Semantic Judges", str(etotals['judge_call_count']), "grading calls")}
        {_kpi("Eval Billed Output", _num(etok['billed_output_tokens']), f"{etotals['cache_hit_pct']}% cache hit")}
      </div>
      <div class="card">
        <div class="card-header"><div>
          <div class="card-title">Benchmarked Skills</div>
          <div class="card-subtitle">Worker + judge cost per evaluated skill</div>
        </div><span class="badge badge-blue">{len(harness_evals['by_skill'])} skills</span></div>
        <table><thead><tr><th>Evaluated Skill</th><th class="num">Exps</th>
          <th>Latest Outcome</th>
          <th class="num">Trials</th><th class="num">Subagents</th>
          <th class="num">Judges</th><th class="num">Worker Out</th>
          <th class="num">Judge Out</th><th class="num">Worker Cost</th>
          <th class="num">Judge Cost</th><th class="num">Total Cost</th></tr></thead>
          <tbody>{_eval_skill_rows(harness_evals)}</tbody></table>
      </div>
      <div class="card">
        <div class="card-header"><div>
          <div class="card-title">Experiment Run History</div>
          <div class="card-subtitle">Click an experiment to drill into its trials</div>
        </div></div>
        <table><thead><tr><th>Skill</th><th>Experiment</th><th>Model</th>
          <th class="num">Trials</th><th class="num">Judges</th>
          <th>Outcome</th><th class="num">Billed Out</th><th class="num">Total Cost</th></tr></thead>
          <tbody>{_experiment_history_rows(harness_evals)}</tbody></table>
      </div>
    </div>
    {_eval_experiment_details(harness_evals)}
    {_eval_trial_details(harness_evals)}
  </div>

  <div class="note-box"><strong>Zero-token local telemetry.</strong>
    Parsed from <code>~/.codex/sessions/</code> and local
    <code>&lt;skill&gt;/runs/</code> traces. Online eval telemetry and Offline eval
    are tracked separately so benchmark runs never pollute real usage.
    {cost_note}</div>
</main>

<script>{_SCRIPT}</script>
</body>
</html>
"""


_STYLE = """
:root { color-scheme: light; --mono: ui-monospace, SFMono-Regular, Menlo, monospace;
  --primary:#2563eb; --indigo:#4f46e5; --emerald:#059669; --amber:#d97706;
  --rose:#e11d48; --text:#172033; --text-muted:#5b677d; --border:#dbe3ef;
  --bg:#f6f8fb; --card:#ffffff; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); line-height:1.5; }
.hidden { display:none !important; }
.mono { font-family:var(--mono); }
.topbar { background:#fff; border-bottom:1px solid var(--border); padding:14px 24px;
  display:flex; align-items:center; gap:20px; flex-wrap:wrap; }
.brand { display:flex; align-items:center; gap:10px; }
.brand-title { font-weight:700; font-size:15px; }
.repo-badge { font-family:var(--mono); font-size:11px; background:#eef4fb;
  border:1px solid var(--border); border-radius:6px; padding:2px 8px; color:var(--text-muted); }
.mode-tabs { display:flex; gap:6px; }
.mode-tab { background:transparent; border:1px solid transparent; border-radius:8px;
  padding:7px 14px; font-size:13px; font-weight:600; cursor:pointer; color:var(--text-muted);
  display:flex; align-items:center; gap:8px; }
.mode-tab.active { background:#eef4fb; color:var(--primary); border-color:#bfdbfe; }
.tab-pill { font-family:var(--mono); font-size:11px; font-weight:600; background:#fff;
  border:1px solid var(--border); border-radius:999px; padding:1px 7px; color:var(--text-muted); }
.updated { margin-left:auto; font-family:var(--mono); font-size:12px; color:var(--text-muted); }
.container { max-width:1180px; margin:0 auto; padding:24px; }
.root-meta { font-size:12px; color:var(--text-muted); margin-bottom:16px; }
.root-meta code { font-family:var(--mono); }
.latest-banner { display:flex; justify-content:space-between; align-items:center; gap:16px;
  background:linear-gradient(90deg,#eef4fb,#f6f8fb); border:1px solid #bfdbfe;
  border-radius:10px; padding:16px 18px; margin-bottom:18px; flex-wrap:wrap; }
.empty-banner { color:var(--text-muted); justify-content:center; }
.latest-left { display:flex; gap:12px; align-items:center; }
.pulse-dot { width:10px; height:10px; border-radius:50%; background:var(--emerald);
  box-shadow:0 0 0 4px rgba(5,150,105,0.15); flex-shrink:0; }
.latest-meta { display:flex; gap:8px; align-items:center; flex-wrap:wrap; font-size:13px;
  color:var(--text-muted); }
.latest-tag { font-weight:700; color:var(--primary); }
.latest-prompt { margin-top:4px; font-weight:500; color:var(--text); }
.kpi-grid { display:grid; grid-template-columns:repeat(4,minmax(160px,1fr)); gap:12px;
  margin-bottom:18px; }
@media (max-width:860px){ .kpi-grid{ grid-template-columns:repeat(2,1fr);} }
.kpi-card { background:#fff; border:1px solid var(--border); border-radius:10px; padding:14px; box-shadow:0 2px 6px rgba(25,45,80,.035); }
.kpi-label { font-size:11px; text-transform:uppercase; letter-spacing:0.04em;
  color:var(--text-muted); font-weight:600; }
.kpi-value { font-size:22px; font-weight:700; font-variant-numeric:tabular-nums; margin-top:6px; }
.kpi-sub { font-size:12px; color:var(--text-muted); margin-top:4px; }
.mini-bar { display:flex; height:6px; border-radius:3px; overflow:hidden; margin-top:8px;
  background:#e2e8f0; }
.mini-bar > div { height:100%; }
.card { background:var(--card); border:1px solid var(--border); border-radius:12px;
  padding:16px 18px; margin-bottom:18px; box-shadow:0 3px 12px rgba(25,45,80,.035); }
.card-header { display:flex; justify-content:space-between; align-items:flex-start;
  gap:12px; margin-bottom:10px; flex-wrap:wrap; }
.overview-usage { margin:0 0 18px; }.overview-usage-head { display:flex; justify-content:space-between; align-items:flex-end; gap:12px; }.overview-usage-head > strong { font-size:16px; font-variant-numeric:tabular-nums; }.usage-legend { display:flex; flex-wrap:wrap; gap:10px 18px; margin-top:9px; color:var(--text-muted); font-size:11px; }.usage-legend span { display:inline-flex; align-items:center; gap:5px; }.usage-legend i { width:9px; height:9px; border-radius:2px; display:inline-block; }.usage-legend .usage-total { margin-left:auto; color:var(--text); font-weight:700; }
.card-title { font-weight:700; font-size:15px; }
.card-subtitle { font-size:12px; color:var(--text-muted); margin-top:2px; }
.card-head-actions { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.btn-link { font-size:12px; font-weight:600; color:var(--primary); text-decoration:none;
  border:1px solid #bfdbfe; border-radius:6px; padding:4px 8px; background:#eef4fb; }
.btn-link:hover { background:#dbeafe; }
table { width:100%; border-collapse:collapse; font-size:12px; }
th,td { border-bottom:1px solid #eef1f6; padding:8px 10px; text-align:left; vertical-align:top; }
th { font-size:10px; text-transform:uppercase; letter-spacing:0.04em; color:var(--text-muted);
  font-weight:600; }
th.num,td.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
tr:last-child td { border-bottom:none; }
.clickable-row { cursor:pointer; }
.clickable-row:hover { background:#f6f9fe; }
.row-title { font-weight:700; color:var(--text); margin-bottom:2px; }
.exp-badges { display:flex; gap:4px; flex-wrap:wrap; margin-top:3px; }
.row-meta { display:inline-flex; align-items:center; gap:5px; color:var(--text-muted); font-size:10px; margin:2px 8px 0 0; }
.status-dot { width:8px; height:8px; display:inline-block; flex:0 0 8px; border-radius:50%; background:#94a3b8; }
.status-dot.green { background:#059669; }.status-dot.blue { background:#2563eb; }.status-dot.violet { background:#7c3aed; }.status-dot.rose { background:#e11d48; }.status-dot.slate { background:#94a3b8; }
.quiet-status { color:#a16207; font-size:10px; margin-left:5px; }
.outcome-score { display:flex; align-items:center; gap:6px; font-variant-numeric:tabular-nums; }.outcome-score b { font-size:13px; }.outcome-score small { color:var(--text-muted); font-size:9px; text-transform:uppercase; letter-spacing:.06em; }
.outcome-inline { display:inline-flex; align-items:center; gap:6px; font-size:10px; font-weight:700; }
.count-value { display:inline-grid; place-items:center; min-width:26px; height:26px; border-radius:7px; background:#eef2f8; color:#334155; font-weight:700; }
.table-cost { display:flex; flex-direction:column; align-items:flex-end; gap:4px; min-width:84px; }.table-cost strong { font-variant-numeric:tabular-nums; }.table-cost > span { display:block; width:72px; height:5px; overflow:hidden; border-radius:4px; background:#e8edf5; }.table-cost i { display:block; height:100%; border-radius:4px; background:#2563eb; }.table-cost .meter-violet { background:#7c3aed; }.table-cost .meter-amber { background:#d97706; }
code { font-family:var(--mono); font-size:11px; background:#eef4fb; border:1px solid var(--border);
  border-radius:4px; padding:1px 5px; color:#1e293b; }
.badge { display:inline-block; padding:2px 7px; border-radius:4px; font-size:10px;
  font-weight:600; border:1px solid transparent; }
.badge-green { background:#dcfce7; color:#166534; border-color:#bbf7d0; }
.badge-blue { background:#dbeafe; color:#1e40af; border-color:#bfdbfe; }
.badge-amber { background:#fef3c7; color:#92400e; border-color:#fde68a; }
.badge-slate { background:#e2e8f0; color:#334155; border-color:#cbd5e1; }
.muted { color:var(--text-muted); }
.prompt-cell { max-width:300px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  color:var(--text-muted); }
.empty { text-align:center; color:var(--text-muted); padding:18px; }
.btn { border:none; border-radius:8px; padding:8px 14px; font-size:13px; font-weight:600;
  cursor:pointer; }
.btn-primary { background:var(--primary); color:#fff; }
.crumb-link { background:none; border:none; color:var(--primary); font-size:13px; font-weight:600;
  cursor:pointer; padding:0 0 12px; }
.detail-view h4 { font-size:11px; text-transform:uppercase; letter-spacing:0.04em;
  color:var(--text-muted); margin:18px 0 8px; }
.trace-block { background:#f8fafc; border:1px solid var(--border); border-radius:8px;
  padding:12px; font-size:13px; white-space:pre-wrap; }
.trace-timeline { display:flex; flex-direction:column; gap:0; }
.trace-step { display:flex; gap:12px; padding-bottom:16px; position:relative; }
.trace-step:not(:last-child)::before { content:""; position:absolute; left:4px; top:12px;
  bottom:0; width:2px; background:var(--border); }
.trace-dot { width:10px; height:10px; border-radius:50%; background:var(--primary);
  margin-top:3px; flex-shrink:0; z-index:1; }
.trace-step-title { font-weight:700; font-size:13px; }
.trace-step-body { font-size:12px; color:var(--text-muted); margin-top:2px; }
.trace-code { font-family:var(--mono); font-size:11px; background:#f1f5f9; border:1px solid var(--border);
  border-radius:4px; padding:4px 8px; margin-top:4px; white-space:pre-wrap; word-break:break-all;
  color:#1e293b; }
.trace-time { font-family:var(--mono); font-size:11px; color:var(--text-muted); min-width:48px;
  flex-shrink:0; padding-top:1px; }
.grade-grid { display:grid; grid-template-columns:repeat(2,1fr); gap:12px; }
@media (max-width:860px){ .grade-grid{ grid-template-columns:1fr; } }
.grade-panel { background:#f8fafc; border:1px solid var(--border); border-radius:8px; padding:12px; }
.grade-panel-head { display:flex; justify-content:space-between; align-items:center;
  font-weight:700; font-size:13px; margin-bottom:8px; }
.grade-score { font-family:var(--mono); font-weight:600; font-size:12px; }
.grade-meta { margin:0 0 8px; font-size:11px; }
.notes-cell { font-size:11px; color:var(--text-muted); }
.judge-block { background:#f8fafc; border:1px solid var(--border); border-radius:8px;
  padding:12px; margin-bottom:10px; }
.judge-head { font-weight:700; font-size:13px; margin-bottom:8px; display:flex; gap:8px;
  align-items:center; }
.criterion-card { background:#fff; border:1px solid var(--border); border-radius:6px;
  padding:10px 12px; margin-bottom:8px; }
.criterion-head { display:flex; gap:8px; align-items:center; font-size:12px; margin-bottom:4px; }
.criterion-notes { font-size:12px; color:var(--text-muted); line-height:1.5; }
.note-box { font-size:12px; color:var(--text-muted); background:#eef4fb;
  border:1px solid var(--border); border-radius:8px; padding:12px 14px; }
.cost-total { font-size:15px; font-weight:800; font-variant-numeric:tabular-nums; }
.detail-tabs { display:flex; gap:5px; border-bottom:1px solid var(--border); margin:0 0 14px; }
.detail-tab { border:0; border-bottom:3px solid transparent; padding:9px 13px; background:transparent;
  color:var(--text-muted); font-weight:700; cursor:pointer; }
.detail-tab.active { color:var(--primary); border-color:var(--primary); }
.detail-tab-panel { min-width:0; }

/* Prototype AA: vertical Gantt with a linked event stream. */
.u-card,.z-card { background:#fff; border:1px solid var(--border); border-radius:12px; padding:15px 17px; margin:12px 0; }
.u-card-head { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:12px; }
.u-card-head b { display:block; font-size:14px; }
.u-card-head small { display:block; color:var(--text-muted); margin-top:3px; }
.u-hint { color:var(--text-muted); font:11px var(--mono); background:#eef2f8; border:1px solid var(--border); padding:4px 9px; border-radius:6px; }
.u-stage { display:flex; gap:0; align-items:stretch; --axis-w:48px; --col-min:140px; }
.u-stage.solo .u-heads .u-col-head { flex:0 0 150px; }.u-stage.solo .u-lanes { justify-content:flex-start; }.u-stage.solo .u-lane { flex:0 0 150px; }.u-stage.solo .u-lane .u-block { left:calc(50% - 55px); right:auto; width:110px; }
.u-stage.open { gap:16px; }
.u-gantt { flex:1 1 100%; min-width:0; transition:flex-basis .32s cubic-bezier(.2,.7,.3,1); }
.u-stage.open .u-gantt { flex:0 0 42%; }
.u-heads { display:flex; gap:10px; margin-left:var(--axis-w); margin-bottom:8px; height:67px; box-sizing:border-box; }
.u-col-head { flex:1 0 var(--col-min); min-width:0; text-align:center; }
.u-col-icon { display:inline-grid; place-items:center; width:26px; height:26px; border-radius:7px; background:color-mix(in srgb,var(--agent) 14%,#fff); border:1px solid color-mix(in srgb,var(--agent) 40%,transparent); margin-bottom:3px; color:var(--agent); }
.u-col-name { font-size:12px; font-weight:800; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.u-col-role,.lane-reuse { color:var(--text-muted); font:10px var(--mono); }
.u-plot { display:flex; position:relative; height:var(--canvas-h); min-width:0; }
.u-axis { position:relative; flex:0 0 var(--axis-w); color:var(--text-muted); font:10px var(--mono); }
.u-tick { position:absolute; right:8px; transform:translateY(-50%); }
.u-lanes { position:relative; display:flex; flex:1; gap:9px; min-width:0; overflow-x:auto; }
.u-grid { position:absolute; left:0; right:0; border-top:1px solid #e9edf4; z-index:0; }
.u-lane { position:relative; flex:1 0 var(--col-min); min-width:var(--col-min); border:1px solid var(--border); border-radius:8px; background:#f8fafd; }
.u-life { position:absolute; left:50%; width:3px; transform:translateX(-50%); opacity:.3; border-radius:3px; background:var(--agent); }
.u-block { position:absolute; box-sizing:border-box; min-height:0; z-index:1; left:5px; right:5px; border-radius:7px; border:1px solid color-mix(in srgb,var(--agent) 55%,#000); background:linear-gradient(180deg,var(--agent),color-mix(in srgb,var(--agent) 82%,#000)); color:#fff; box-shadow:0 2px 5px -2px color-mix(in srgb,var(--agent) 45%,transparent); cursor:pointer; text-align:left; overflow:hidden; padding:0; font:inherit; appearance:none; transition:transform .1s,box-shadow .12s; }
.u-block:hover { filter:brightness(1.05); }
.u-block { display:flex; flex-direction:column; justify-content:flex-start; align-items:stretch; }
.u-block:focus-visible { outline:2px solid #fff; outline-offset:-4px; }
.u-block > span { display:flex; align-items:center; gap:5px; flex-shrink:0; padding:6px 8px 2px; font-size:11px; font-weight:800; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.u-bar-label i { font-style:normal; font-size:10px; opacity:.85; flex:none; }.u-bar-label > span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.u-block > small { display:block; flex-shrink:0; padding:0 8px 6px; font:9px/1.3 var(--mono); color:rgba(255,255,255,.85); }
.u-block.active { z-index:2; box-shadow:inset 0 0 0 2px #fff,inset 0 0 0 4px var(--agent); filter:brightness(1.05); }
.u-block.wait { background:repeating-linear-gradient(45deg,#e7edf6,#e7edf6 7px,#dae2ee 7px,#dae2ee 14px); border-color:var(--border); box-shadow:none; color:#55657f; cursor:default; }
.u-block.wait > span { color:#55657f; font-weight:700; }.u-block.wait > small { color:#55657f; }
.u-block.missing { background:#e2e8f0; border:1px dashed #94a3b8; color:#64748b; box-shadow:none; cursor:default; }
.c-main,.a-main { --agent:#2563eb; } .c-a,.a-a { --agent:#7c3aed; } .c-b,.a-b { --agent:#db2777; }
.c-c3,.a-c3 { --agent:#0d9488; } .c-c4,.a-c4 { --agent:#ea580c; }
.u-trace { flex:0 0 0; min-width:0; opacity:0; overflow:hidden; padding:0; transition:flex-basis .32s cubic-bezier(.2,.7,.3,1),opacity .25s; background:#fff; border:1px solid var(--border); border-radius:12px; }
.u-stage.open .u-trace { flex:0 0 calc(58% - 16px); opacity:1; padding:14px 16px; }
.u-trace-agent { position:relative; display:none; }
.u-stage.open .u-trace-agent:not(.hidden) { display:block; }
.u-trace-head { display:flex; justify-content:space-between; align-items:center; gap:9px; margin-bottom:10px; }
.u-avatar { width:30px; height:30px; display:grid; place-items:center; border-radius:8px; flex:none; color:var(--agent); background:color-mix(in srgb,var(--agent) 14%,#fff); border:1px solid color-mix(in srgb,var(--agent) 40%,transparent); }.u-agent-title { flex:1; min-width:0; }.u-agent-title b { font-size:15px; font-weight:800; }
.u-trace-head b,.u-trace-head small { display:block; }.u-trace-head small { color:var(--text-muted); font-size:11px; }
.u-close { border:1px solid var(--border); background:#eef2f8; border-radius:7px; width:30px; height:30px; cursor:pointer; font-size:18px; }
.u-prompt { background:#eef2f8; border:1px solid var(--border); border-left:3px solid var(--agent); border-radius:0 8px 8px 0; padding:7px 11px; font-size:12px; margin-bottom:10px; white-space:pre-wrap; overflow-wrap:anywhere; }
.u-event-meta { font-size:11px; color:var(--text-muted); margin:2px 0 10px; }
.u-event-list { max-height:var(--canvas-h); overflow:auto; border-top:1px solid var(--border); scrollbar-gutter:stable; }
.u-event { display:grid; grid-template-columns:64px minmax(0,1fr); gap:12px; padding:12px 10px; border-bottom:1px solid #eef1f6; border-left:3px solid transparent; }
.u-event[data-position] { cursor:pointer; }
.u-event:hover { background:#f8fafd; }
.u-event.selected { background:#eef4fb; border-left-color:var(--agent); }
.u-event:focus-visible { outline:2px solid var(--agent); outline-offset:-2px; }
.u-event-time { font:11px var(--mono); color:var(--text-muted); padding-top:2px; font-variant-numeric:tabular-nums; }
.u-event-content { min-width:0; }
.u-event-head { display:flex; align-items:center; gap:8px; flex-wrap:wrap; font:800 10px var(--mono); text-transform:uppercase; letter-spacing:.04em; color:var(--agent); }
.u-event-text { font-size:13px; line-height:1.5; margin-top:5px; white-space:pre-wrap; overflow-wrap:anywhere; }
.u-event.kind-reason .u-event-text { color:#7c3aed; font-style:italic; }
.u-event.kind-ret .u-event-content { background:#f0fdf7; border-radius:7px; padding:8px; }
.u-terminal { font:11.5px/1.5 var(--mono); background:#0d1526; color:#dbe8ff; border:1px solid #24314e; border-radius:5px; padding:7px 9px; margin:7px 0 0; white-space:pre-wrap; overflow-wrap:anywhere; }
.u-captured { font:11px/1.5 var(--mono); background:#f6f8fc; color:var(--text-muted); border:1px solid var(--border); border-left:3px solid var(--border); border-radius:0 5px 5px 0; padding:7px 9px; margin:5px 0 0; white-space:pre-wrap; overflow-wrap:anywhere; }
.u-exit { color:#059669; text-transform:none; }.u-exit.failed { color:#e11d48; }
.u-time-cursor { position:absolute; left:var(--axis-w); right:0; height:0; border-top:2px dashed #d97706; z-index:4; pointer-events:none; }
.u-time-cursor span { position:absolute; right:0; top:-19px; background:#fff7ed; color:#9a3412; border:1px solid #fed7aa; border-radius:4px; padding:1px 5px; font:10px var(--mono); }
.u-foot { font-size:11px; color:var(--text-muted); margin-top:10px; }
.u-stage.open .u-block > small { display:none; }

/* Prototype Z: sticky context tank and visual inventory cards. */
.z-inventory > h3 { margin:0 0 12px; font-size:14px; font-weight:800; }
.z-tabs { display:flex; gap:20px; flex-wrap:wrap; border-bottom:1px solid var(--border); margin:0 0 22px; }
.z-tab { display:flex; align-items:center; gap:7px; border:0; border-bottom:2px solid transparent; background:none; border-radius:0; padding:8px 2px; margin-bottom:-1px; color:#8a97ab; font-size:13px; font-weight:700; cursor:pointer; }
.z-tab::before { content:""; width:9px; height:9px; border-radius:3px; background:var(--agent); }
.z-tab.active { color:var(--text); border-bottom-color:var(--agent); }
.z-layout { display:grid; grid-template-columns:210px minmax(0,1fr); gap:28px; align-items:start; }
.z-tank-column { position:sticky; top:16px; height:calc(100vh - 120px); min-height:440px; display:flex; flex-direction:column; align-items:center; background:#fff; border:1px solid var(--border); border-radius:14px; padding:16px 14px; text-align:center; }
.z-tank-column h4 { margin:0 0 10px; font-size:11px; text-transform:uppercase; letter-spacing:.07em; color:#8a97ab; font-weight:800; }
.z-tank { position:relative; display:flex; flex-direction:column-reverse; flex:1; width:138px; min-height:240px; overflow:hidden; border:2px solid var(--border); border-radius:14px; background:#eef2f8; box-shadow:inset 0 0 0 1px #fff,0 8px 20px -14px rgba(20,30,60,.35); }
.z-seg { width:100%; flex-shrink:0; border-top:1px solid rgba(255,255,255,.75); }.fill-input { background:#2563eb; }.fill-base { background:#64748b; }.fill-skills { background:#7c3aed; }.fill-environment { background:#0d9488; }.fill-task { background:#2563eb; }.fill-file { background:#0ea5e9; }.fill-output { background:#ea580c; }.fill-unknown { background:#cbd5e1; }
.z-water { position:absolute; left:-3px; right:-3px; border-top:2px dashed #b45309; z-index:3; }.z-fill { margin-top:10px; font-size:24px; font-weight:800; }.z-tank-column > small { color:var(--text-muted); text-align:center; margin-top:3px; }
.z-sections { min-width:0; }.z-sections > .z-card { padding:16px 18px; border-radius:14px; margin:0 0 16px; }.z-card h4 { margin:0 0 11px; font-size:11px; text-transform:uppercase; letter-spacing:.07em; font-weight:800; color:#8a97ab; }.z-break-row { display:flex; align-items:center; gap:10px; padding:5px 0; font-size:13px; }.z-break-row b { min-width:0; overflow-wrap:anywhere; }.z-break-row span { margin-left:auto; color:var(--text-muted); text-align:right; font:11px var(--mono); }
.z-swatch { width:11px; height:11px; border-radius:3px; flex:none; background:#94a3b8; }.z-swatch.src-skills { background:#7c3aed; }.z-swatch.src-environment { background:#0d9488; }.z-swatch.src-task { background:#2563eb; }.z-swatch.src-file { background:#0ea5e9; }.z-swatch.src-output { background:#ea580c; }
.z-tiles { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; }.z-tiles > div { background:#eef2f8; border:1px solid var(--border); border-radius:9px; padding:10px; text-align:center; min-width:0; }.z-tiles b,.z-tiles small { display:block; overflow-wrap:anywhere; }.z-tiles b { font-size:17px; font-weight:800; }.z-tiles small { color:var(--text-muted); font-size:10px; margin-top:3px; }
.z-sandbox { display:flex; gap:16px; align-items:stretch; }
.z-fsmap { flex:1; min-width:0; border:1px solid var(--border); border-radius:11px; padding:12px 14px; background:#eef2f8; }
.z-fs-row { display:flex; align-items:center; gap:9px; position:relative; padding:5px 0 5px calc(var(--depth) * 16px); font-size:12px; min-width:0; }
.z-fs-row:not([style='--depth:0'])::before { content:""; position:absolute; width:9px; height:13px; left:calc(var(--depth) * 16px - 13px); top:0; border-left:1px solid #cbd5e1; border-bottom:1px solid #cbd5e1; }
.z-folder { display:block; width:17px; height:14px; border-radius:2px; background:#cbd5e1; position:relative; flex:none; }
.z-folder::before { content:""; position:absolute; top:-3px; left:1px; width:7px; height:3px; border-radius:2px 2px 0 0; background:inherit; }
.z-folder.rw { background:#86d9b4; border:1px solid #0a7d53; }.z-folder.unknown { background:#e2e8f0; border:1px dashed #94a3b8; }
.z-fs-path { font-family:var(--mono); font-weight:700; overflow-wrap:anywhere; min-width:0; }
.z-access { margin-left:auto; flex:none; font-size:9px; font-weight:800; letter-spacing:.03em; padding:1px 7px; border-radius:5px; background:#e2e8f0; color:#55657f; }
.z-access.ro { background:#e7edf8; color:#3457a8; }.z-access.rw { background:#dcf2e8; color:#0a7d53; }
.z-network { width:106px; flex:none; border:1px solid var(--border); border-radius:11px; padding:12px; background:#eef2f8; text-align:center; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:8px; font-size:11px; color:#2563eb; }
.z-globe { width:40px; height:40px; border-radius:50%; border:2.5px solid currentColor; position:relative; }
.z-globe::before,.z-globe::after { content:""; position:absolute; background:currentColor; }
.z-globe::before { left:50%; top:0; bottom:0; width:2px; transform:translateX(-50%); }.z-globe::after { top:50%; left:0; right:0; height:2px; transform:translateY(-50%); }
.z-network.off { color:#b45309; }.z-network.unknown { color:#8a97ab; }
.z-slash { position:absolute; left:-6px; right:-6px; top:50%; height:3px; background:#dc2626; transform:translateY(-50%) rotate(-45deg); border-radius:2px; z-index:1; }
.z-policy-note { color:var(--text-muted); font-size:11px; margin:9px 0 0; }.z-policy { margin-top:8px; font-size:11px; color:var(--text-muted); }.z-policy summary { cursor:pointer; }.z-policy pre { max-height:280px; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; background:#f6f8fc; padding:10px; border:1px solid var(--border); border-radius:7px; }
.z-files,.z-tools { display:flex; flex-wrap:wrap; gap:12px; }.z-file { width:110px; text-align:center; border:1px solid var(--border); border-radius:10px; background:#eef2f8; padding:12px 10px 10px; overflow-wrap:anywhere; }.z-file b { display:block; font:700 11.5px var(--mono); }.z-file small { display:block; color:var(--text-muted); font:10px var(--mono); margin-top:3px; }
.z-doc { width:40px; height:50px; margin:0 auto 8px; position:relative; background:#f0f9ff; border:2px solid #0ea5e9; border-radius:4px; }
.z-doc::before { content:""; position:absolute; top:-1px; right:-1px; border-width:0 12px 12px 0; border-style:solid; border-color:#fff #fff #0ea5e9 #0ea5e9; border-radius:0 0 0 3px; }
.z-doc-lines { position:absolute; left:6px; right:6px; top:16px; height:2px; background:#9bd7f2; box-shadow:0 5px 0 #9bd7f2,0 10px 0 #9bd7f2,0 15px 0 #9bd7f2; }
.z-file.miss .z-doc { border-color:#dc2626; border-style:dashed; background:#fef2f2; }.z-file.miss .z-doc::before { display:none; }.z-doc-x { position:absolute; inset:0; display:grid; place-items:center; color:#dc2626; font-size:20px; font-weight:800; }
.z-tool { display:flex; align-items:center; gap:10px; padding:4px 0; width:100%; font-size:13px; }.z-tool b { width:70px; font-family:var(--mono); }.z-tool span { display:flex; gap:3px; }.z-tool i { width:13px; height:18px; border-radius:3px; background:#2563eb; }.z-tool small { color:var(--text-muted); font-family:var(--mono); }.z-cache { display:flex; height:15px; border:1px solid var(--border); border-radius:5px; overflow:hidden; }.z-cache i { background:#94a3b8; }.z-cache b { background:#2563eb; }.z-cache-label { margin-top:5px; font-size:10px; color:var(--text-muted); }.z-note { color:var(--text-muted); font-size:10px; margin:8px 0 0; }.z-base summary { cursor:pointer; font-weight:700; }.z-base pre { max-height:360px; overflow:auto; white-space:pre-wrap; background:#0d1526; color:#cfe0ff; border-radius:7px; padding:10px; font:10px var(--mono); }
@media(max-width:760px){.z-layout{grid-template-columns:1fr}.z-tank-column{position:relative;min-height:0;height:auto}.z-tank{height:420px;flex:none}}
@media(max-width:600px){.z-tiles{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:900px){.u-stage.open{display:block}.u-stage.open .u-gantt{flex:auto}.u-stage.open .u-trace{min-height:440px;margin-top:12px}.u-heads{overflow-x:auto}}
@media(max-width:560px){.z-sandbox{flex-direction:column}.z-network{width:100%;flex-direction:row;gap:14px}.z-fsmap{padding:10px}.z-access{font-size:8px;padding:1px 4px}.u-card-head{align-items:flex-start;flex-direction:column}}
"""


_SCRIPT = """
function switchMode(mode) {
  document.getElementById('tab-real').classList.toggle('active', mode === 'real');
  document.getElementById('tab-eval').classList.toggle('active', mode === 'eval');
  document.getElementById('mode-real').classList.toggle('hidden', mode !== 'real');
  document.getElementById('mode-eval').classList.toggle('hidden', mode !== 'eval');
  if (mode === 'real') backToRealOverview(); else backToEvalOverview();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
function _hideAll(selector) {
  document.querySelectorAll(selector).forEach(function (el) { el.classList.add('hidden'); });
}
function backToRealOverview() {
  _hideAll('#mode-real .detail-view');
  document.getElementById('real-overview').classList.remove('hidden');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
function openSkillDetail(id) {
  document.getElementById('real-overview').classList.add('hidden');
  _hideAll('#mode-real .detail-view');
  var el = document.getElementById(id);
  if (el) el.classList.remove('hidden');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
function openSessionDetail(id) {
  document.getElementById('real-overview').classList.add('hidden');
  _hideAll('#mode-real .detail-view');
  var el = document.getElementById(id);
  if (el) el.classList.remove('hidden');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
function backToEvalOverview() {
  _hideAll('#mode-eval .detail-view');
  document.getElementById('eval-overview').classList.remove('hidden');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
function openEvalExperiment(id) {
  document.getElementById('eval-overview').classList.add('hidden');
  _hideAll('#mode-eval .detail-view');
  var el = document.getElementById(id);
  if (el) el.classList.remove('hidden');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
function openEvalTrial(id) {
  document.getElementById('eval-overview').classList.add('hidden');
  _hideAll('#mode-eval .detail-view');
  var el = document.getElementById(id);
  if (el) el.classList.remove('hidden');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
function selectDetailTab(prefix, key) {
  var root = document.getElementById(prefix);
  if (!root) return;
  root.querySelectorAll('.detail-tab').forEach(function (tab) {
    tab.classList.toggle('active', tab.getAttribute('onclick').indexOf("'" + key + "'") >= 0);
  });
  root.querySelectorAll('.detail-tab-panel').forEach(function (panel) {
    panel.classList.toggle('hidden', panel.id !== prefix + '-panel-' + key);
  });
}
function selectTimelineEvent(row) {
  var stage = row.closest('.u-stage');
  if (!stage) return;
  var position = Number(row.dataset.position);
  if (!Number.isFinite(position)) return;
  var cursor = stage.querySelector('.u-time-cursor');
  cursor.style.top = Math.min(99.7, Math.max(0, position)) + '%';
  cursor.querySelector('span').textContent = '+' + row.dataset.time + 's';
  cursor.hidden = false;
  stage.querySelectorAll('.u-event').forEach(function (item) {
    item.classList.toggle('selected', item === row);
  });
}
function openTimelineActor(id) {
  var panel = document.getElementById(id);
  if (!panel) return;
  var stage = panel.closest('.u-stage');
  if (!stage) return;
  if (stage.dataset.activeActor === id) {
    closeTimeline(stage);
    return;
  }
  stage.querySelector('.u-time-cursor').hidden = true;
  stage.querySelectorAll('.u-event.selected').forEach(function (row) { row.classList.remove('selected'); });
  stage.dataset.activeActor = id;
  stage.classList.add('open');
  stage.querySelectorAll('.u-trace-agent').forEach(function (item) {
    item.classList.toggle('hidden', item.id !== id);
  });
  stage.querySelectorAll('.u-block').forEach(function (bar) {
    var selected = bar.dataset.actor === id;
    bar.classList.toggle('active', selected);
    bar.setAttribute('aria-pressed', String(selected));
  });
  var hint = stage.parentElement.querySelector('.u-hint');
  if (hint) hint.textContent = 'selected actor · close to return to overview';
}
function closeTimeline(stage) {
  if (typeof stage === 'string') stage = document.querySelector('#' + stage);
  if (!stage) stage = document.querySelector('.u-stage.open');
  if (!stage) return;
  stage.dataset.activeActor = '';
  stage.querySelector('.u-time-cursor').hidden = true;
  stage.classList.remove('open');
  stage.querySelectorAll('.u-block').forEach(function (bar) { bar.classList.remove('active'); bar.setAttribute('aria-pressed', 'false'); });
  var hint = stage.parentElement.querySelector('.u-hint');
  if (hint) hint.textContent = 'overview · full width';
}
function selectContextAgent(prefix, id) {
  var view = document.getElementById(id);
  if (!view) return;
  var card = view.closest('.z-inventory');
  if (!card) return;
  card.querySelectorAll('.z-agent').forEach(function (agent) {
    agent.classList.toggle('hidden', agent.id !== id);
  });
  card.querySelectorAll('.z-tab').forEach(function (tab) {
    var selected = tab.dataset.context === id;
    tab.classList.toggle('active', selected);
    tab.setAttribute('aria-pressed', String(selected));
  });
}
"""
