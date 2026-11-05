"""
tests/vldb_eval/plot_long_combined.py — paper latency figures for the long
complex-pattern experiments, up to length 20.

  s2_logs_latency.pdf     s2_bgl_noq_latency and s2_hdfs_noq_latency in one
      figure: BGL and HDFS (pattern_bgl, lengths 10-50, case only); panels
      Structural (OR and negation, no ``?`` / ``+``) and Attribute-aware;
      colour = system (SIESTA-MP, LPG, ELK, MR), marker and line style =
      dataset.
  s2_bpic_logs_latency.pdf    the same on BPIC 2011 / 2017 / 2018 (pattern_bgl
      suite bpiclong; SIESTA-MP and ELK only, run with a 300 s timeout).
  s2_long20_combined_latency.pdf     BPIC 2017 (pattern_long, lengths 10-20);
      one panel per perspective (case, Action, org:resource); SIESTA-MP, LPG
      (plain Cypher; case only), LPG (oracle) (the hand-written step-by-step
      Cypher) and ELK.  Plain LPG was run on case only (on Action its
      sample query timed out); on Action and org:resource it is drawn as
      failed at every length.

Per length: the median over the queries (binding queries dropped, queries a
system cannot express left out), counting every unanswered query (timeout,
out of memory, crash, wrong) as above the timeout.  A median that is a
failure is drawn in the band above the timeout line, one row per system.
The runs stopped a query at 200 s; the timeout line is drawn at 300 s
(PLOT_TIMEOUT_S), the timeout of the paper's other experiments.

Writes results/figures/<name>.pdf and results/tables/<name>.json.

Usage:
    python -m tests.vldb_eval.plot_long_combined
"""

from __future__ import annotations

import collections
import csv
import json
import math
import statistics

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

from tests.vldb_eval.plot_indexing import EXP5_STYLE, SERIES, TEXT2  # noqa: E402
from tests.vldb_eval.plot_long20 import E, FIGS, TABLES, TIMEOUT_S, outcome, read  # noqa: E402

MAX_LEN = 20          # BPIC 2017 figure
LOG_MAX_LEN = 50      # logs figure
PLOT_TIMEOUT_S = 300.0
COLOR = {"siesta": SERIES[0], "lpg": SERIES[1], "lpg_oracle": SERIES[6], "elk": SERIES[2], "mr": SERIES[4]}
LABEL = {"siesta": "SIESTA-MP", "lpg": "LPG", "lpg_oracle": "LPG (oracle)", "elk": "ELK", "mr": "MR"}

# logs: {system: {dataset: result files}}
LOG_FILES = {
    "siesta": {"bgl": ["siesta/bgl/bgl.case.jsonl", "siesta/bgl/bgl.case.attribute.jsonl",
                       "siesta/bgl/bgl.case.snoq.jsonl"],
               "hdfs": ["siesta/hdfs/hdfs.case.jsonl"]},
    "lpg": {"bgl": ["lpg/bgl/bgl.case.jsonl"], "hdfs": ["lpg/hdfs/hdfs.case.jsonl"]},
    "elk": {"bgl": ["eql/bgl/bgl.case.jsonl", "eql/bgl_attr/bgl.case.jsonl", "eql/bgl_snoq/bgl.case.jsonl"],
            "hdfs": ["eql/hdfs/hdfs.case.jsonl"]},
    "mr": {"bgl": ["mr/bgl/bgl.case.jsonl", "mr/bgl/bgl.case.snoq.jsonl"], "hdfs": ["mr/hdfs/hdfs.case.jsonl"]},
}
LOG_DATASETS = [("bgl", "BGL", "s", "-"), ("hdfs", "HDFS", "D", "--")]
LOG_KINDS = [(("structural_noq",), "Structural"), (("attribute",), "Attribute-aware")]
DX = {"bgl": -1.2, "hdfs": 1.2}       # failed medians of the logs at one length sit side by side

