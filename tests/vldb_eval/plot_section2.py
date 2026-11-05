"""
tests/vldb_eval/plot_section2.py — figures and summary numbers for Section 2
(Exp 1 space-time, Exp 3 sensitivity, Exp 4 LPG pattern-length latency).

Reads results/exp_space_time/, results/exp_sensitivity/ and
results/exp4_patterns/lpg/; writes results/figures/s2_*.pdf and
results/tables/s2_summary.json (the numbers quoted in SECTION2_RESULTS.md).

Exp 1 file layout (see SECTION2_REPORT_NOTES.md):
  <ds>.jsonl            parts 1-2 (part-3 records in it are superseded)
  <ds>.part3attr.jsonl  part 3 as plotted: attribute-aware queries only,
                        5 per length 8-11, eager and adaptive, both layouts
  <ds>.part3.jsonl      earlier part 3 (mixed kinds, 1 per length); only used
                        for the BPIC 2017 eager embedded pair-table size,
                        whose part3attr ingest wrote no pairs index (its
                        latency is shown as n/a)

Usage:
    python -m tests.vldb_eval.plot_section2
"""

from __future__ import annotations

import collections
import json
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker  # noqa: E402

from tests.vldb_eval.eval_common import RESULTS_DIR  # noqa: E402
from tests.vldb_eval.plot_indexing import (  # noqa: E402  (shared style, palette, labels)
    DS_COLOR, DS_MARKER, EXP5_STYLE, LABEL, SERIES, SURFACE, TEXT2,
)

FIGS = RESULTS_DIR / "figures"
TABLES = RESULTS_DIR / "tables"
E1 = RESULTS_DIR / "exp_space_time"
E3 = RESULTS_DIR / "exp_sensitivity"
E4 = RESULTS_DIR / "exp4_patterns"
E1_DS = ["bpic2015", "bpic2012", "bpic2017"]
E4_DS = ["bpic2011", "bpic2012", "bpic2015", "bpic2017", "bpic2018"]
ADAPTIVE, EAGER = SERIES[0], SERIES[1]          # system identity, fixed everywhere
EMBEDDED, JOIN = SERIES[2], SERIES[3]           # attribute layout identity
MB = 1e6
PERSP_COLOR = {"case": SERIES[4], "org:resource": SERIES[5], "lifecycle:transition": SERIES[6],
               "EventOrigin": SERIES[7], "Action": SERIES[2]}
PERSP_SHORT = {"case": "case", "org:resource": "resource", "lifecycle:transition": "lifecycle",
               "EventOrigin": "EventOrigin", "Action": "Action", "trace_id": "case"}


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def q_time(r):
    return None if r.get("timed_out") else r.get("time")


# ---------------------------------------------------------------------------
# Exp 1
# ---------------------------------------------------------------------------

def exp1_part1(summary):
    fig, axes = plt.subplots(1, 3, figsize=(7.0, 2.2))
    rows = {}
    for ax, ds in zip(axes, E1_DS):
        rs = read(E1 / f"{ds}.jsonl")
        st = [r for r in rs if r["event"] == "storage" and r.get("part") == "1"]
        q = [r for r in rs if r["event"] == "query" and r.get("part") == "1"]
        eager_mb = next(r["summary"]["total"] for r in st if r["system"] == "eager") / MB
        ad = sorted((r["round"], r["summary"]["total"] / MB) for r in st
                    if r["system"] == "adaptive" and r.get("round", -1) >= 0)
        lat = {rnd: med([q_time(r) for r in q if r["system"] == "adaptive" and r["round"] == rnd])
               for rnd, _ in ad}
        e_lat = med([q_time(r) for r in q if r["system"] == "eager"])
        xs = [mb for _, mb in ad]
        ys = [lat[rnd] for rnd, _ in ad]
        ax.plot(xs, ys, "-o", color=ADAPTIVE, label="Adaptive (after each round)")
        for (rnd, mb), y in (list(zip(ad, ys))[0], list(zip(ad, ys))[-1]):
            ax.annotate(f"round {rnd + 1}", (mb, y), textcoords="offset points", xytext=(5, 2),
                        fontsize=6, color=TEXT2)
        ax.plot([eager_mb], [e_lat], "s", color=EAGER, markersize=6, label="Eager (all pairs)")
        ax.set_xscale("log")
        ax.set_title(LABEL[ds])
        ax.set_xlabel("Storage (MB, log)")
        rows[ds] = {"eager_mb": eager_mb, "eager_median_s": e_lat,
                    "adaptive_mb": dict((f"round{r + 1}", mb) for r, mb in ad),
                    "adaptive_median_s": {f"round{r + 1}": lat[r] for r, _ in ad}}
    axes[0].set_ylabel("Median query latency (s)")
    axes[0].legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(FIGS / "s2_exp1_case_tradeoff.pdf")
    plt.close(fig)
    summary["exp1_part1"] = rows


