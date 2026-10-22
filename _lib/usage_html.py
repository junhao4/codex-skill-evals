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
from collections import defaultdict
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


# --------------------------------------------------------------------------- #
# Real Project Work                                                           #
# --------------------------------------------------------------------------- #

def _real_skill_rows(summary: dict[str, Any]) -> str:
    rows: list[str] = []
    for sk in summary["skills"]:
        tok = sk["tokens"]
        triggered = sk["sessions_triggered"]
        cost = (
            _cost(sk["est_cost"])
            if sk["priced_count"] > 0
            else ("$0.0000" if triggered == 0 else "n/a")
        )
        status = "Installed" if sk["installed"] else "External"
        status_cls = "badge-green" if sk["installed"] else "badge-slate"
        unused = (
            " <span class='badge badge-amber'>0 triggers</span>"
            if sk["installed"] and triggered == 0
            else ""
        )
        clickable = "clickable-row" if triggered > 0 else ""
        onclick = (
            f"onclick=\"openSkillDetail('{_esc(sk['skill'])}')\""
            if triggered > 0
            else ""
        )
        rows.append(
            f"<tr class='{clickable}' {onclick}>"
            f"<td><div class='row-title'>{_esc(sk['skill'])}</div>"
            f"<span class='badge {status_cls}'>{status}</span>{unused}</td>"
            f"<td class='num'>{triggered}</td>"
            f"<td class='num'>{sk['explicit_count']} / {sk['implicit_count']}</td>"
            f"<td class='num'>{_num(tok['uncached_input_tokens'])}</td>"
            f"<td class='num'>{_num(tok['cached_input_tokens'])}</td>"
            f"<td class='num'>{_num(tok['billed_output_tokens'])}</td>"
            f"<td class='num'><strong>{_esc(cost)}</strong></td>"
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
    for ag in summary["subagents"]:
        tok = ag["tokens"]
        cost = (
            _cost(ag["est_cost"])
            if ag["priced_count"] > 0
            else ("$0.0000" if ag["spawns"] == 0 else "n/a")
        )
        src = (
            "<span class='badge badge-blue'>Configured</span>"
            if ag["configured"]
            else "<span class='badge badge-slate'>Built-in / ad-hoc</span>"
        )
        rows.append(
            f"<tr>"
            f"<td><div class='row-title'>{_esc(ag['role'])}</div>{src}</td>"
            f"<td class='num'>{ag['spawns']}</td>"
            f"<td class='num'>{ag['sessions_used']}</td>"
            f"<td class='num'>{ag['command_count']} ({ag['failed_command_count']})</td>"
            f"<td class='num'>{_num(tok['uncached_input_tokens'])}</td>"
            f"<td class='num'>{_num(tok['billed_output_tokens'])}</td>"
            f"<td class='num'>{_esc(cost)}</td>"
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
            " ".join(f"<span class='badge badge-blue'>{_esc(s)}</span>" for s in skills)
            if skills
            else "<span class='muted'>—</span>"
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


def _real_session_details(summary: dict[str, Any]) -> str:
    views: list[str] = []
    for idx, sess in enumerate(summary["sessions"]):
        tok = sess["token_usage"]
        detail_id = f"real-sess-{idx}"
        sub_rows: list[str] = []
        for sub in sess.get("subagents", []):
            stok = sub["token_usage"]
            sub_model = sub.get("model") or "—"
            if sub.get("reasoning_effort"):
                sub_model = f"{sub_model} ({sub['reasoning_effort']})"
            sub_rows.append(
                f"<tr><td><code>depth {sub.get('depth', 1)}</code></td>"
                f"<td><code>{_esc(sub.get('agent_role') or 'default')}</code></td>"
                f"<td><code>{_esc(str(sub['thread_id'])[:12])}</code></td>"
                f"<td><code>{_esc(sub_model)}</code></td>"
                f"<td class='num'>{sub['command_count']} ({sub['failed_command_count']})</td>"
                f"<td class='num'>{_num(stok['billed_output_tokens'])}</td>"
                f"<td class='num'>{_esc(_cost(sub.get('est_cost')))}</td></tr>"
            )
        sub_table = (
            "<table><thead><tr><th>Depth</th><th>Role</th><th>Thread</th>"
            "<th>Model</th><th class='num'>Cmds (fail)</th>"
            "<th class='num'>Billed out</th><th class='num'>Est. cost</th></tr></thead>"
            f"<tbody>{''.join(sub_rows)}</tbody></table>"
            if sub_rows
            else "<p class='muted'>No subagents spawned in this session.</p>"
        )
        skills = ", ".join(sess["skills_used"]) or "—"
        turns_sub = f"{sess['command_count']} cmds"
        views.append(
            f"<div id='{detail_id}' class='detail-view hidden'>"
            f"<button class='crumb-link' onclick='backToRealOverview()'>← Real Project Work</button>"
            f"<div class='card'><div class='card-header'><div>"
            f"<div class='card-title'>Session <code>{_esc(str(sess['thread_id'])[:16])}</code></div>"
            f"<div class='card-subtitle'>{_esc(_ts(sess['started_at']))} · "
            f"{_esc(sess['model'] or 'unknown')} · skills: {_esc(skills)}</div></div>"
            f"<span class='badge badge-slate'>{_esc(_cost(sess['est_cost']))}</span></div>"
            f"<div class='kpi-grid'>"
            f"{_kpi('Billed output', _num(tok['billed_output_tokens']), 'tokens')}"
            f"{_kpi('Uncached input', _num(tok['uncached_input_tokens']), 'tokens')}"
            f"{_kpi('Cached input', _num(tok['cached_input_tokens']), 'tokens')}"
            f"{_kpi('Turns', str(sess['turn_count']), turns_sub)}"
            f"</div>"
            f"<h4>First prompt</h4><div class='trace-block'>{_esc(sess['first_prompt'] or '—')}</div>"
            f"<h4>Subagents ({sess['subagent_spawn_count']})</h4>{sub_table}"
            f"</div></div>"
        )
    return "".join(views)


def _real_skill_details(summary: dict[str, Any]) -> str:
    """A single reusable skill-detail shell populated client-side by name."""
    return (
        "<div id='real-skill-detail' class='detail-view hidden'>"
        "<button class='crumb-link' onclick='backToRealOverview()'>← Real Project Work</button>"
        "<div class='card'><div class='card-header'><div>"
        "<div class='card-title'>Skill: <span id='skill-detail-name'>—</span></div>"
        "<div class='card-subtitle'>Sessions that triggered this skill (see "
        "Session History below for full traces)</div></div></div>"
        "<p class='muted'>Use the Session History table on the overview to open "
        "any individual session's trace. Per-skill token and cost totals are in "
        "the Cost &amp; Health by Skill table.</p></div></div>"
    )


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
    for es in harness_evals["by_skill"]:
        wtok, jtok = es["worker_tokens"], es["judge_tokens"]
        exps = " ".join(
            f"<span class='badge badge-slate'>{_esc(e)}</span>" for e in es["experiments"]
        )
        rows.append(
            f"<tr>"
            f"<td><div class='row-title'>{_esc(es['skill'])}</div>"
            f"<div class='exp-badges'>{exps or '—'}</div></td>"
            f"<td class='num'>{es['experiment_count']}</td>"
            f"<td class='num'>{es['worker_trials']} "
            f"<span class='muted'>({es['with_skill_trials']}w/{es['baseline_trials']}b)</span></td>"
            f"<td class='num'>{es['subagent_spawns']}</td>"
            f"<td class='num'>{es['judge_calls']}</td>"
            f"<td class='num'>{_num(wtok['billed_output_tokens'])}</td>"
            f"<td class='num'>{_num(jtok['billed_output_tokens'])}</td>"
            f"<td class='num'>{_esc(_cost(es['worker_est_cost']))}</td>"
            f"<td class='num'>{_esc(_cost(es['judge_est_cost']))}</td>"
            f"<td class='num'><strong>{_esc(_cost(es['est_cost'], zero_ok=es['priced_count'] > 0))}</strong></td>"
            f"</tr>"
        )
    if not rows:
        rows.append(
            "<tr><td colspan='10' class='empty'>No harness eval runs recorded "
            "under <code>&lt;skill&gt;/runs/</code>.</td></tr>"
        )
    return "".join(rows)


def _experiment_history_rows(harness_evals: dict[str, Any]) -> str:
    grouped = _group_eval_by_experiment(harness_evals)
    rows: list[str] = []
    for (skill, exp), trials in sorted(grouped.items(), reverse=True):
        worker = sum(1 for t in trials if t["configuration"] in ("with-skill", "baseline"))
        judges = sum(t.get("judge_call_count", 0) for t in trials)
        billed = sum(t["token_usage"]["billed_output_tokens"] for t in trials)
        costs = [t["est_cost"] for t in trials if t["est_cost"] is not None]
        total_cost = round(sum(costs), 6) if costs else None
        models = sorted({t["model"] or "unknown" for t in trials})
        model_label = models[0] + ("…" if len(models) > 1 else "")
        vid = _slug("eval-exp", skill, exp)
        rows.append(
            f"<tr class='clickable-row' onclick=\"openEvalExperiment('{vid}')\">"
            f"<td><code>{_esc(skill)}</code></td>"
            f"<td><code>{_esc(exp)}</code></td>"
            f"<td><code>{_esc(model_label)}</code></td>"
            f"<td class='num'>{worker}</td>"
            f"<td class='num'>{judges}</td>"
            f"<td class='num'>{_num(billed)}</td>"
            f"<td class='num'><strong>{_esc(_cost(total_cost, zero_ok=bool(costs)))}</strong></td>"
            f"</tr>"
        )
    if not rows:
        rows.append(
            "<tr><td colspan='7' class='empty'>No experiments recorded yet.</td></tr>"
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
            f"<button class='crumb-link' onclick='backToEvalOverview()'>← Harness Eval Calls</button>"
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

        sub_rows: list[str] = []
        for sub in t.get("subagents", []):
            sm = sub.get("model") or "—"
            if sub.get("reasoning_effort"):
                sm = f"{sm} ({sub['reasoning_effort']})"
            sub_rows.append(
                f"<tr><td><code>depth {sub.get('depth', 1)}</code></td>"
                f"<td><code>{_esc(sub.get('agent_role') or 'default')}</code></td>"
                f"<td><code>{_esc(str(sub['thread_id'])[:12])}</code></td>"
                f"<td><code>{_esc(sm)}</code></td>"
                f"<td class='num'>{sub['command_count']} ({sub['failed_command_count']})</td>"
                f"<td class='num'>{_num(sub['token_usage']['billed_output_tokens'])}</td>"
                f"<td class='num'>{_esc(_cost(sub.get('est_cost')))}</td></tr>"
            )
        sub_table = (
            "<table><thead><tr><th>Depth</th><th>Role</th><th>Thread</th>"
            "<th>Model</th><th class='num'>Cmds (fail)</th>"
            "<th class='num'>Billed out</th><th class='num'>Est. cost</th></tr></thead>"
            f"<tbody>{''.join(sub_rows)}</tbody></table>"
            if sub_rows
            else "<p class='muted'>No subagents spawned in this trial.</p>"
        )

        # Chronological trial trace: prompt -> worker -> subagents -> judges
        trace_steps = [
            ("Trial prompt", _esc(t["first_prompt"] or "—")),
            (
                "Worker synthesis",
                f"{t['turn_count']} turn(s), {t['command_count']} command(s) "
                f"({t['failed_command_count']} failed), "
                f"{_num(ptok['billed_output_tokens'])} billed output tokens",
            ),
        ]
        if t["subagent_spawn_count"]:
            trace_steps.append(
                (
                    "Parallel subagents",
                    f"{t['subagent_spawn_count']} spawn(s), "
                    f"{_num(stok['billed_output_tokens'])} billed output tokens",
                )
            )
        if t.get("judge_call_count"):
            trace_steps.append(
                (
                    "Semantic judge call",
                    f"{t['judge_call_count']} judge(s), "
                    f"{_num(jtok['billed_output_tokens'])} billed output tokens, "
                    f"{_esc(_cost(t['judge_est_cost']))}",
                )
            )
        trace_html = "".join(
            f"<div class='trace-step'><div class='trace-dot'></div>"
            f"<div><div class='trace-step-title'>{label}</div>"
            f"<div class='trace-step-body'>{body}</div></div></div>"
            for label, body in trace_steps
        )

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
            judge_blocks += (
                f"<div class='judge-block'><div class='judge-head'>Judge "
                f"{_esc(j.get('judge'))} <span class='badge {jcls}'>{_esc(jv)}</span> "
                f"<span class='grade-score'>{_esc(j.get('score'))}/100</span></div>"
                "<table><thead><tr><th>Check</th><th>Pass</th><th class='num'>Pts</th>"
                "<th>Notes</th></tr></thead>"
                f"<tbody>{_grade_check_rows(j.get('checks') or [])}</tbody></table></div>"
            )
        grade_section = (
            "<h4>Grading</h4><div class='grade-grid'>"
            f"{_grade_panel('Deterministic', grades.get('deterministic'))}"
            f"{_grade_panel('Semantic (consensus)', sem, extra=sem_extra)}"
            "</div>"
        )
        if judge_blocks:
            grade_section += f"<h4>Per-judge detail</h4>{judge_blocks}"

        views.append(
            f"<div id='{vid}' class='detail-view hidden'>"
            f"<button class='crumb-link' onclick=\"openEvalExperiment('{exp_vid}')\">"
            f"← {_esc(t['eval_skill'])} / {_esc(t['experiment'])}</button>"
            f"<div class='card'><div class='card-header'><div>"
            f"<div class='card-title'>{_esc(t['test_case'])} "
            f"<span class='badge {cfg_cls}'>{_esc(cfg)}</span> / {_esc(t['trial'])}</div>"
            f"<div class='card-subtitle'>{_esc(_ts(t['started_at']))} · "
            f"{_esc(t['model'] or 'unknown')}</div></div>"
            f"<span class='badge badge-slate'>{_esc(_cost(t['est_cost']))}</span></div>"
            f"<div class='kpi-grid'>"
            f"{_kpi('Worker billed', _num(ptok['billed_output_tokens']), 'tokens')}"
            f"{_kpi('Subagent billed', _num(stok['billed_output_tokens']), 'tokens')}"
            f"{_kpi('Judge billed', _num(jtok['billed_output_tokens']), 'tokens')}"
            f"{_kpi('Trial total', _num(tok['billed_output_tokens']), _cost(t['est_cost']))}"
            f"</div>"
            f"{grade_section}"
            f"<h4>Trial trace</h4><div class='trace-timeline'>{trace_html}</div>"
            f"<h4>Subagents ({t['subagent_spawn_count']})</h4>{sub_table}"
            f"</div></div>"
        )
    return "".join(views)


# --------------------------------------------------------------------------- #
# Shared chrome                                                               #
# --------------------------------------------------------------------------- #

def _kpi(label: str, value: str, sub: str = "") -> str:
    sub_html = f"<div class='kpi-sub'>{sub}</div>" if sub else ""
    return (
        f"<div class='kpi-card'><div class='kpi-label'>{_esc(label)}</div>"
        f"<div class='kpi-value'>{value}</div>{sub_html}</div>"
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
        eval_banner = _latest_banner(
            "LATEST EVAL",
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
      Real Project Work <span class="tab-pill">{_esc(real_tab_pill)}</span>
    </button>
    <button class="mode-tab" id="tab-eval" onclick="switchMode('eval')">
      Harness Eval Calls <span class="tab-pill">{_esc(eval_tab_pill)}</span>
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
      {real_banner}
      <div class="kpi-grid">
        {_kpi("Total Est. Cost", real_cost, f"{totals['session_count']} sessions · {totals['subagent_spawn_count']} subagents")}
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
          <div class="card-title">Subagents (Real Project Work)</div>
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
        {_kpi("Eval Est. Cost", eval_cost, f"{etotals['total_call_count']} total calls")}
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
          <th class="num">Billed Out</th><th class="num">Total Cost</th></tr></thead>
          <tbody>{_experiment_history_rows(harness_evals)}</tbody></table>
      </div>
    </div>
    {_eval_experiment_details(harness_evals)}
    {_eval_trial_details(harness_evals)}
  </div>

  <div class="note-box"><strong>Zero-token local telemetry.</strong>
    Parsed from <code>~/.codex/sessions/</code> and local
    <code>&lt;skill&gt;/runs/</code> traces. Real Project Work and Harness Eval
    Calls are tracked separately so benchmark runs never pollute real usage.
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
.kpi-card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px; }
.kpi-label { font-size:11px; text-transform:uppercase; letter-spacing:0.04em;
  color:var(--text-muted); font-weight:600; }
.kpi-value { font-size:22px; font-weight:700; font-variant-numeric:tabular-nums; margin-top:6px; }
.kpi-sub { font-size:12px; color:var(--text-muted); margin-top:4px; }
.card { background:var(--card); border:1px solid var(--border); border-radius:10px;
  padding:16px 18px; margin-bottom:18px; }
.card-header { display:flex; justify-content:space-between; align-items:flex-start;
  gap:12px; margin-bottom:10px; flex-wrap:wrap; }
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
.row-title { font-weight:700; color:var(--primary); margin-bottom:2px; }
.exp-badges { display:flex; gap:4px; flex-wrap:wrap; margin-top:3px; }
code { font-family:var(--mono); font-size:11px; background:#eef4fb; border:1px solid var(--border);
  border-radius:4px; padding:1px 5px; color:#1e293b; }
.badge { display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px;
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
.note-box { font-size:12px; color:var(--text-muted); background:#eef4fb;
  border:1px solid var(--border); border-radius:8px; padding:12px 14px; }
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
function openSkillDetail(skillName) {
  document.getElementById('real-overview').classList.add('hidden');
  _hideAll('#mode-real .detail-view');
  var el = document.getElementById('real-skill-detail');
  if (el) {
    el.classList.remove('hidden');
    var nm = document.getElementById('skill-detail-name');
    if (nm) nm.textContent = skillName;
  }
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
"""