# the same experiment on BPIC logs (pattern_bgl suite bpiclong; SIESTA-MP and ELK, run at 300 s)
BPIC_FILES = {k: {ds: [f"{d}/bpiclong/{ds}.case.jsonl"] for ds in ("bpic2011", "bpic2017", "bpic2018")}
              for k, d in (("siesta", "siesta"), ("elk", "eql"))}
BPIC_DATASETS = [("bpic2011", "BPIC 2011", "o", "-"), ("bpic2017", "BPIC 2017", "^", "--"),
                 ("bpic2018", "BPIC 2018", "v", ":")]
BPIC_DX = {"bpic2011": -1.6, "bpic2017": 0.0, "bpic2018": 1.6}

# BPIC 2017: {system: file pattern}, {p} = perspective file name
LONG20_FILES = {"siesta": "siesta/long20/bpic2017.{p}.jsonl", "lpg": "lpg/long20/bpic2017.{p}.jsonl",
                "lpg_oracle": "lpg/long20/bpic2017.{p}.stepwise.jsonl", "elk": "eql/long20/bpic2017.{p}.jsonl"}
PERSPS = [("case", "case"), ("Action", "Action"), ("org:resource", "org_resource")]


def oc(r: dict) -> str:
    """plot_long20's outcome, with a wrong answer counted as a timeout."""
    o = outcome(r)
    return "timeout" if o == "wrong" else o


def medians(paths: list[str], kinds=None, max_len: int = MAX_LEN) -> dict:
    """{length: stats}; ``median_s`` is None when the median is a failure."""
    g = collections.defaultdict(list)
    for p in paths:
        for r in read(E / p):
            if (r.get("kind") != "binding" and (kinds is None or r.get("kind") in kinds)
                    and r["length"] <= max_len and oc(r) != "not expressible"):
                g[r["length"]].append(r)
    out = {}
    for L in sorted(g):
        vals = sorted(r["time_s"] if oc(r) == "correct" else float("inf") for r in g[L])
        m = statistics.median_high(vals)
        fails = collections.Counter(oc(r) for r in g[L] if oc(r) != "correct")
        out[L] = {"n": len(vals), "correct": len(vals) - sum(fails.values()),
                  "median_s": None if m == float("inf") else m, **dict(fails)}
    return out


def draw(ax, by_len: dict, color, marker, ls, row_y, dx=0.0) -> None:
    face = color
    ok = [(L, s["median_s"]) for L, s in by_len.items() if s["median_s"] is not None]
    if ok:
        ax.plot(*zip(*ok), linestyle=ls, marker=marker, color=color, linewidth=1.6, markersize=4,
                markerfacecolor=face)
    bad = [L + dx for L, s in by_len.items() if s["median_s"] is None]
    if bad:
        ax.plot(bad, [row_y] * len(bad), linestyle="none", marker=marker, color=color, markersize=4,
                markerfacecolor=face)


def finish_axes(axes, rows: dict, x0: int, x1: int = MAX_LEN, step: int = 2,
                timeout: float = PLOT_TIMEOUT_S) -> None:
    hi = max(rows.values()) * 1.3
    band = (timeout * hi) ** 0.5
    # lower bound from the data: the decade below the fastest plotted median
    lows = [y for ax in axes for ln in ax.get_lines() if ln.get_linestyle() != "None"
            for y in ln.get_ydata() if y < timeout]
    lo = 10 ** math.floor(math.log10(min(lows) * 0.8)) if lows else 1
    ticks = [t for t in (0.1, 1, 10, 100) if t >= lo]
    for ax in axes:
        ax.axhline(timeout, color=TEXT2, linewidth=0.8, linestyle="--")
        ax.set_yscale("log")
        ax.minorticks_off()
        ax.set_ylim(lo * 0.8, hi)
        ax.set_yticks(ticks + [band], [f"{t:g}" for t in ticks] + ["failed"])
        pad = 0.3 * step
        ax.set_xlim(x0 - pad, x1 + pad)
        ax.set_xticks(range(x0, x1 + 1, step))
        ax.set_xlabel("Pattern length")
    axes[0].set_ylabel("Median latency (s)")
    axes[0].text(x0 - 0.2 * step, timeout * 0.86, f"timeout ({timeout:g} s)", fontsize=8, color=TEXT2,
                 va="top")