def exp1_untouched(summary):
    shares = {}
    for ds in E1_DS:
        rs = read(E1 / f"{ds}.jsonl")
        ep = next(r for r in rs if r["event"] == "eager_pairs")
        b = collections.Counter()
        for p in ep["pairs"]:
            t = p["touches"]
            b["never" if t == 0 else ("1-2" if t < 3 else "3+")] += p["rows"]
        tot = sum(b.values())
        shares[ds] = {k: b[k] / tot for k in ("never", "1-2", "3+")}
    fig, ax = plt.subplots(figsize=(3.4, 1.5))
    colors = {"never": "#2a78d6", "1-2": "#8ab4ea", "3+": "#cfe0f6"}
    names = {"never": "never queried", "1-2": "queried 1-2x", "3+": "queried 3+x (hot)"}
    for i, ds in enumerate(E1_DS):
        left = 0.0
        for k in ("never", "1-2", "3+"):
            w = shares[ds][k] * 100
            ax.barh(i, w, left=left, color=colors[k], edgecolor="white", linewidth=1.5,
                    label=names[k] if i == 0 else None, height=0.6)
            left += w
        ax.text(shares[ds]["never"] * 100 / 2, i, f"{shares[ds]['never'] * 100:.1f}%",
                va="center", ha="center", color="white", fontsize=7)
    ax.set_yticks(range(len(E1_DS)), [LABEL[d] for d in E1_DS])
    ax.set_xlabel("Share of eager pair-index rows (%)")
    ax.set_xlim(0, 100)
    ax.invert_yaxis()
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.45), ncol=3)
    ax.grid(axis="y", visible=False)
    fig.savefig(FIGS / "s2_exp1_untouched_pairs.pdf")
    plt.close(fig)
    summary["exp1_untouched_share"] = shares


