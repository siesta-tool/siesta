"""
tests/vldb_eval/plot_long20.py — figures and summary numbers for the long
complex-pattern experiment (pattern_long: BPIC 2017, lengths 10-20,
perspectives case / Action / org:resource; SIESTA vs ELK (EQL) vs LPG (Kuzu)).

Reads results/exp4_patterns/{siesta,eql,lpg}/long20/; writes
  results/figures/s2_long20_outcomes.pdf   outcome of every query, per system
  results/figures/s2_long20_latency.pdf    median latency vs length (Action, org:resource),
      SIESTA-MP / ELK / Upper-bound (LPG step-by-step Cypher, dotted), failed medians above the 200 s timeout
  results/tables/s2_long20_summary.json    the numbers quoted in the report
  results/figures/s2_long20_main_outcomes.pdf, s2_long20_main_latency.pdf
      the main comparison: SIESTA vs the native LPG (plain Cypher) and ELK
      queries.  The step-by-step Cypher is a hand-optimised upper bound for
      LPG and is left out.  In the latency figure the per-length median
      counts every unanswered query (timeout, memory, crash, wrong) as above
      the timeout; a median that is a failure is drawn as a sign on the
      timeout line (x timeout, triangle memory / crash).

Outcomes: correct, wrong (a result that differs from the ground truth),
timeout (incl. skipped after 3 consecutive timeouts of a kind), memory
(Kuzu buffer pool exhausted), crash (Kuzu segfault), not expressible (EQL has
no ``?`` / ``+``), error (anything else).  SIESTA queries whose measured run
overlapped an OOM kill of the Spark worker (oom_<perspective>.log) are counted
in ``oom_overlap``.

Usage:
    python -m tests.vldb_eval.plot_long20
"""

from __future__ import annotations

import collections
import json
import math
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from tests.vldb_eval.eval_common import RESULTS_DIR  # noqa: E402
from tests.vldb_eval.plot_indexing import GRID, SERIES, TEXT2  # noqa: E402  (shared style)

FIGS = RESULTS_DIR / "figures"
TABLES = RESULTS_DIR / "tables"
E = RESULTS_DIR / "exp4_patterns"
PERSPS = [("case", "case"), ("Action", "Action"), ("org:resource", "org_resource")]
# (key, label, file pattern); colour follows the system, never its rank
SYSTEMS = [
    ("siesta", "SIESTA (adaptive, warm)", "siesta/long20/bpic2017.{p}.jsonl", SERIES[0]),
    ("lpg_stepwise", "LPG, step-by-step Cypher", "lpg/long20/bpic2017.{p}.stepwise.jsonl", SERIES[1]),
    ("lpg_plain", "LPG, plain Cypher", "lpg/long20/bpic2017.{p}.jsonl", SERIES[6]),
    ("elk", "ELK (EQL)", "eql/long20/bpic2017.{p}.jsonl", SERIES[2]),
]
# main comparison: native queries only; ELK = EQL (the step-by-step Cypher and ELK's
# per-group regexp / intervals indexes are reported separately, not compared here)
MAIN_SYSTEMS = [
    ("siesta", "SIESTA (adaptive, warm)", "siesta/long20/bpic2017.{p}.jsonl", SERIES[0]),
    ("lpg_plain", "LPG (Cypher)", "lpg/long20/bpic2017.{p}.jsonl", SERIES[1]),
    ("elk", "ELK (EQL)", "eql/long20/bpic2017.{p}.jsonl", SERIES[2]),
]
# why a system has no record for a perspective
MISSING = {"lpg_plain": "not run (see report)"}
TIMEOUT_S = 200.0
OUTCOMES = ["correct", "wrong", "timeout", "memory", "crash", "error", "not expressible"]
OUTCOME_STYLE = {   # fill, hatch: status-like states, never the system colours
    "correct": ("#3d3c39", None), "wrong": ("#e34948", None), "timeout": ("#a8a6a0", None),
    "memory": ("#d6d4ce", "////"), "crash": ("#d6d4ce", "xxxx"), "error": ("#d6d4ce", "...."),
    "not expressible": ("#ffffff", "\\\\\\\\"),
}


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("qid"):
            out.append(r)
    return out


