"""
tests/vldb_eval/pattern_bgl.py — long, complex patterns over long validating
traces: BGL (LogHub 2.0; case = Node, activity = EventId, see bgl_data.py).

Lengths 10, 20, ..., 50; per length 5 ``structural``, 5 ``binding`` and 5
``attribute`` queries.

structural  L events picked spread over a witness node of >= MIN_TRACE events
            (one per equal segment, from activities the witness repeats >=
            MIN_REPEAT times), then 2-4 operators from ``X?``, ``X+``,
            ``(X || W)`` and ``A !Y B`` (Y absent from the witness between A and B).
binding     every element (and the negated one) carries ``[Component=$1]``:
            the whole match must share one Component value.  This is the only
            binding EQL can express (a join key on every stage, missing-event
            stages included).  Picked from the witness events of one Component
            value; operators OR and negation only (EQL has no ``?`` / ``+``).

attribute   like binding (OR and negation only, so EQL can express it), but with
            1-3 literal predicates ``X[attr="value"]`` (attr in Component, Level,
            Type; values from the witness's events) and no binding.

A query is kept only if every node it matches has >= MIN_MATCH_LEN events
(BGL: p99 node length 366), so the matches are validated in long traces.

Ground truth: existence semantics in node order; for a binding query a node
matches when, for some value v, the pattern matches the node's events with
Component = v (the negated events included, as EQL's join key and SIESTA's
``!Y[Component=$1]`` require).

Writes results/exp4_patterns/queries/bgl/bgl.case.jsonl

Usage:
    python -m tests.vldb_eval.pattern_bgl generate
"""

from __future__ import annotations

import argparse
import collections
import json
import random
import statistics
import sys
from pathlib import Path

import numpy as np

from tests.vldb_eval import pattern_common as pc
from tests.vldb_eval import pattern_long as pl

DS = "bgl"
SUITE = "bgl"
BIND = "Component"
LENGTHS = [10, 20, 30, 40, 50]
PER_KIND = 5
MIN_TRACE = 1000
MIN_REPEAT = 3
MIN_MATCH_LEN = 500
OPS = ["neg", "opt", "plus", "or"]


# per-dataset settings (configure() switches the module to one of them)
CONFIGS = {
    "bgl": {"suite": "bgl", "bind": "Component", "attrs": ["Component", "Level", "Type"],
            "min_trace": 1000, "min_match_len": 500},
    # HDFS blocks are short (median 19, p99 32, max 297 events): the long traces are the
    # 44 blocks of >= 200 events, and a match must be in a block of >= 100 (~3x p99)
    "hdfs": {"suite": "hdfs", "bind": "Component", "attrs": ["Level", "Component"],
             "min_trace": 200, "min_match_len": 100},
    # BPIC logs (suite bpiclong; kinds structural_noq and attribute only): witnesses and
    # matched traces scaled to each log's trace lengths (p99: 2011 979, 2017 93, 2018 170)
    "bpic2011": {"suite": "bpiclong", "bind": "Section", "attrs": ["Section", "org:group"],
                 "min_trace": 500, "min_match_len": 100},
    "bpic2017": {"suite": "bpiclong", "bind": "Action", "attrs": ["Action", "org:resource", "lifecycle:transition"],
                 "min_trace": 100, "min_match_len": 50},
    "bpic2018": {"suite": "bpiclong", "bind": "doctype", "attrs": ["doctype", "subprocess", "org:resource"],
                 "min_trace": 500, "min_match_len": 100},
}


def configure(ds: str) -> None:
    global DS, SUITE, BIND, ATTRS, MIN_TRACE, MIN_MATCH_LEN
    c = CONFIGS[ds]
    DS, SUITE, BIND, ATTRS = ds, c["suite"], c["bind"], c["attrs"]
    MIN_TRACE, MIN_MATCH_LEN = c["min_trace"], c["min_match_len"]


def query_path(ds: str | None = None, persp: str = "case") -> Path:
    ds = ds or DS
    return pc.QUERY_DIR / CONFIGS.get(ds, {"suite": SUITE})["suite"] / f"{ds}.{pc.safe(persp)}.jsonl"


def load(ds: str | None = None, persp: str = "case") -> list[dict]:
    with query_path(ds, persp).open() as f:
        return [json.loads(l) for l in f if l.strip()]