def exp1_part2(summary):
    fig, axes = plt.subplots(2, 3, figsize=(7.4, 4.0))
    rows = {}
    for col, ds in enumerate(E1_DS):
        rs = read(E1 / f"{ds}.jsonl")
        st = [r for r in rs if r["event"] == "storage" and r.get("part") == "2"]
        q = [r for r in rs if r["event"] == "query" and r.get("part") == "2"]
        eager = [(r["perspective"], r["summary"]["total"] / MB) for r in st if r["system"] == "eager"]
        ad_mb = [r["summary"]["total"] / MB for r in st if r["system"] == "adaptive"][-1]
        ax = axes[0][col]
        bottom = 0.0
        for i, (p, mb) in enumerate(eager):
            ax.bar(0, mb, bottom=bottom, color=PERSP_COLOR[p], edgecolor="white",
                   linewidth=1.5, width=0.6, label=f"eager: {PERSP_SHORT[p]}")
            bottom += mb
        ax.bar(1, ad_mb, color=ADAPTIVE, width=0.6, label="adaptive (all)")
        ax.text(0, bottom, f"{bottom:.0f}", ha="center", va="bottom", fontsize=7)
        ax.text(1, ad_mb, f"{ad_mb:.0f}", ha="center", va="bottom", fontsize=7)
        ax.set_xticks([0, 1], ["Eager", "Adaptive"])
        ax.set_title(LABEL[ds])
        ax.set_ylim(0, bottom * 1.12)
        ax.legend(fontsize=6, loc="upper left", bbox_to_anchor=(1.0, 1.0))
        persps = [p for p, _ in eager]
        ax2 = axes[1][col]
        w = 0.38
        lat = {}
        for j, p in enumerate(persps):
            a = med([q_time(r) for r in q if r["system"] == "adaptive" and r["perspective"] == p])
            e = med([q_time(r) for r in q if r["system"] == "eager" and r["perspective"] == p])
            lat[p] = {"adaptive_s": a, "eager_s": e}
            ax2.bar(j - w / 2, a or 0, w, color=ADAPTIVE, label="Adaptive" if j == 0 else None)
            ax2.bar(j + w / 2, e or 0, w, color=EAGER, label="Eager" if j == 0 else None)
            if e is None:
                ax2.text(j + w / 2, 1, "timeout", rotation=90, ha="center", va="bottom", fontsize=6, color=TEXT2)
        ax2.set_yscale("log")
        ax2.yaxis.set_major_locator(matplotlib.ticker.LogLocator(subs=(1.0, 2.0, 5.0)))
        ax2.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}"))
        ax2.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax2.set_xticks(range(len(persps)), [PERSP_SHORT[p] for p in persps], rotation=20)
        rows[ds] = {"eager_mb_by_perspective": dict(eager), "eager_mb_total": bottom,
                    "adaptive_mb": ad_mb, "median_latency_s": lat}
    axes[0][0].set_ylabel("Storage (MB)")
    axes[1][0].set_ylabel("Median latency (s, log)")
    axes[1][0].legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(FIGS / "s2_exp1_perspectives.pdf")
    plt.close(fig)
    summary["exp1_part2"] = rows


P3_DS = ["bpic2015", "bpic2012", "bpic2017"]
P3_SYSTEMS, P3_VARIANTS = ("eager", "adaptive"), ("embedded", "join")
P3_NAME = {"eager": "SIESTA", "adaptive": "SIESTA-MP"}     # eager here is case-only
P3_VAR_NAME = {"embedded": "embedded", "join": "join-back"}
# Median latencies (s) supplied by hand where the part3attr run has no valid
# query: the BPIC 2017 eager embedded ingest wrote no pairs index.
P3_LATENCY_GIVEN = {("bpic2017", "eager", "embedded"): 51.1}


def _p3_valid(r):
    """A finished query that returned the true count (timeouts are ignored)."""
    return not r.get("timed_out") and r.get("matched_groups") == r.get("truth")


def _p3_pair_mb(st, sysn, var):
    key = "pairs_index" if sysn == "eager" else "adaptive_pairs"
    s = [r for r in st if r["system"] == sysn and r.get("variant") == var]
    return s[-1]["summary"].get(key, 0) / MB if s else 0.0