def outcome(r: dict) -> str:
    if r.get("unsupported"):
        return "not expressible"
    if r.get("parity") is True:
        return "correct"
    if r.get("parity") is False:
        return "wrong"
    if r.get("crashed"):
        return "crash"
    if r.get("timed_out") or r.get("skipped"):
        return "timeout"
    if "Buffer manager" in (r.get("error") or ""):
        return "memory"
    return "error"


def oom_times(p_file: str) -> list[float]:
    path = E / "siesta" / "long20" / f"oom_{p_file}.log"
    if not path.exists():
        return []
    return [float(l.split()[0]) for l in path.read_text().splitlines() if "oom-kill:constraint" in l]


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def eql_expressible(pf: str) -> set[str]:
    """Query ids EQL can express (no ``?`` / ``+``), from ELK's records."""
    return {r["qid"] for r in read(E / "eql" / "long20" / f"bpic2017.{pf}.jsonl")
            if outcome(r) != "not expressible"}


def summarise(subset: dict | None = None) -> dict:
    """``subset``: {perspective file name: qids} to restrict every system to."""
    out: dict = {}
    for persp, pf in PERSPS:
        kills = oom_times(pf)
        for key, label, pat, _ in {x[0]: x for x in SYSTEMS + MAIN_SYSTEMS}.values():
            rs = read(E / pat.format(p=pf))
            if subset is not None:
                rs = [r for r in rs if r["qid"] in subset[pf]]
            if not rs:
                continue
            c = collections.Counter(outcome(r) for r in rs)
            ok = [r["time_s"] for r in rs if outcome(r) == "correct"]
            rec = {"queries": len(rs), **{o: c.get(o, 0) for o in OUTCOMES},
                   "median_s": statistics.median(ok) if ok else None,
                   "p90_s": pct(ok, 0.9), "max_s": max(ok) if ok else None}
            for kind in ("structural", "attribute"):
                kk = [r["time_s"] for r in rs if r["kind"] == kind and outcome(r) == "correct"]
                rec[f"{kind}_correct"] = len(kk)
                rec[f"{kind}_median_s"] = statistics.median(kk) if kk else None
            if key == "siesta":
                rec["all_persisted"] = sum(1 for r in rs if r.get("all_persisted"))
                rec["oom_kills"] = len(kills)
                rec["oom_overlap"] = sorted(
                    r["qid"] for r in rs if r.get("started_at") and r.get("ended_at")
                    and any(r["started_at"] <= t <= r["ended_at"] for t in kills))
            out.setdefault(persp, {})[key] = rec
    return out


def fig_outcomes(summary: dict, systems=None, name: str = "s2_long20_outcomes.pdf",
                 missing: dict | None = None, totals: dict | None = None) -> None:
    """``totals``: queries per perspective (default 110), the x range of each panel."""
    systems = [x for x in (systems or SYSTEMS) if any(x[0] in sy for sy in summary.values())]
    fig, axes = plt.subplots(1, 3, figsize=(7.0, 0.5 + 0.4 * len(systems)), sharey=True)
    for ax, (persp, _) in zip(axes, PERSPS):
        names = [(k, lab) for k, lab, _, _ in systems]          # same rows in every panel
        for i, (k, _) in enumerate(names):
            s, left = summary.get(persp, {}).get(k), 0
            if s is None:
                ax.text(2, i, (missing or {}).get(k, "not run"), va="center", ha="left", fontsize=6.5,
                        color=TEXT2)
                continue
            for o in OUTCOMES:
                w = s[o]
                if not w:
                    continue
                fill, hatch = OUTCOME_STYLE[o]
                # hatch lines take the edge colour: grey for hatched states
                ax.barh(i, w, left=left, height=0.62, color=fill, hatch=hatch,
                        edgecolor=TEXT2 if hatch else "white", linewidth=0.4 if hatch else 1.0)
                left += w
        ax.set_yticks(range(len(names)), [lab for _, lab in names], fontsize=7)
        ax.set_ylim(len(names) - 0.5, -0.5)
        total = (totals or {}).get(persp, 110)
        ax.set_xlim(0, total)
        ax.set_title(persp, fontsize=8)
        ax.set_xlabel(f"Queries (of {total})", fontsize=7)
        ax.tick_params(axis="x", labelsize=7)
        ax.grid(axis="x", color=GRID, linewidth=0.5)
        ax.set_axisbelow(True)
    keys = {k for k, _, _, _ in systems}
    present = [o for o in OUTCOMES if any(s[o] for sy in summary.values() for k, s in sy.items() if k in keys)]
    handles = [Patch(facecolor=OUTCOME_STYLE[o][0], hatch=OUTCOME_STYLE[o][1], edgecolor=TEXT2,
                     linewidth=0.5, label=o) for o in present]
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, -0.04), ncol=len(handles),
               fontsize=7, frameon=False)
    fig.tight_layout(rect=(0, 0.1, 1, 1))
    fig.savefig(FIGS / name)
    plt.close(fig)


