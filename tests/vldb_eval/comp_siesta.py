"""
tests/vldb_eval/comp_siesta.py — adaptive SIESTA on the Exp 4 pattern queries.

Same queries and result format as the competitor adapters (see
pattern_common).  Per (dataset, perspective), on a freshly ingested log,
SIESTA is measured WARM only, like the competitors' pre-built indexes: for
each query, one unmeasured run with forced promotion (min_query_count = 1,
which bypasses the cost gate) builds and persists the query's pairs, then
the query is run again and measured (variant ``warm``).  Pairs persisted
for earlier queries stay persisted, as in a pre-built index.  Build /
promotion cost is not part of this experiment.

``time_s`` is SIESTA's server-side latency.  A query exceeding the timeout
(in the promoting or the measured run) is recorded as timed out and the API
restarted (Spark keeps running a job whose HTTP request timed out).

Output: results/exp4_patterns/siesta/<ds>.<perspective>.jsonl

Usage:
    python -m tests.vldb_eval.comp_siesta --datasets bpic2015 --perspectives case
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import requests

from tests.vldb_eval.eval_common import (
    RETENTION, RESULTS_DIR, eval_drain, eval_force_persist, ingest, query_adaptive, restart_api, run_meta,
)
from tests.vldb_eval.pattern_common import (
    PERSPECTIVES, SKIP_AFTER, TIMEOUT_S, SkipAfter, load_queries, safe, skipped_record,
)
from tests.vldb_eval.suite_data import DATA_DIR, prepare

OUT_DIR = RESULTS_DIR / "exp4_patterns" / "siesta"
NO_PROMOTION = {**RETENTION, "min_query_count": 10**9}
FORCE_PROMOTION = {**RETENTION, "min_query_count": 1}
# --warm adaptive: no pre-warming; a pair is persisted at its second use (once a
# use was served without a scan, i.e. it saved something): demand gate 2, cost
# gate off (cost_scale 0), no decay over the run.
ADAPTIVE = {**RETENTION, "min_query_count": 2, "cost_scale": 0.0, "half_life_seconds": 1e12,
            "lru_capacity": 32}   # a smaller LRU keeps the driver within memory
WARMUP_GROUPING = ["lifecycle:transition"]   # JVM warm-up on a perspective outside the workload
QUERY_TIMEOUT_S = TIMEOUT_S  # --timeout
WARM = "promote"             # --warm


def query_pairs(pattern: str) -> list[list[str]]:
    """The pairs SIESTA's planner fetches for ``pattern`` (responded + info pairs)."""
    from siesta.modules.query.parse_seql import extract_info_pairs, extract_responded_pairs
    pairs = {(rp.source.label, rp.target.label) for rp in extract_responded_pairs(pattern)}
    pairs |= {(p[0], p[1]) for p in extract_info_pairs(pattern)}
    return [list(p) for p in sorted(pairs)]


def _persist(log: str, q: dict) -> dict:
    """Warm by building and persisting the query's pairs directly (Spark write, pinned)."""
    try:
        eval_force_persist(log, q["grouping_keys"], query_pairs(q["siesta_pattern"]))
        eval_drain(log)
        return {}
    except requests.exceptions.RequestException as exc:
        restart_api()
        return {"error": f"force_persist: {str(exc)[:280]}"}


def _run(log: str, q: dict, retention: dict, wait: bool = False) -> dict:
    """One query; ``started_at`` / ``ended_at`` (epoch s) locate it against OOM-kill logs."""
    t0 = time.time()
    try:
        r = query_adaptive(log, q["siesta_pattern"], q["grouping_keys"],
                           wait_promotion=wait, retention=retention,
                           timeout=QUERY_TIMEOUT_S + 60)
    except requests.exceptions.Timeout:
        restart_api()
        r = {"timed_out": True}
    except requests.exceptions.RequestException as exc:
        restart_api()
        r = {"error": str(exc)[:300]}
    if r.get("code", 200) != 200:
        r = {"error": str(r)[:300]}
    return {**r, "started_at": t0, "ended_at": time.time()}