def siesta_pattern(q: dict) -> str:
    bind = q.get("binding")
    out = []
    for e in q["elements"]:
        acts = e["acts"]
        tok = pl.quote_label(acts[0]) if len(acts) == 1 else "(" + " || ".join(pl.quote_label(a) for a in acts) + ")"
        if bind and len(acts) == 1:
            tok += f"[{bind}=$1]"
        elif e["preds"] and len(acts) == 1:
            tok += "[" + ",".join(f'{p["attr"]}="{p["value"]}"' for p in e["preds"]) + "]"
        elif bind:                       # bind every branch of an alternative
            tok = "(" + " || ".join(f"{pl.quote_label(a)}[{bind}=$1]" for a in acts) + ")"
        tok += {"one": "", "opt": "?", "plus": "+"}[e["quant"]]
        out.append(tok)
        for y in e["neg_after"]:
            out.append("!" + pl.quote_label(y) + (f"[{bind}=$1]" if bind else ""))
    return " ".join(out)


ATTRS = ["Component", "Level", "Type"]


def _hit(e: dict, a: str, row: dict | None) -> bool:
    return a in e["acts"] and all(row is not None and row.get(p["attr"]) == p["value"] for p in e["preds"])


def _match(acts: list[str], req: list[dict], rows: list[dict] | None = None) -> bool:
    """Existence of the required elements in order, with negated gaps (pattern_long.Groups.matches);
    ``rows``: per-event attributes, needed when elements carry literal predicates."""
    m = len(req)
    reach = [True] + [False] * m
    for i, a in enumerate(acts):
        nxt = list(reach)
        for k in range(1, m):
            if reach[k] and a in req[k - 1]["neg_after"]:
                nxt[k] = False
        for k in range(m):
            if reach[k] and not nxt[k + 1] and _hit(req[k], a, rows[i] if rows else None):
                nxt[k + 1] = True
        reach = nxt
        if reach[m]:
            return True
    return False


class Nodes:
    def __init__(self, ds: str | None = None):
        df = pc.load_events(ds or DS)        # the configured dataset (not the default bound at import)                       # sorted by (trace_id, position)
        self.names = df["trace_id"].to_numpy(dtype=object)
        cut = np.flatnonzero(self.names[1:] != self.names[:-1]) + 1
        starts, ends = np.concatenate([[0], cut]), np.concatenate([cut, [len(df)]])
        acts = df["activity"].to_numpy(dtype=object)
        comp = df[BIND].to_numpy(dtype=object)
        cols = {a: df[a].to_numpy(dtype=object) for a in ATTRS}
        self.attr = {a: [c[s:e].tolist() for s, e in zip(starts, ends)] for a, c in cols.items()}
        self.ids = [str(self.names[s]) for s in starts]
        self.acts = [acts[s:e].tolist() for s, e in zip(starts, ends)]
        self.comp = [comp[s:e].tolist() for s, e in zip(starts, ends)]
        self.len = [int(e - s) for s, e in zip(starts, ends)]
        self.counts = [collections.Counter(a) for a in self.acts]
        self.freq = [a for a, _ in collections.Counter(acts.tolist()).most_common()]

    def matching(self, q: dict) -> list[int]:
        req = pl.required(q["elements"])
        need = collections.Counter(r["acts"][0] for r in req if len(r["acts"]) == 1)
        out = []
        for k in range(len(self.ids)):
            if any(self.counts[k][a] < n for a, n in need.items()):
                continue
            if any(not any(self.counts[k][a] for a in r["acts"]) for r in req):
                continue
            if q.get("binding"):
                byv = collections.defaultdict(list)
                for a, v in zip(self.acts[k], self.comp[k]):
                    byv[v].append(a)
                if any(_match(sub, req) for v, sub in byv.items() if v is not None):
                    out.append(k)
            elif any(r["preds"] for r in req):
                rows = [dict(zip(ATTRS, vals)) for vals in zip(*(self.attr[a][k] for a in ATTRS))]
                if _match(self.acts[k], req, rows):
                    out.append(k)
            elif _match(self.acts[k], req):
                out.append(k)
        return out


def _spread(rng, cand: list[int], L: int) -> list[int] | None:
    n = len(cand)
    if n < L:
        return None
    b = [round(i * n / L) for i in range(L + 1)]
    return [cand[rng.randrange(b[i], b[i + 1])] for i in range(L)]