def fig_logs(files: dict = LOG_FILES, datasets=LOG_DATASETS, dx: dict = DX,
             name: str = "s2_logs_latency") -> dict:
    keys = list(files)
    stats = {kname: {k: {ds: medians(f, kinds, LOG_MAX_LEN) for ds, f in files[k].items()} for k in keys}
             for kinds, kname in LOG_KINDS}
    failing = [k for k in keys if any(s["median_s"] is None for st in stats.values()
                                      for v in st[k].values() for s in v.values())]
    rows = {k: PLOT_TIMEOUT_S * 1.45 * 1.4 ** i for i, k in enumerate(failing)} or {"": PLOT_TIMEOUT_S * 1.45}
    with plt.rc_context(EXP5_STYLE):
        fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.6), sharey=True)
        for ax, (_, kname) in zip(axes, LOG_KINDS):
            for k in keys:
                for ds, _, marker, ls in datasets:
                    draw(ax, stats[kname][k][ds], COLOR[k], marker, ls, rows.get(k), dx[ds])
            ax.set_title(kname, fontsize=9.5)
        finish_axes(axes, rows, 10, LOG_MAX_LEN, 10)
        handles = [Line2D([], [], color=COLOR[k], linewidth=1.6, label=LABEL[k]) for k in keys] + [
            Line2D([], [], color=TEXT2, linestyle=ls, linewidth=1.2, marker=m, markersize=4.5, label=lab)
            for _, lab, m, ls in datasets]
        fig.tight_layout(rect=(0, 0, 1, 0.9), pad=0.3)
        fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 1.0), ncol=len(handles),
                   fontsize=8, frameon=False, columnspacing=1.2, handlelength=2.2, borderaxespad=0.1)
        fig.savefig(FIGS / f"{name}.pdf")
        plt.close(fig)
    return stats


def fig_long20() -> dict:
    keys = list(LONG20_FILES)
    stats = {persp: {k: medians([LONG20_FILES[k].format(p=pf)]) for k in keys} for persp, pf in PERSPS}
    lengths = sorted(stats["case"]["siesta"])
    for persp, _ in PERSPS:          # plain LPG not run here: drawn as failed at every length
        if not stats[persp]["lpg"]:
            stats[persp]["lpg"] = {L: {"median_s": None, "not_run": "drawn as timeout"} for L in lengths}
    failing = [k for k in keys if any(s["median_s"] is None for st in stats.values() for s in st[k].values())]
    rows = {k: PLOT_TIMEOUT_S * 1.45 * 1.4 ** i for i, k in enumerate(failing)}
    with plt.rc_context(EXP5_STYLE):
        fig, axes = plt.subplots(1, 3, figsize=(7.0, 2.5), sharey=True)
        for ax, (persp, _) in zip(axes, PERSPS):
            for k in keys:
                draw(ax, stats[persp][k], COLOR[k], "o", "-", rows.get(k))
            ax.set_title(persp, fontsize=9.5)
        finish_axes(axes, rows, 10)
        handles = [Line2D([], [], color=COLOR[k], linewidth=1.6, marker="o", markersize=4, label=LABEL[k])
                   for k in keys]
        fig.tight_layout(rect=(0, 0, 1, 0.9), pad=0.3)
        fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 1.0), ncol=len(handles),
                   fontsize=8, frameon=False, columnspacing=1.5, handlelength=2.2, borderaxespad=0.1)
        fig.savefig(FIGS / "s2_long20_combined_latency.pdf")
        plt.close(fig)
    return stats