def _record(f, q: dict, variant: str, r: dict, rep: int = 0, **extra) -> dict:
    timed_out = bool(r.get("timed_out")) or ((r.get("time") or 0) > QUERY_TIMEOUT_S)
    n = None if timed_out or "error" in r else r.get("matched_groups")
    rec = {
        "system": "siesta", "variant": variant, "qid": q["qid"], "dataset": q["dataset"],
        "perspective": q["perspective"], "length": q["length"], "kind": q["kind"], "rep": rep,
        "time_s": r.get("time"), "wall_s": r.get("wall_s"), "matched_groups": n,
        "truth": q["truth"]["matched_groups"], "parity": n == q["truth"]["matched_groups"] if n is not None else None,
        "timed_out": timed_out, "error": r.get("error"),
        "phases": r.get("timings"), "pair_sources": r.get("pair_sources"),
        "promotion_s": r.get("promotion_s"),
        "started_at": r.get("started_at"), "ended_at": r.get("ended_at"), **extra,
    }
    f.write(json.dumps(rec) + "\n")
    f.flush()
    return rec


def run(ds: str, perspective: str, setup: bool, limit: int | None, qids: set | None,
        skip_after: int = SKIP_AFTER, out: str | None = None, suite: str = "") -> None:
    if suite == "complex":
        from tests.vldb_eval.pattern_complex import load as load_complex
        qs = load_complex(ds, perspective)
    elif suite == "long20":
        from tests.vldb_eval.pattern_long import load as load_long
        qs = load_long(ds, perspective)
    elif suite in ("bgl", "bpiclong"):          # pattern_bgl's query files (suite from its CONFIGS)
        from tests.vldb_eval.pattern_bgl import load as load_bgl
        qs = load_bgl(ds, perspective)
    else:
        qs = load_queries(ds, perspective)
    if qids:
        qs = [q for q in qs if q["qid"] in qids]
    if limit:
        qs = qs[:limit]
    log = ds
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = Path(out) if out else OUT_DIR / suite / f"{ds}.{safe(perspective)}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        restart_api()
        t0 = time.perf_counter()
        csv_path = DATA_DIR / ds / "full.csv"      # a ready log (e.g. scale_data.py) needs no preparation
        if not csv_path.exists():
            csv_path = prepare(ds).full_csv
        ing = ingest("adaptive", log, csv_path, clear_existing=True) if setup else {}
        f.write(json.dumps({"event": "setup", "system": "siesta", "dataset": ds, "perspective": perspective,
                            "ingest_s": ing.get("time"), "wall_s": time.perf_counter() - t0,
                            "retention": ADAPTIVE if WARM == "adaptive" else RETENTION, "warm": WARM,
                            "meta": run_meta()}) + "\n")
        def warm_up() -> None:
            """JVM / perspective L1 warm-up, discarded."""
            if WARM == "adaptive":
                # a perspective the workload does not use: its pair statistics stay cold
                _run(log, {**qs[-1], "grouping_keys": WARMUP_GROUPING}, NO_PROMOTION)
                return
            if WARM == "persist":
                _persist(log, qs[-1])
                _run(log, qs[-1], FORCE_PROMOTION)
            else:
                _run(log, qs[-1], NO_PROMOTION)

        warm_up()

        def attempt(q: dict) -> tuple[dict, bool]:
            """Forced promotion (unmeasured), then the measured warm run."""
            if WARM == "adaptive":
                # one measured run in workload order; background promotions are then
                # drained (unmeasured, recorded) so the next query sees their result
                r = _run(log, q, ADAPTIVE)
                t0 = time.perf_counter()
                try:
                    eval_drain(log)
                except requests.exceptions.RequestException:
                    pass
                r["drain_s"] = time.perf_counter() - t0
                srcs = set((r.get("pair_sources") or {}).values())
                return r, bool(srcs) and srcs <= {"DELTA"}
            if WARM == "persist":
                # The query's pairs are built and persisted by Spark: the
                # promotion path would first collect every pair row of a cold
                # query into the driver (the LRU tier), which exceeds the
                # equal-resource driver budget on BPIC 2017.
                # The measured run must read only persisted pairs (DELTA); if
                # any pair came from elsewhere, persist again and re-measure.
                for _ in range(2):
                    r = _persist(log, q)
                    if "error" in r:
                        return {**r, "stage": "promotion"}, False
                    r = _run(log, q, FORCE_PROMOTION)
                    srcs = set((r.get("pair_sources") or {}).values())
                    if r.get("timed_out") or "error" in r or (srcs and srcs <= {"DELTA"}):
                        break
                return r, bool(srcs) and srcs <= {"DELTA"}
            persisted, r = False, {}
            for _ in range(2):
                r = _run(log, q, FORCE_PROMOTION, wait=True)
                if r.get("timed_out") or "error" in r:
                    return {**r, "stage": "promotion"}, False
                after = (r.get("pair_status_after") or {}).values()
                if after and all(st == "PERSISTENT" for st in after):
                    persisted = True
                    break
            eval_drain(log)
            return _run(log, q, FORCE_PROMOTION), persisted

        cutoff = SkipAfter(skip_after)
        for q in qs:
            if cutoff.skip(q):
                f.write(json.dumps(skipped_record("siesta", q, variant="warm")) + "\n")
                f.flush()
                print(f"  [{ds}/{perspective}] warm {q['qid']} skipped ({skip_after} consecutive timeouts)")
                continue
            r, persisted = attempt(q)
            retried = False
            if "error" in r and not r.get("timed_out"):
                # The driver died (e.g. its container's memory limit) and _run
                # restarted the API: warm the new JVM up, then retry once.
                warm_up()
                r, persisted = attempt(q)
                retried = True
            rec = _record(f, q, "adaptive" if WARM == "adaptive" else "warm", r, all_persisted=persisted,
                          retried=retried, stage=r.get("stage"), drain_s=r.get("drain_s"))
            cutoff.record(q, rec["timed_out"])
            print(f"  [{ds}/{perspective}] warm {q['qid']} {rec['time_s']} parity={rec['parity']} "
                  f"persisted={persisted}{' retried' if retried else ''} "
                  f"{sorted(set((rec['pair_sources'] or {}).values()))}")
        f.write(json.dumps({"event": "done"}) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--datasets", nargs="+", default=["bpic2011", "bpic2012", "bpic2015", "bpic2017", "bpic2018"])
    ap.add_argument("--perspectives", nargs="*", default=None)
    ap.add_argument("--no-setup", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--qids", nargs="*")
    ap.add_argument("--skip-after", type=int, default=SKIP_AFTER,
                    help="consecutive timeouts of a kind before the rest of that kind is skipped (0: off)")
    ap.add_argument("--timeout", type=float, default=TIMEOUT_S, help="per-query timeout (s)")
    ap.add_argument("--out", default=None, help="output path (single dataset/perspective only)")
    ap.add_argument("--suite", default="", choices=["", "complex", "long20", "bgl", "bpiclong"],
                    help="query suite (complex: pattern_complex, long20: pattern_long)")
    ap.add_argument("--warm", default="promote", choices=["promote", "persist", "adaptive"],
                    help="promote: forced promotion through queries; persist: build and persist "
                         "the query's pairs directly (no driver-side collection of a cold scan); "
                         "adaptive: no pre-warming, one measured run per query, persist at 2nd use")
    args = ap.parse_args()
    global QUERY_TIMEOUT_S, WARM
    QUERY_TIMEOUT_S = args.timeout
    WARM = args.warm
    for ds in args.datasets:
        persps = args.perspectives if args.perspectives is not None else ["case"] + PERSPECTIVES.get(ds, [])
        for persp in persps:
            run(ds, persp, not args.no_setup, args.limit, set(args.qids) if args.qids else None,
                args.skip_after, args.out, args.suite)


if __name__ == "__main__":
    main()