def fig_latency() -> dict:
    """
    Returns the medians of correct answers per length (summary json, all
    perspectives).  The figure (paper style, single column) stacks Action
    and org:resource; the per-length median counts every unanswered query as
    above the 200 s timeout and a failed median is drawn in the band above
    the timeout line, one row per system (plot_long_combined).
    """
    from tests.vldb_eval import plot_long_combined as lc     # lazy: it imports this module
    by_len: dict = {}
    for persp, pf in PERSPS:
        for key, _, pat, _ in SYSTEMS:
            g = collections.defaultdict(list)
            for r in read(E / pat.format(p=pf)):
                if outcome(r) == "correct":
                    g[r["length"]].append(r["time_s"])
            if g:
                by_len.setdefault(persp, {})[key] = {str(L): {"median_s": statistics.median(g[L]), "n": len(g[L])}
                                                     for L in sorted(g)}
    keys = ["siesta", "lpg_oracle", "elk"]
    label = {**lc.LABEL, "lpg_oracle": "Upper-bound (LPG)"}
    persps = [(persp, pf) for persp, pf in PERSPS if persp != "case"]
    stats = {persp: {k: lc.medians([lc.LONG20_FILES[k].format(p=pf)]) for k in keys} for persp, pf in persps}
    for persp, _ in persps:          # a length EQL cannot express at all (org:resource L17): drawn as timeout
        for L in range(10, 21):
            stats[persp]["elk"].setdefault(L, {"median_s": None, "not_expressible": "drawn as timeout"})
        stats[persp]["elk"] = dict(sorted(stats[persp]["elk"].items()))
    failing =[k for k in keys if any(s["median_s"] is None for st in stats.values() for s in st[k].values())]
    rows = {k: TIMEOUT_S * 1.45 * 1.4 ** i for i, k in enumerate(failing)}
    with plt.rc_context(lc.EXP5_STYLE):
        fig, axes = plt.subplots(len(persps), 1, figsize=(4.2, 3.6), sharex=True, sharey=True)
        for ax, (persp, _) in zip(axes, persps):
            for k in keys:
                lc.draw(ax, stats[persp][k], lc.COLOR[k], "o", ":" if k == "lpg_oracle" else "-", rows.get(k))
            ax.set_title(persp, fontsize=9.5, pad=3)
        lc.finish_axes(list(axes), rows, 10, 20, 2, timeout=TIMEOUT_S)
        axes[0].texts[-1].remove()          # the timeout label goes in the legend instead
        lows = [y for ax in axes for ln in ax.get_lines() if ln.get_linestyle() != "None" for y in ln.get_ydata()]
        for i, ax in enumerate(axes):
            ax.set_ylabel("")
            ax.set_xlabel("Pattern length" if i == len(axes) - 1 else "")
            ax.set_ylim(min(lows) * 0.6, ax.get_ylim()[1] * 1.25)    # headroom over the failed band
        fig.supylabel("Median latency (s)", fontsize=9.5)
        handles = [Line2D([], [], color=lc.COLOR[k], linewidth=1.6, marker="o", markersize=4, label=label[k])
                   for k in keys if k != "lpg_oracle"]
        handles.append(Line2D([], [], color=lc.TEXT2, linewidth=0.8, linestyle="--",
                              label=f"timeout ({TIMEOUT_S:g} s)"))
        # the hand-tuned reference last, dotted
        handles.append(Line2D([], [], color=lc.COLOR["lpg_oracle"], linewidth=1.6, linestyle=":", marker="o",
                              markersize=4, label=label["lpg_oracle"]))
        fig.tight_layout(rect=(0, 0, 1, 0.92), pad=0.3, h_pad=0.5)
        fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 1.0), ncol=len(handles),
                   fontsize=8, frameon=False, columnspacing=1.0, handlelength=2.0, handletextpad=0.4,
                   borderaxespad=0.1)
        fig.savefig(FIGS / "s2_long20_latency.pdf")
        plt.close(fig)
    return by_len


