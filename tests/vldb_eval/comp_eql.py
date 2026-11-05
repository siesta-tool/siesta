"""
tests/vldb_eval/comp_eql.py — Elasticsearch with its native sequence language
(EQL) on the long complex-pattern workload (pattern_long).

Index ``<ds>_ev``: one document per canonical event: trace_id, activity,
ts (date, epoch millis; EQL sequences match nothing on epoch_second), idx (row of the canonical frame, sorted by
(trace_id, position)), position, and every attribute as a keyword field
``a_<attr>`` (pattern_long.es_field).  EQL orders events by ``ts`` with
``idx`` as tiebreaker, which is the benchmark's group order for every
perspective: (ts, trace_id, position).

Query: ``sequence by <key> with maxspan=3650d [any where ...] ![any where ...] ...``
(key: trace_id, a_Action or a_org_resource).  Patterns with ``?`` or ``+``
have no EQL equivalent and are recorded as ``unsupported``.  The matching
groups are the distinct join keys of the returned sequences; they are all
fetched (``size``), like the other systems return all matching groups.
The search is asynchronous with ``wait_for_completion_timeout``; a search
still running at the timeout is deleted and recorded as timed out.

Output: results/exp4_patterns/eql/long20/<ds>.<perspective>.jsonl

Usage:
    python -m tests.vldb_eval.comp_eql --setup --limit 4
    python -m tests.vldb_eval.comp_eql
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import requests

from tests.vldb_eval import pattern_common as pc
from tests.vldb_eval import pattern_long as pl
from tests.vldb_eval.eval_common import RESULTS_DIR, run_meta

ES = "http://localhost:9200"
OUT_DIR = RESULTS_DIR / "exp4_patterns" / "eql" / pl.SUITE
TIMEOUT_S = 200.0
MAX_SEQUENCES = 100_000
# EQL fetches every event of the returned sequences through the search
# window: sequences x stages events, more than MAX_SEQUENCES on long patterns.
MAX_RESULT_WINDOW = 10_000_000


def index_name(ds: str) -> str:
    return f"{ds}_ev"


def es(method: str, path: str, body=None, timeout: float = 600, ok=(200, 201)):
    r = requests.request(method, ES + path, json=body, timeout=timeout)
    if r.status_code not in ok:
        raise RuntimeError(f"{method} {path}: {r.status_code} {r.text[:500]}")
    return r.json() if r.text else {}


def setup(ds: str) -> dict:
    df = pc.load_events(ds)
    attrs = pc.attribute_columns(df)
    idx = index_name(ds)
    # one ELK index at a time: drop every other index on the node
    for other in es("GET", "/_cat/indices?h=index&format=json"):
        if not other["index"].startswith("."):
            requests.delete(f"{ES}/{other['index']}", timeout=120)
    props = {"trace_id": {"type": "keyword"}, "activity": {"type": "keyword"},
             "ts": {"type": "date", "format": "epoch_millis"}, "idx": {"type": "long"},
             "position": {"type": "integer"}}
    props.update({pl.es_field(a): {"type": "keyword"} for a in attrs})
    es("PUT", f"/{idx}", {"settings": {"number_of_shards": 1, "number_of_replicas": 0,
                                       "refresh_interval": "-1", "max_result_window": MAX_RESULT_WINDOW},
                          "mappings": {"dynamic": "strict", "properties": props}})
    t0 = time.perf_counter()
    cols = {a: df[a].to_numpy(dtype=object) for a in attrs}
    tid, act = df["trace_id"].to_numpy(dtype=object), df["activity"].to_numpy(dtype=object)
    ts, pos = df["ts"].to_numpy(), df["position"].to_numpy()
    batch = []
    for i in range(len(df)):
        doc = {"trace_id": tid[i], "activity": act[i], "ts": int(ts[i]) * 1000, "idx": i, "position": int(pos[i])}
        for a, c in cols.items():
            v = c[i]
            if isinstance(v, str):
                doc[pl.es_field(a)] = v
        batch.append('{"index":{}}')
        batch.append(json.dumps(doc))
        if len(batch) >= 20_000 or i == len(df) - 1:
            r = requests.post(f"{ES}/{idx}/_bulk", data="\n".join(batch) + "\n",
                              headers={"Content-Type": "application/x-ndjson"}, timeout=600)
            r.raise_for_status()
            if r.json().get("errors"):
                raise RuntimeError(str(r.json()["items"][:2])[:500])
            batch = []
    bulk_s = time.perf_counter() - t0
    es("PUT", f"/{idx}/_settings", {"index": {"refresh_interval": "1s"}})
    es("POST", f"/{idx}/_refresh")
    es("POST", f"/{idx}/_forcemerge?max_num_segments=1", timeout=3600)
    st = es("GET", f"/{idx}/_stats/store,docs")["_all"]["primaries"]
    return {"index": idx, "docs": st["docs"]["count"], "size_bytes": st["store"]["size_in_bytes"],
            "ingest_s": round(time.perf_counter() - t0, 1), "bulk_s": round(bulk_s, 1)}


def run_query(ds: str, q: dict, timeout_s: float) -> dict:
    text = pl.eql(q)
    if text is None:
        return {"unsupported": True}
    body = {"query": text, "timestamp_field": "ts", "tiebreaker_field": "idx",
            "event_category_field": "activity", "size": MAX_SEQUENCES,
            "wait_for_completion_timeout": f"{int(timeout_s)}s", "keep_on_completion": True,
            "keep_alive": "5m"}
    t0 = time.perf_counter()
    try:
        r = es("POST", f"/{index_name(ds)}/_eql/search", body, timeout=timeout_s + 60)
    except Exception as exc:        # noqa: BLE001
        return {"time_s": time.perf_counter() - t0, "error": str(exc)[:300]}
    dt = time.perf_counter() - t0
    if r.get("is_running"):
        try:
            es("DELETE", f"/_eql/search/{r['id']}", timeout=30, ok=(200, 404))
        except Exception:           # noqa: BLE001
            pass
        return {"time_s": dt, "timed_out": True}
    if r.get("id"):
        try:
            es("DELETE", f"/_eql/search/{r['id']}", timeout=30, ok=(200, 404))
        except Exception:           # noqa: BLE001
            pass
    seqs = r.get("hits", {}).get("sequences", [])
    groups = {(s.get("join_keys") or [None])[0] for s in seqs}      # the group (first join key)
    return {"time_s": dt, "matched_groups": len(groups), "sequences": len(seqs),
            "es_took_ms": r.get("took"), "capped": len(seqs) >= MAX_SEQUENCES}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--perspectives", nargs="+", default=pl.PERSPECTIVES)
    ap.add_argument("--dataset", default=pl.DS)
    ap.add_argument("--queries", default=None, help="query JSONL instead of pattern_long's (all perspectives)")
    ap.add_argument("--setup", action="store_true", help="(re)build the event index")
    ap.add_argument("--setup-only", action="store_true", help="build the index and exit")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--qids", nargs="*")
    ap.add_argument("--timeout", type=float, default=TIMEOUT_S)
    ap.add_argument("--skip-after", type=int, default=pc.SKIP_AFTER)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()
    ds = args.dataset
    st = setup(ds) if args.setup else {"index": index_name(ds), "reused": True}
    print("setup", st, flush=True)
    if args.setup_only:
        return
    out_dir = Path(args.out_dir) if args.out_dir else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    node = es("GET", "/_nodes/stats/jvm")["nodes"]
    heap = next(iter(node.values()))["jvm"]["mem"]["heap_max_in_bytes"]
    for persp in args.perspectives:
        if args.queries:
            with open(args.queries) as fq:
                qs = [q for q in (json.loads(l) for l in fq if l.strip()) if q["perspective"] == persp]
        else:
            qs = pl.load(ds, persp)
        if args.qids:
            qs = [q for q in qs if q["qid"] in set(args.qids)]
        if args.limit is not None:
            qs = qs[:args.limit]
        cutoff = pc.SkipAfter(args.skip_after)
        with (out_dir / f"{ds}.{pc.safe(persp)}.jsonl").open("w") as f:
            f.write(json.dumps({"event": "setup", "system": "elk-eql", "dataset": ds, "perspective": persp,
                                **st, "heap_bytes": heap, "timeout_s": args.timeout, "meta": run_meta()}) + "\n")
            warm = next((q for q in qs if pl.eql(q)), None)
            if warm:
                run_query(ds, warm, args.timeout)      # warm-up, discarded
            for q in qs:
                base = {"system": "elk-eql", "qid": q["qid"], "dataset": ds, "perspective": persp,
                        "length": q["length"], "kind": q["kind"], "ops": q["ops"],
                        "truth": q["truth"]["matched_groups"]}
                if pl.eql(q) is None:
                    rec = {**base, "unsupported": True, "time_s": None, "matched_groups": None,
                           "parity": None, "timed_out": False, "error": "not expressible in EQL (? or +)"}
                elif cutoff.skip(q):
                    rec = pc.skipped_record("elk-eql", q)
                else:
                    r = run_query(ds, q, args.timeout)
                    n = r.get("matched_groups")
                    rec = {**base, "time_s": r.get("time_s"), "matched_groups": n,
                           "parity": None if n is None else n == base["truth"],
                           "timed_out": bool(r.get("timed_out")), "error": r.get("error"),
                           "sequences": r.get("sequences"), "es_took_ms": r.get("es_took_ms"),
                           "capped": r.get("capped")}
                    cutoff.record(q, rec["timed_out"])
                f.write(json.dumps(rec) + "\n")
                f.flush()
                print(f"[{persp}] {q['qid']} {rec.get('time_s')} n={rec.get('matched_groups')} "
                      f"truth={base['truth']} {'UNSUPPORTED' if rec.get('unsupported') else ''}"
                      f"{' MISMATCH' if rec.get('parity') is False else ''}{' TIMEOUT' if rec.get('timed_out') else ''}",
                      flush=True)


if __name__ == "__main__":
    main()