def _edit(rng, acts, pos, freq, ops):
    picked = [acts[p] for p in pos]
    els = [{"acts": [a], "quant": "one", "neg_after": [], "preds": []} for a in picked]
    pattern_acts = set(picked)
    neg_gap = None
    if "neg" in ops:
        gaps = list(range(len(els) - 1))
        rng.shuffle(gaps)
        for j in gaps:
            between = set(acts[pos[j] + 1:pos[j + 1]])
            cands = [a for a in freq[:60] if a not in pattern_acts and a not in between]
            if cands:
                neg_gap = j
                els[j]["neg_after"] = [rng.choice(cands[:15])]
                break
        if neg_gap is None:
            return None
    forbidden = {y for e in els for y in e["neg_after"]}
    if "or" in ops:
        i = rng.randrange(len(els))
        others = [a for a in freq[:30] if a not in els[i]["acts"] and a not in forbidden]
        els[i]["acts"].append(rng.choice(others[:15]))
    if "plus" in ops:
        cands = [i for i in range(len(els)) if neg_gap is None or i not in (neg_gap, neg_gap + 1)]
        els[rng.choice(cands)]["quant"] = "plus"
    if "opt" in ops:
        j = rng.choice([j for j in range(len(els) - 1) if j != neg_gap])
        z = rng.choice([a for a in freq[:15] if a not in forbidden])
        els.insert(j + 1, {"acts": [z], "quant": "opt", "neg_after": [], "preds": []})
    return els


def generate(seed: int = pc.SEED, kinds=("structural", "binding")) -> list[dict]:
    rng = random.Random(f"{seed}:{DS}:{SUITE}")
    N = Nodes()
    long_ids = [k for k, n in enumerate(N.len) if n >= MIN_TRACE]
    print(f"[{DS}] {len(long_ids)} nodes with >= {MIN_TRACE} events", file=sys.stderr)
    out = []
    for L in LENGTHS:
        for kind in kinds:
            made, attempts = 0, 0
            while made < PER_KIND and attempts < 400 * PER_KIND:
                attempts += 1
                k = rng.choice(long_ids)
                acts, comp = N.acts[k], N.comp[k]
                if kind == "binding":
                    vals = [v for v, c in collections.Counter(comp).items() if v is not None and c >= L]
                    if not vals:
                        continue
                    v = rng.choice(vals)
                    idx = [i for i, c in enumerate(comp) if c == v]
                    sub = [acts[i] for i in idx]       # the witness's events with Component = v
                    ops = rng.sample(["neg", "or"], rng.randint(1, 2))
                else:
                    sub = acts
                    ops = rng.sample(OPS, rng.randint(2, 4))
                cnt = collections.Counter(sub)
                pos = _spread(rng, [i for i, a in enumerate(sub) if cnt[a] >= MIN_REPEAT], L)
                if pos is None:
                    continue
                els = _edit(rng, sub, pos, N.freq, ops)
                if els is None:
                    continue
                q = {"elements": els, "binding": BIND if kind == "binding" else None}
                hits = N.matching(q)
                assert k in hits, (L, kind, els)
                hl = [N.len[h] for h in hits]
                if min(hl) < MIN_MATCH_LEN:
                    continue
                q.update({
                    "qid": f"{DS}.case.{SUITE}.L{L:02d}.{kind[:6]}.{made:02d}",
                    "dataset": DS, "perspective": "case", "grouping_keys": pc.grouping_keys("case"),
                    "length": L, "kind": kind, "ops": sorted(ops),
                    "truth": {"matched_groups": len(hits), "group_count": len(N.ids)},
                    "witness_group": N.ids[k], "witness_length": N.len[k],
                    "matched_trace_length": {"min": min(hl), "median": float(statistics.median(hl)),
                                             "max": max(hl)},
                })
                q["siesta_pattern"] = siesta_pattern(q)
                out.append(q)
                made += 1
            print(f"  [{DS}] L={L} {kind}: {made} ({attempts} attempts)", file=sys.stderr, flush=True)
    return out