# sign of a failed median: the most frequent failure kind at that length; ties go to the first here
FAIL_SIGN = {"timeout": ("x", "timeout"), "memory": ("^", "out of memory / crash"),
             "crash": ("^", "out of memory / crash"), "error": ("^", "out of memory / crash"),
             "wrong": ("D", "wrong result")}


def fig_main_latency() -> dict:
    """Censored median per length: unanswered queries count as above the timeout."""
    fig, axes = plt.subplots(1, 3, figsize=(7.0, 2.4), sharey=True)
    by_len: dict = {}
    # failure signs sit above the timeout line, one row per system so they never overlap
    ceiling = {k: TIMEOUT_S * 1.3 * 1.32 ** i for i, (k, _, _, _) in enumerate(MAIN_SYSTEMS)}
    for ax, (persp, pf) in zip(axes, PERSPS):
        ax.axhline(TIMEOUT_S, color=TEXT2, linewidth=0.8, linestyle="--")
        for key, label, pat, color in MAIN_SYSTEMS:
            rs = [r for r in read(E / pat.format(p=pf)) if outcome(r) != "not expressible"]
            if not rs:
                continue
            g = collections.defaultdict(list)
            for r in rs:
                g[r["length"]].append(r)
            line_x, line_y, fail = [], [], collections.defaultdict(list)
            stats = {}
            for L in sorted(g):
                vals = sorted(r["time_s"] if outcome(r) == "correct" else float("inf") for r in g[L])
                m = statistics.median_high(vals)
                fails = collections.Counter(outcome(r) for r in g[L] if outcome(r) != "correct")
                stats[str(L)] = {"n": len(vals), "correct": sum(v != float("inf") for v in vals),
                                 "median_s": None if m == float("inf") else m, **dict(fails)}
                if m == float("inf"):
                    order = list(FAIL_SIGN)
                    kind = max(fails, key=lambda k: (fails[k], -order.index(k)))
                    fail[FAIL_SIGN[kind][0]].append(L)
                else:
                    line_x.append(L)
                    line_y.append(m)
            if line_x:
                ax.plot(line_x, line_y, "-o", color=color, linewidth=2, markersize=4, label=label)
            for marker, Ls in fail.items():
                ax.plot(Ls, [ceiling[key]] * len(Ls), linestyle="none", marker=marker, color=color,
                        markersize=6 if marker == "x" else 5, markeredgewidth=1.6,
                        markerfacecolor="none" if marker in ("^", "D") else color,
                        label=label if not line_x else None)
            by_len.setdefault(persp, {})[key] = stats
        ax.set_yscale("log")
        ax.set_ylim(top=max(ceiling.values()) * 1.3)
        ax.minorticks_off()
        ax.set_title(persp, fontsize=8)
        ax.set_xlabel("Pattern length", fontsize=7)
        ax.set_xticks(range(10, 21, 2))
        ax.tick_params(labelsize=7)
        ax.grid(color=GRID, linewidth=0.5)
        ax.set_axisbelow(True)
    # lower bound from the data: the decade below the fastest plotted median
    lows = [y for ax in axes for ln in ax.get_lines() if ln.get_linestyle() != "None"
            for y in ln.get_ydata() if y < TIMEOUT_S]
    lo = 10 ** math.floor(math.log10(min(lows) * 0.8)) if lows else 1
    ticks = [t for t in (0.01, 0.1, 1, 10, 100) if t >= lo]
    for ax in axes:
        ax.set_ylim(bottom=lo)
        ax.set_yticks(ticks, [f"{t:g}" for t in ticks])
    axes[0].text(10, TIMEOUT_S * 0.82, "timeout (200 s)", fontsize=6, color=TEXT2, va="top")
    axes[0].set_ylabel("Median latency per length\n(s, log)", fontsize=7)
    handles = {}
    for ax in axes:
        for h, l in zip(*ax.get_legend_handles_labels()):
            handles.setdefault(l, h)
    order = [lab for _, lab, _, _ in MAIN_SYSTEMS if lab in handles]
    from matplotlib.lines import Line2D
    used = {ln.get_marker() for ax in axes for ln in ax.get_lines() if ln.get_linestyle() == "None"}
    signs = [Line2D([], [], linestyle="none", marker=m, color=TEXT2, markeredgewidth=1.6,
                    markerfacecolor=TEXT2 if m == "x" else "none", label=lab)
             for m, lab in dict.fromkeys(FAIL_SIGN.values()) if m in used]
    fig.legend([handles[l] for l in order] + signs, order + [h.get_label() for h in signs],
               loc="lower center", bbox_to_anchor=(0.5, -0.04), ncol=len(order) + len(signs), fontsize=7,
               frameon=False)
    fig.tight_layout(rect=(0, 0.1, 1, 1))
    fig.savefig(FIGS / "s2_long20_main_latency.pdf")
    plt.close(fig)
    return by_len