def exp1_part3(summary):
    """
    Paper style (EXP5_STYLE), panels stacked for single-column width: pair-table
    storage (top) and median latency (bottom), per dataset one bar per (system,
    attribute layout).  A layout with no valid query is drawn as n/a.
    """
    rows = {}
    bars = [(sysn, var) for sysn in P3_SYSTEMS for var in P3_VARIANTS]
    w = 0.2
    with plt.rc_context(EXP5_STYLE):
        fig, (ax_s, ax_l) = plt.subplots(2, 1, figsize=(4.2, 4.0), sharex=True)
        for i, ds in enumerate(P3_DS):
            rs = read(E1 / f"{ds}.part3attr.jsonl")
            st = [r for r in rs if r["event"] == "storage"]
            q = [r for r in rs if r["event"] == "query"]
            old_st = [r for r in read(E1 / f"{ds}.part3.jsonl") if r["event"] == "storage"]
            size, lat, timeouts, wrong, earlier = {}, {}, {}, {}, []
            for sysn, var in bars:
                qs = [r for r in q if r["system"] == sysn and r.get("variant") == var]
                size[(sysn, var)] = _p3_pair_mb(st, sysn, var)
                if not size[(sysn, var)]:
                    # ingest wrote no pairs index: the same index's size from the earlier run
                    size[(sysn, var)] = _p3_pair_mb(old_st, sysn, var) or None
                    earlier.append(f"{sysn}/{var}")
                lat[(sysn, var)] = med([r["time"] for r in qs if _p3_valid(r)])
                if lat[(sysn, var)] is None:
                    lat[(sysn, var)] = P3_LATENCY_GIVEN.get((ds, sysn, var))
                timeouts[(sysn, var)] = sum(1 for r in qs if r.get("timed_out"))
                wrong[(sysn, var)] = sum(1 for r in qs if not r.get("timed_out") and not _p3_valid(r))
            # per-query join/embedded latency ratio, over queries valid in both layouts
            ratio = {}
            for sysn in P3_SYSTEMS:
                by = {var: {r["qid"]: r["time"] for r in q if r["system"] == sysn and r.get("variant") == var
                            and _p3_valid(r)} for var in P3_VARIANTS}
                rr = [by["join"][k] / by["embedded"][k] for k in sorted(by["embedded"].keys() & by["join"].keys())]
                ratio[sysn] = {"n": len(rr), "geomean": statistics.geometric_mean(rr) if rr else None,
                               "min": min(rr, default=None), "max": max(rr, default=None)}
            for ax, data in ((ax_s, size), (ax_l, lat)):
                for k, (sysn, var) in enumerate(bars):
                    v = data[(sysn, var)]
                    x = i + (k - 1.5) * w
                    c = EAGER if sysn == "eager" else ADAPTIVE
                    style = (dict(color=c, edgecolor=c) if var == "embedded"
                             else dict(facecolor=SURFACE, edgecolor=c, hatch="/////"))
                    ax.bar(x, v or 0, w * 0.92, linewidth=0.9, **style,
                           label=f"{P3_NAME[sysn]}, {P3_VAR_NAME[var]}" if i == 0 else None)
                    if v is None:
                        ax.text(x, 0.02, "n/a", transform=ax.get_xaxis_transform(), rotation=90,
                                ha="center", va="bottom", fontsize=7, color=TEXT2)
                    else:
                        ax.text(x, v * 1.08, f"{v:.0f}" if v >= 10 else f"{v:.1f}",
                                ha="center", va="bottom", fontsize=7)
            key = lambda d: {f"{a}/{b}": v for (a, b), v in d.items()}  # noqa: E731
            rows[ds] = {"pair_storage_mb": key(size), "pair_storage_from_earlier_run": earlier,
                        "median_latency_s": key(lat), "timeouts": key(timeouts), "wrong": key(wrong),
                        "join_over_embedded_per_query": ratio}
        ax_s.set_ylabel("Pair-table storage (MB)")
        ax_l.set_ylabel("Median latency (s)")
        for ax, bottom in ((ax_s, 1.0), (ax_l, 0.5)):
            ax.set_yscale("log")
            ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}"))
            ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
            ax.set_ylim(bottom, ax.get_ylim()[1] * 2.5)     # fixed base; headroom for the labels
        ax_l.set_xticks(range(len(P3_DS)), [LABEL[d] for d in P3_DS])
        ax_l.set_xlim(-0.5, len(P3_DS) - 0.5)
        fig.tight_layout(rect=(0, 0, 1, 0.90), pad=0.3)
        h, l = ax_s.get_legend_handles_labels()
        fig.legend(h, l, loc="upper center", bbox_to_anchor=(0.5, 1.0), ncol=2, fontsize=8,
                   labelspacing=0.2, columnspacing=0.8, handlelength=1.6, handletextpad=0.4,
                   borderaxespad=0.1, borderpad=0.1)
        fig.savefig(FIGS / "s2_exp1_attributes.pdf")
        plt.close(fig)
    summary["exp1_part3"] = rows


# ---------------------------------------------------------------------------
# Exp 3
# ---------------------------------------------------------------------------