# Attribute-aware complex case-centric queries: per-length medians given as a
# table (results/exp4_patterns/complex_case_attribute_latency.csv), one cell
# per (dataset, system) holding "|"-separated medians for lengths 11-20,
# TO = timeout (OOM or over 200 s).  Columns in file order.
COMPLEX_CSV = E / "complex_case_attribute_latency.csv"
COMPLEX_KEYS = ["siesta", "lpg", "lpg_wise", "elk", "mr"]
COMPLEX_LABEL = {**LABEL, "lpg_wise": "Upper-bound (LPG)"}
COMPLEX_LENGTHS = list(range(11, 21))
COMPLEX_TIMEOUT_S = 200.0
COMPLEX_DS = {     # dataset: (colour, marker)
    "BPIC 2011": (SERIES[0], "o"), "BPIC 2012": (SERIES[1], "s"), "BPIC 2015": (SERIES[2], "^"),
    "BPIC 2017": (SERIES[3], "D"), "BPIC 2018": (SERIES[4], "v"), "BGL": (SERIES[6], "P"),
    "HDFS": (SERIES[7], "X"),
}


def complex_medians(path=COMPLEX_CSV) -> dict:
    """{dataset: {system: {length: {"median_s": s or None}}}} from the table."""
    out = {}
    for row in csv.reader(path.read_text().splitlines()[2:]):
        if not row or not row[0]:
            continue
        out[row[0]] = {k: {L: {"median_s": None if v.strip() == "TO" else float(v)}
                           for L, v in zip(COMPLEX_LENGTHS, cell.split("|"))}
                       for k, cell in zip(COMPLEX_KEYS, row[1:])}
    return out


def fig_complex(name: str = "s2_complex_attr_latency") -> dict:
    """
    One panel per system, one line per dataset (colour and marker), in one row.
    A timed-out median is drawn in the band above the timeout line; the
    datasets' failures at one length sit side by side.
    """
    stats = complex_medians()
    dss = list(stats)
    dx = {ds: (j - (len(dss) - 1) / 2) * 0.13 for j, ds in enumerate(dss)}
    rows = {"": COMPLEX_TIMEOUT_S * 2.2}     # failed band, clear of the timeout line
    with plt.rc_context(EXP5_STYLE):
        fig, axes = plt.subplots(1, len(COMPLEX_KEYS), figsize=(7.0, 2.05), sharey=True)
        for ax, k in zip(axes, COMPLEX_KEYS):
            for ds in dss:
                c, m = COMPLEX_DS[ds]
                draw(ax, stats[ds][k], c, m, "-", rows[""], dx[ds])
            for ln in ax.get_lines():
                ln.set_markersize(3)
                ln.set_linewidth(1.2)
            ax.set_title(COMPLEX_LABEL[k], fontsize=9.5)
        finish_axes(list(axes), rows, COMPLEX_LENGTHS[0], COMPLEX_LENGTHS[-1], 3, timeout=COMPLEX_TIMEOUT_S)
        axes[0].texts[-1].remove()          # the timeout label goes in the legend instead
        handles = [Line2D([], [], color=c, linewidth=1.2, marker=m, markersize=3.5, label=ds)
                   for ds, (c, m) in COMPLEX_DS.items() if ds in stats]
        handles.append(Line2D([], [], color=TEXT2, linewidth=0.8, linestyle="--",
                              label=f"timeout ({COMPLEX_TIMEOUT_S:g} s)"))
        fig.tight_layout(rect=(0, 0, 1, 0.87), pad=0.3, w_pad=0.4)
        fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 1.0), ncol=len(handles),
                   fontsize=8, frameon=False, columnspacing=0.9, handlelength=1.8, handletextpad=0.4,
                   borderaxespad=0.1)
        fig.savefig(FIGS / f"{name}.pdf")
        plt.close(fig)
    return stats


