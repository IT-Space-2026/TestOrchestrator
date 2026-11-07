"""Shared Plotly figure construction + theme — Python-side only.
Figures serialize to JSON; templates render via vendored plotly.js.
Aggregation stays in queries, NOT inside figures."""
import json

_PALETTE = {"passed": "#2e7d32", "failed": "#c62828",
            "error": "#ef6c00", "skipped": "#757575",
            "running": "#1565c0", "queued": "#90a4ae",
            "unknown": "#616161", "canceled": "#8d6e63"}
_BASE = dict(font=dict(family="system-ui,sans-serif", size=12),
             height=300,
             margin=dict(l=46, r=16, t=10, b=46),
             paper_bgcolor="rgba(0,0,0,0)",
             plot_bgcolor="rgba(0,0,0,0)", showlegend=True,
             legend=dict(orientation="h", y=-0.28),
             xaxis=dict(tickangle=0))


def _fig(traces, layout=None):
    lay = dict(_BASE)
    lay.update(layout or {})
    return json.dumps({"data": traces, "layout": lay})


def outcome_trend(rows) -> str:
    """rows: [{day, passed, failed, error, skipped}]"""
    days = [r["day"] for r in rows]
    out = []
    for k in ("passed", "failed", "error", "skipped"):
        if any(r.get(k) for r in rows):
            out.append({"type": "bar", "name": k.capitalize(),
                        "x": days, "y": [r.get(k, 0) for r in rows],
                        "marker": {"color": _PALETTE[k]}})
    if not out:                      # all-zero period — no trace spam
        return "null"
    return _fig(out, {"barmode": "stack",
                      "yaxis": {"title": "Test cases"}})


def pass_rate_trend(rows) -> str:
    """rows: [{day, passed, failed, error}] — denominator
    passed+failed+error, skips excluded."""
    pts = [{"x": r["day"],
            "y": (100 * r["passed"] /
                  max(1, r["passed"] + r["failed"] + r["error"]))}
           for r in rows if r.get("passed") is not None]
    tr = [{"type": "scatter", "mode": "lines+markers",
           "name": "Pass rate %",
           "x": [p["x"] for p in pts], "y": [p["y"] for p in pts],
           "line": {"color": _PALETTE["passed"]}}]
    return _fig(tr, {"yaxis": {"title": "Pass rate %",
                               "range": [0, 105]}})


def duration_trend(rows) -> str:
    """rows: [{day, seconds}] completed executions only."""
    tr = [{"type": "scatter", "mode": "lines+markers",
           "name": "Duration (s)",
           "x": [r["day"] for r in rows],
           "y": [r["seconds"] for r in rows],
           "line": {"color": _PALETTE["running"]}}]
    return _fig(tr, {"yaxis": {"title": "Duration (s)"}})


def failure_breakdown(rows) -> str:
    """rows: [{case, count}]"""
    tr = [{"type": "bar", "orientation": "h",
           "name": "Failures",
           "x": [r["count"] for r in rows],
           "y": [r["case"] for r in rows],
           "marker": {"color": _PALETTE["failed"]}}]
    return _fig(tr, {"xaxis": {"title": "Occurrences"}})


def palette(key):
    return _PALETTE.get(key, _PALETTE["unknown"])