def _persisted(cat):
    out = set()
    for pid, pv in (cat.get("perspectives") or {}).items():
        for k, v in (pv.get("pairs", {}) if isinstance(pv, dict) else {}).items():
            if isinstance(v, dict) and v.get("status") == "PERSISTENT":
                out.add((pid, k))
    return out


def _sens(path: Path) -> dict:
    rs = read(path)
    q = [r for r in rs if r["event"] == "query"]
    snaps = [_persisted(r["catalog"]) for r in rs if r["event"] == "catalog"]
    src = collections.Counter(s for r in q for s in (r.get("pair_sources") or {}).values())
    hist = collections.defaultdict(list)
    allp = set().union(*snaps) if snaps else set()
    for s in snaps:
        for p in allp:
            hist[p].append(p in s)
    repromo = sum(1 for h in hist.values() for i in range(1, len(h) - 1) if h[i - 1] and not h[i] and any(h[i + 1:]))
    maint = sum(sum(v.get("total_s", 0) for v in ((r.get("timings") or {}).get("maintenance") or {}).values())
                for r in rs if r["event"] == "ingest" and r.get("batch", 0) > 0)
    tot = sum(src.values()) or 1
    return {"queries": len(q), "median_s": med([r.get("time") for r in q]),
            "mean_s": statistics.mean([r["time"] for r in q if r.get("time")]) if q else None,
            "delta_share": src.get("DELTA", 0) / tot, "sources": dict(src),
            "promotions": sum(len(b - a) for a, b in zip(snaps, snaps[1:])),
            "demotions": sum(len(a - b) for a, b in zip(snaps, snaps[1:])),
            "re_promotions": repromo, "maintenance_s": maint, "snapshots": snaps,
            "records": rs}


def exp3(summary):
    out = {"eps": {}, "eps2": {}, "cost": {}}
    for v in ("0", "0.05", "0.15", "0.3", "0.5"):
        for sweep in ("eps", "eps2"):
            s = _sens(E3 / f"bpic2017.{sweep}_{v}.jsonl")
            out[sweep][v] = {k: s[k] for k in s if k not in ("snapshots", "records")}
    cost = {}
    for v in ("0.25", "0.5", "1", "2", "4"):
        f = E3 / ("bpic2017.eps_0.15.jsonl" if v == "1" else f"bpic2017.cost_{v}.jsonl")
        cost[v] = _sens(f)
        out["cost"][v] = {k: cost[v][k] for k in cost[v] if k not in ("snapshots", "records")}
    drift = _sens(E3 / "bpic2017.drift_0.15.jsonl")
    out["drift"] = {k: drift[k] for k in drift if k not in ("snapshots", "records")}
    summary["exp3"] = out

    # Paper style (EXP5_STYLE), two narrow panels side by side at single-column
    # width: (a) cost misestimation, Delta share; (b) drift of the hot perspective.
    with plt.rc_context(EXP5_STYLE):
        fig, (a, b) = plt.subplots(1, 2, figsize=(4.2, 1.9), gridspec_kw={"width_ratios": [1, 1.35]})
        xs = [float(v) for v in cost]
        a.plot(xs, [cost[v]["delta_share"] * 100 for v in cost], "-o", color=ADAPTIVE,
               markersize=3.5, linewidth=1.4)
        a.set_xscale("log", base=2)
        a.set_xticks(xs, [f"{x:g}" for x in xs])
        a.xaxis.set_minor_locator(matplotlib.ticker.NullLocator())
        a.set_ylim(0, 100)
        a.set_ylabel("From Delta (%)")
        a.set_xlabel("Cost scale (\u00d7)")
        a.text(-0.42, 1.02, "(a)", transform=a.transAxes, fontweight="bold", va="bottom")
        # drift: persisted pairs per perspective, after each phase and each ingest;
        # phases labelled, ingests as unlabelled ticks in between
        cats = [r for r in drift["records"] if r["event"] == "catalog"]
        pids = sorted({p for s in drift["snapshots"] for p, _ in s})
        for pid in pids:
            ys = [sum(1 for p, _ in s if p == pid) for s in drift["snapshots"]]
            name = "case" if pid == "trace_id" else pid
            b.plot(range(len(ys)), ys, "-o", color=PERSP_COLOR.get(name, TEXT2), markersize=3,
                   linewidth=1.2, label=PERSP_SHORT.get(pid, pid))
        phase = [i for i, c in enumerate(cats) if c.get("after") == "phase"]
        b.set_xticks(phase, [f"$\\Phi_{{{cats[i]['phase'] + 1}}}$" for i in phase])
        b.set_xticks([i for i in range(len(cats)) if i not in phase], minor=True)
        b.set_xlim(-0.4, len(cats) - 0.6)
        b.set_ylabel("Persisted pairs")
        b.set_xlabel("Query phase (ingests between)")
        b.text(-0.28, 1.02, "(b)", transform=b.transAxes, fontweight="bold", va="bottom")
        fig.tight_layout(rect=(0, 0, 1, 0.84), pad=0.3, w_pad=1.0)
        h, l = b.get_legend_handles_labels()
        fig.legend(h, l, loc="upper center", bbox_to_anchor=(0.5, 1.0), ncol=4, fontsize=8,
                   columnspacing=0.8, handlelength=1.6, handletextpad=0.4, borderaxespad=0.1, borderpad=0.1)
        fig.savefig(FIGS / "s2_exp3_sensitivity.pdf")
        plt.close(fig)