def main() -> None:
    FIGS.mkdir(parents=True, exist_ok=True)
    TABLES.mkdir(parents=True, exist_ok=True)
    summary = summarise()
    fig_outcomes(summary)
    by_len = fig_latency()
    (TABLES / "s2_long20_summary.json").write_text(
        json.dumps({"overall": summary, "by_length": by_len}, indent=1, default=str))
    fig_outcomes(summary, MAIN_SYSTEMS, "s2_long20_main_outcomes.pdf", MISSING)
    main_len = fig_main_latency()
    # the same comparison restricted to the queries EQL can express (no ? / +)
    sub = {pf: eql_expressible(pf) for _, pf in PERSPS}
    sub_summary = summarise(sub)
    fig_outcomes(sub_summary, MAIN_SYSTEMS, "s2_long20_main_outcomes_expressible.pdf", MISSING,
                 totals={persp: len(sub[pf]) for persp, pf in PERSPS})
    (TABLES / "s2_long20_main_expressible_summary.json").write_text(json.dumps(
        {"queries": {persp: len(sub[pf]) for persp, pf in PERSPS},
         "overall": {p: {k: v for k, v in sy.items() if k in {m[0] for m in MAIN_SYSTEMS}}
                     for p, sy in sub_summary.items()}}, indent=1, default=str))
    (TABLES / "s2_long20_main_summary.json").write_text(json.dumps(
        {"overall": {p: {k: v for k, v in sy.items() if k in {m[0] for m in MAIN_SYSTEMS}}
                     for p, sy in summary.items()},
         "by_length_censored": main_len}, indent=1, default=str))
    for persp, sy in summary.items():
        for k, s in sy.items():
            print(f"{persp:13s} {k:13s} " + " ".join(f"{o[:6]}={s[o]}" for o in OUTCOMES if s[o])
                  + f"  median={s['median_s'] and round(s['median_s'], 2)}"
                  + (f"  oom_overlap={len(s['oom_overlap'])}" if k == 'siesta' else ""))
    print("figures ->", FIGS, "| summary ->", TABLES / "s2_long20_summary.json")


if __name__ == "__main__":
    main()