def generate_attribute(seed: int = pc.SEED, preds: bool = True) -> list[dict]:
    """The ``attribute`` kind (its own random stream: the other kinds stay unchanged).
    ``preds=False``: the ``structural_noq`` kind, the same OR / negation patterns
    without predicates (EQL-expressible structural queries: no ``?`` / ``+``)."""
    kind = "attribute" if preds else "structural_noq"
    rng = random.Random(f"{seed}:{DS}:{SUITE}:{kind}")
    N = Nodes()
    long_ids = [k for k, n in enumerate(N.len) if n >= MIN_TRACE]
    out = []
    for L in LENGTHS:
        made, attempts = 0, 0
        while made < PER_KIND and attempts < 400 * PER_KIND:
            attempts += 1
            k = rng.choice(long_ids)
            acts = N.acts[k]
            cnt = collections.Counter(acts)
            pos = _spread(rng, [i for i, a in enumerate(acts) if cnt[a] >= MIN_REPEAT], L)
            if pos is None:
                continue
            ops = rng.sample(["neg", "or"], rng.randint(1, 2))
            els = _edit(rng, acts, pos, N.freq, ops)     # no ? / +: elements stay aligned with pos
            if els is None:
                continue
            plain = [j for j, e in enumerate(els) if len(e["acts"]) == 1] if preds else []
            for j in rng.sample(plain, min(len(plain), rng.randint(1, 3))):
                avail = [a for a in ATTRS if isinstance(N.attr[a][k][pos[j]], str)
                         and not pc._UNSAFE_VALUE.search(N.attr[a][k][pos[j]])]
                if avail:
                    a = rng.choice(avail)
                    els[j]["preds"] = [{"attr": a, "value": N.attr[a][k][pos[j]]}]
            if preds and not any(e["preds"] for e in els):
                continue
            q = {"elements": els, "binding": None}
            hits = N.matching(q)
            assert k in hits, (L, els)
            hl = [N.len[h] for h in hits]
            if min(hl) < MIN_MATCH_LEN:
                continue
            q.update({
                "qid": f"{DS}.case.{SUITE}.L{L:02d}.{'attrib' if preds else 'snoq'}.{made:02d}",
                "dataset": DS, "perspective": "case", "grouping_keys": pc.grouping_keys("case"),
                "length": L, "kind": kind, "ops": sorted(ops),
                "truth": {"matched_groups": len(hits), "group_count": len(N.ids)},
                "witness_group": N.ids[k], "witness_length": N.len[k],
                "matched_trace_length": {"min": min(hl), "median": float(statistics.median(hl)),
                                         "max": max(hl)},
            })
            q["siesta_pattern"] = siesta_pattern(q)
            out.append(q)
            made += 1
        print(f"  [{DS}] L={L} {kind}: {made} ({attempts} attempts)", file=sys.stderr, flush=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["generate", "add-attribute", "add-structural-noq", "generate-nobind",
                                    "generate-noq-attr"])
    ap.add_argument("--dataset", default="bgl", choices=sorted(CONFIGS))
    args = ap.parse_args()
    configure(args.dataset)
    if args.cmd == "generate-noq-attr":
        # structural without ? / + and attribute-aware only (the BPIC logs)
        qs = generate_attribute(preds=False) + generate_attribute()
        p = query_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps(q) + "\n" for q in qs))
        print(f"{DS}: {len(qs)} queries, {dict(collections.Counter(q['kind'] for q in qs))} -> {p}",
              file=sys.stderr)
        return
    if args.cmd == "generate-nobind":
        # structural (with ? / +), structural without ? / +, attribute-aware: no bindings
        qs = generate(kinds=("structural",)) + generate_attribute(preds=False) + generate_attribute()
        p = query_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps(q) + "\n" for q in qs))
        print(f"{DS}: {len(qs)} queries, {dict(collections.Counter(q['kind'] for q in qs))} -> {p}",
              file=sys.stderr)
        return
    if args.cmd == "add-structural-noq":
        # structural queries without ? / + (OR and negation only), appended; other kinds unchanged
        qs = [q for q in load() if q["kind"] != "structural_noq"] + generate_attribute(preds=False)
        query_path().write_text("".join(json.dumps(q) + "\n" for q in qs))
        print(f"{DS}: {len(qs)} queries -> {query_path()}", file=sys.stderr)
        return
    if args.cmd == "add-attribute":
        # append the attribute kind to the existing file (structural / binding unchanged)
        qs = [q for q in load() if q["kind"] != "attribute"] + generate_attribute()
        query_path().write_text("".join(json.dumps(q) + "\n" for q in qs))
        print(f"{DS}: {len(qs)} queries -> {query_path()}", file=sys.stderr)
        return
    qs = generate()
    p = query_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(q) + "\n" for q in qs))
    ops = collections.Counter(o for q in qs for o in q["ops"])
    print(f"{DS}: {len(qs)} queries, ops {dict(ops)}, median matched-trace length "
          f"{statistics.median(q['matched_trace_length']['median'] for q in qs)} -> {p}", file=sys.stderr)


if __name__ == "__main__":
    main()