# ---------------------------------------------------------------------------
# Exp 4 (LPG only, current query sets)
# ---------------------------------------------------------------------------

def exp4_lpg(summary):
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.3), sharey=True)
    out = {}
    for ax, kind in zip(axes, ("structural", "attribute")):
        for ds in E4_DS:
            rs = [r for r in read(E4 / "lpg" / f"{ds}.case.stepwise.jsonl") if r.get("qid") and r["kind"] == kind]
            by = collections.defaultdict(list)
            for r in rs:
                if r.get("time_s") is not None and not r.get("timed_out"):
                    by[r["length"]].append(r["time_s"])
            Ls = sorted(by)
            m = [statistics.median(by[L]) for L in Ls]
            lo = [sorted(by[L])[len(by[L]) // 4] for L in Ls]
            hi = [sorted(by[L])[(3 * len(by[L])) // 4] for L in Ls]
            ax.plot(Ls, m, "-", marker=DS_MARKER[ds], color=DS_COLOR[ds], label=LABEL[ds])
            ax.fill_between(Ls, lo, hi, color=DS_COLOR[ds], alpha=0.12, linewidth=0)
            out.setdefault(ds, {})[kind] = {str(L): v for L, v in zip(Ls, m)}
        ax.set_yscale("log")
        ax.set_xlabel("Pattern length")
        ax.set_title("Structural" if kind == "structural" else "Attribute-aware (literal predicates)")
    axes[0].set_ylabel("LPG latency (s, log), median and IQR")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", bbox_to_anchor=(0.5, -0.06), ncol=5, fontsize=7)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.savefig(FIGS / "s2_exp4_lpg_length.pdf")
    plt.close(fig)
    parity = {}
    for ds in E4_DS:
        rs = [r for r in read(E4 / "lpg" / f"{ds}.case.stepwise.jsonl") if r.get("qid")]
        parity[ds] = {"queries": len(rs), "correct": sum(1 for r in rs if r.get("parity")),
                      "timeouts": sum(1 for r in rs if r.get("timed_out")),
                      "median_s": med([r.get("time_s") for r in rs if not r.get("timed_out")])}
    summary["exp4_lpg"] = {"by_length": out, "overall": parity}


def main() -> None:
    FIGS.mkdir(parents=True, exist_ok=True)
    TABLES.mkdir(parents=True, exist_ok=True)
    summary: dict = {}
    for fn in (exp1_part1, exp1_untouched, exp1_part2, exp1_part3, exp3, exp4_lpg):
        fn(summary)
        print("ok", fn.__name__)
    (TABLES / "s2_summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print("figures ->", FIGS, "| summary ->", TABLES / "s2_summary.json")


if __name__ == "__main__":
    main()