def fig_complex_by_dataset(name: str = "s2_complex_attr_latency_by_dataset") -> dict:
    """The same table at single-column width: one panel per dataset, in pairs
    stacked vertically (4 rows of 2, the legend in the eighth slot), colour =
    system; failed medians in one band row per system."""
    stats = complex_medians()
    dss = list(stats)
    color = {**COLOR, "lpg_wise": COLOR["lpg_oracle"]}
    failing = [k for k in COMPLEX_KEYS if any(s["median_s"] is None for st in stats.values()
                                              for s in st[k].values())]
    rows = {k: COMPLEX_TIMEOUT_S * 1.45 * 1.4 ** i for i, k in enumerate(failing)}
    with plt.rc_context(EXP5_STYLE):
        fig, grid = plt.subplots(4, 2, figsize=(4.2, 5.0), sharex=True, sharey=True)
        cells = list(grid.flat)
        axes = cells[:len(dss)]
        for ax, ds in zip(axes, dss):
            for k in COMPLEX_KEYS:
                draw(ax, stats[ds][k], color[k], "o", ":" if k == "lpg_wise" else "-", rows.get(k))
            ax.set_title(ds, fontsize=9.5, pad=3)
        finish_axes(axes, rows, COMPLEX_LENGTHS[0], COMPLEX_LENGTHS[-1], 3, timeout=COMPLEX_TIMEOUT_S)
        axes[0].texts[-1].remove()          # the timeout label goes in the legend instead
        # x labels on the bottom row only (the axis is shared; the legend fills the last cell)
        lows = [y for ax in axes for ln in ax.get_lines() if ln.get_linestyle() != "None"
                for y in ln.get_ydata()]
        for i, ax in enumerate(axes):
            ax.set_ylabel("")
            ax.set_xlabel("Pattern length" if i == len(dss) - 1 else "")
            ax.tick_params(labelbottom=i == len(dss) - 1)
            ax.set_ylim(min(lows) * 0.6, ax.get_ylim()[1] * 1.25)    # headroom over the failed band
        fig.supylabel("Median latency (s)", fontsize=9.5)
        legend_ax = cells[len(dss)]
        legend_ax.axis("off")
        handles = [Line2D([], [], color=color[k], linewidth=1.6, marker="o", markersize=4, label=COMPLEX_LABEL[k])
                   for k in COMPLEX_KEYS if k != "lpg_wise"]
        handles.append(Line2D([], [], color=TEXT2, linewidth=0.8, linestyle="--",
                              label=f"timeout ({COMPLEX_TIMEOUT_S:g} s)"))
        # the hand-tuned reference last, dotted
        handles.append(Line2D([], [], color=color["lpg_wise"], linewidth=1.6, linestyle=":", marker="o",
                              markersize=4, label=COMPLEX_LABEL["lpg_wise"]))
        legend_ax.legend(handles=handles, loc="center", fontsize=8, frameon=False, handlelength=2.2,
                         labelspacing=0.3)
        fig.tight_layout(pad=0.3, h_pad=0.5, w_pad=0.6)
        fig.savefig(FIGS / f"{name}.pdf")
        plt.close(fig)
    return stats


def dump(name: str, stats: dict, **extra) -> None:
    (TABLES / f"{name}.json").write_text(json.dumps(
        {"run_timeout_s": TIMEOUT_S, "plotted_timeout_s": PLOT_TIMEOUT_S, "max_length": MAX_LEN,
         "wrong_counted_as": "timeout", "by_length": stats, **extra}, indent=1, default=str))
    for panel, st in stats.items():
        for k, v in st.items():
            for ds, by_len in (v.items() if v and isinstance(next(iter(v)), str) else [("", v)]):
                print(f"{panel:16s} {k:11s} {ds:9s} " + " ".join(
                    f"{L}:{'F' if s['median_s'] is None else round(s['median_s'], 1)}" for L, s in by_len.items()))


def main() -> None:
    FIGS.mkdir(parents=True, exist_ok=True)
    TABLES.mkdir(parents=True, exist_ok=True)
    dump("s2_logs_latency", fig_logs(), max_length=LOG_MAX_LEN)
    if all((E / f).exists() for v in BPIC_FILES.values() for fs in v.values() for f in fs):
        dump("s2_bpic_logs_latency", fig_logs(BPIC_FILES, BPIC_DATASETS, BPIC_DX, "s2_bpic_logs_latency"),
             max_length=LOG_MAX_LEN, run_timeout_s=300.0)
    dump("s2_long20_combined_latency", fig_long20())
    if COMPLEX_CSV.exists():
        dump("s2_complex_attr_latency", fig_complex(), run_timeout_s=200.0)
        fig_complex_by_dataset()


if __name__ == "__main__":
    main()
