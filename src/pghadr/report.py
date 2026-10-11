"""Render a DrillResult as Markdown (for people) and JSON (for diffing and charts)."""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pghadr import __version__, analysis
from pghadr.drill import DrillResult
from pghadr.events import write_jsonl


def _f(x: float | None, unit: str = "", nd: int = 1) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:.{nd}f}{unit}"


def to_dict(r: DrillResult) -> dict[str, Any]:
    s = r.summary
    d: dict[str, Any] = {
        "pghadr_version": __version__,
        "kind": r.kind,
        "run_id": r.run_id,
        "started": r.started,
        "params": r.params,
        "action": {**asdict(r.action), "duration_s": r.action.duration_s},
        "state_before": r.state_before,
        "state_after": r.state_after,
        "summary": {
            "attempts": s.attempts,
            "ok": s.ok,
            "failed": s.failed,
            "availability_pct": s.availability_pct,
            "duration_s": s.duration_s,
            "latency_ms": s.latency_ms,
            "max_write_gap_s": s.max_write_gap_s,
            "outages": [
                {**asdict(o), "write_gap_s": o.write_gap_s, "recovered": o.recovered}
                for o in s.outages
            ],
            "switches": [asdict(w) for w in s.switches],
        },
        "notes": r.notes,
    }
    if r.loss is not None:
        d["loss"] = {**asdict(r.loss), "ok": r.loss.ok}
    if r.dr is not None:
        d["dr"] = {
            **asdict(r.dr),
            "rto_s": r.dr_rto_s,
            "replica_writable_t": r.replica_writable_t,
            "replicated_after_cutoff_s": r.dr.replicated_after_cutoff_s,
        }
    return d


def _state_rows(before: dict | None, after: dict | None) -> list[str]:
    if not before and not after:
        return []
    keys = [
        ("state", "State"),
        ("sku", "SKU"),
        ("version", "PostgreSQL"),
        ("location", "Region"),
        ("ha_mode", "HA mode"),
        ("ha_state", "HA state"),
        ("zone", "Primary zone"),
        ("standby_zone", "Standby zone"),
        ("replication_role", "Replication role"),
    ]
    b, a = before or {}, after or {}
    rows = ["| | Before | After |", "|---|---|---|"]
    for k, label in keys:
        if b.get(k) is None and a.get(k) is None:
            continue
        rows.append(f"| {label} | {b.get(k) or ''} | {a.get(k) or ''} |")
    return rows


def to_markdown(r: DrillResult) -> str:
    s = r.summary
    title = "HA drill" if r.kind == "ha" else "Cross-region DR drill"
    name = (r.state_before or {}).get("name")
    lines = [f"# pghadr {title}: {r.action.name}", ""]
    lines.append(f"- run: `{r.run_id}` (pghadr {__version__})")
    lines.append(f"- started: {r.started}")
    if name:
        lines.append(f"- server: `{name}`")
    lines.append(
        f"- probe: one committed INSERT every {_f(s.interval_s, 's', 2)}, "
        f"{s.attempts} attempts over {_f(s.duration_s, 's')}"
    )
    lines.append("")

    lines += ["## Result", ""]
    lines += ["| Metric | Value |", "|---|---|"]
    lines.append(f"| Action took (as reported by the tool) | {_f(r.action.duration_s, ' s')} |")
    if r.kind == "ha":
        lines.append(
            f"| Longest write gap seen by the client | **{_f(s.max_write_gap_s, ' s')}** |"
        )
        lines.append(f"| Outages (runs of failed writes) | {len(s.outages)} |")
        lines.append(f"| Node switches | {len(s.switches)} |")
        if r.loss is not None:
            verdict = "**0 lost**" if r.loss.ok else f"**{len(r.loss.lost)} LOST**"
            lines.append(f"| Acknowledged writes | {r.loss.acknowledged} |")
            lines.append(f"| Acknowledged writes missing afterwards | {verdict} |")
            lines.append(
                f"| In-doubt writes (sent, then failed) | {r.loss.in_doubt}, "
                f"of which {r.loss.in_doubt_committed} were committed |"
            )
    else:
        dr = r.dr
        assert dr is not None
        lines.append(f"| Replica accepted writes after (RTO) | **{_f(r.dr_rto_s, ' s')}** |")
        lines.append(
            f"| Primary writes acknowledged before the promote | {dr.acknowledged_before_cutoff} |"
        )
        lines.append(f"| ...found on the promoted replica | {dr.present_on_replica} |")
        lines.append(
            f"| ...missing (data lost in a real regional failover) | **{len(dr.missing)}** |"
        )
        lines.append(
            f"| RPO at the cutoff (age of the oldest missing write) | **{_f(dr.rpo_s, ' s', 3)}** |"
        )
        lines.append(
            f"| Replication kept running after the promote was requested | "
            f"{_f(dr.replicated_after_cutoff_s, ' s')} |"
        )
        lines.append(
            f"| Acknowledged writes missing before replication stopped (holes) | "
            f"**{len(dr.holes)}** |"
        )
    lines.append(f"| Availability during the run | {_f(s.availability_pct, ' %', 2)} |")
    lat = s.latency_ms
    lines.append(
        f"| Write latency p50 / p95 / p99 | {_f(lat['p50'], ' ms')} / {_f(lat['p95'], ' ms')} "
        f"/ {_f(lat['p99'], ' ms')} |"
    )
    lines.append("")

    line, bucket = analysis.timeline(r.samples)
    if line:
        lines += [
            "## Timeline",
            "",
            f"One character per {bucket:g} s: `#` all writes ok, `.` all failed, `:` mixed.",
            "",
            "```",
            line,
            "```",
            "",
        ]

    lines += ["## Events", "", "| t (s) | Event |", "|---:|---|"]
    events: list[tuple[float, str]] = []
    if r.action.t_start is not None:
        events.append((r.action.t_start, f"{r.action.name} started"))
    if r.action.t_end is not None:
        events.append((r.action.t_end, f"{r.action.name} returned"))
    for o in s.outages:
        err = "; ".join(f"{e} (x{n})" for e, n in o.errors)
        events.append((o.first_fail_t, f"first failed write ({err})"))
        if o.recovered:
            events.append(
                (o.first_ok_after_t, f"writes back after a {_f(o.write_gap_s, ' s')} gap")
            )
        else:
            events.append((o.last_fail_t, "still failing when the probe stopped"))
    for w in s.switches:
        if w.after[0] == w.before[0]:
            what = f"same address `{w.after[0]}`, new postmaster start time (server restarted)"
        else:
            what = f"now answered by `{w.after[0]}` (was `{w.before[0]}`)"
        events.append((w.t, what))
    if r.replica_writable_t is not None:
        events.append((r.replica_writable_t, "promoted replica accepted its first write"))
    if r.dr is not None and r.dr.replication_stopped_t is not None:
        events.append((r.dr.replication_stopped_t, "last primary write that reached the replica"))
    for t, what in sorted(events, key=lambda e: e[0]):
        lines.append(f"| {t:.2f} | {what} |")
    lines.append("")

    rows = _state_rows(r.state_before, r.state_after)
    if rows:
        lines += ["## Server", "", *rows, ""]

    if r.loss is not None and r.loss.lost:
        shown = ", ".join(str(x) for x in r.loss.lost[:50])
        lines += ["## Lost writes", "", f"Sequence numbers: {shown}", ""]
    if r.notes:
        lines += ["## Notes", "", *[f"- {n}" for n in r.notes], ""]

    lines += [
        "## How to read this",
        "",
        "The write gap is measured from the last acknowledged write before an outage "
        "to the first one after it, so it is what an application that writes "
        "continuously would see. It overstates the true unavailability by at most one "
        "probe interval plus one write latency. A failed write that had already been "
        "sent is *in doubt*: the server may have committed it, and either outcome is "
        "correct, so it is never counted as lost.",
        "",
    ]
    return "\n".join(lines)


def write(r: DrillResult, out_dir: Path, name: str | None = None) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = name or r.run_id
    md = out_dir / f"{stem}.md"
    js = out_dir / f"{stem}.json"
    md.write_text(to_markdown(r), encoding="utf-8")
    js.write_text(json.dumps(to_dict(r), indent=2, default=str) + "\n", encoding="utf-8")
    write_jsonl(r.samples, out_dir / f"{stem}.samples.jsonl")
    if r.replica_samples:
        write_jsonl(r.replica_samples, out_dir / f"{stem}.replica-samples.jsonl")
    return md, js
