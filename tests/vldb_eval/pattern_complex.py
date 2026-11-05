"""
tests/vldb_eval/pattern_complex.py — complex-operator pattern workload
(BPIC 2017; perspectives case, Action, org:resource; lengths 8-15).

Each query is a real window of a perspective group (pattern_common's window
rules: contiguous, <= 4 occurrences per activity), edited with 1-3 operators
that keep the witness group matching (so no query is empty):

  opt   an extra optional activity ``Z?`` inserted between two events
  plus  one event becomes ``X+`` (one or more)
  or    one event becomes ``(X || W)`` (X or W)
  neg   ``!Y`` between two consecutive events, Y an activity of the log that
        is not in the pattern (the window is contiguous, so nothing lies
        between them in the witness)

At most one negation per query; negation is never adjacent to an optional or
Kleene element, so every system reads it the same way: no Y strictly between
the events matched by its two neighbours.

Existence semantics (what every system counts: matching groups):
``X?`` may be skipped, ``X+`` matches like ``X`` (one occurrence suffices),
``(X || W)`` matches either, and ``A !Y B`` requires no Y strictly between the
matched A and B.  The ground truth (``truth_count``) is a one-pass automaton
over each group's events in group order.

Query record (``results/exp4_patterns/queries/complex/bpic2017.<persp>.jsonl``)::

    {"qid", "dataset", "perspective", "grouping_keys", "length", "kind": "complex",
     "ops": ["neg", "or"], "elements": [{"acts": ["A"], "quant": "one"|"opt"|"plus",
     "neg_after": ["Y"] | []}, ...], "siesta_pattern": "...", "truth": {...},
     "witness_group": "..."}

``length`` is the window length (elements of the sampled fragment); an
inserted optional element is extra.  ``events`` is also present (the
sampled window, for adapters that report it); it is not the query.

Usage:
    python -m tests.vldb_eval.pattern_complex generate
"""

from __future__ import annotations

import argparse
import collections
import json
import random
import sys
import time
from pathlib import Path

import numpy as np

from tests.vldb_eval import pattern_common as pc
from tests.vldb_eval.eval_common import quote_label

DS = "bpic2017"
PERSPECTIVES = ["case", "Action", "org:resource"]
SUITE = "complex"
PER_LENGTH = 10
OPS = ["neg", "opt", "plus", "or"]


def query_path(ds: str, persp: str) -> Path:
    return pc.QUERY_DIR / SUITE / f"{ds}.{pc.safe(persp)}.jsonl"


def load(ds: str, persp: str) -> list[dict]:
    with query_path(ds, persp).open() as f:
        return [json.loads(l) for l in f if l.strip()]


# ---------------------------------------------------------------------------
# Pattern model
# ---------------------------------------------------------------------------

def required(elements: list[dict]) -> list[dict]:
    """Elements that must be matched (optional ones dropped; ``+`` as one)."""
    return [e for e in elements if e["quant"] != "opt"]


def siesta_pattern(elements: list[dict]) -> str:
    out = []
    for e in elements:
        acts = e["acts"]
        tok = quote_label(acts[0]) if len(acts) == 1 else "(" + " || ".join(quote_label(a) for a in acts) + ")"
        tok += {"one": "", "opt": "?", "plus": "+"}[e["quant"]]
        out.append(tok)
        for y in e["neg_after"]:
            out.append("!" + quote_label(y))
    return " ".join(out)


def automaton(elements: list[dict]):
    """(S, F): per required element its activity set; F[k] forbidden between k and k+1."""
    req = required(elements)
    S = [set(e["acts"]) for e in req]
    F = [set(e["neg_after"]) for e in req]
    return S, F


def match_sequence(acts, S, F) -> bool:
    """One pass over a group's activities in group order (existence semantics)."""
    m = len(S)
    reach = [True] + [False] * m
    for a in acts:
        for k in range(m - 1, -1, -1):
            if reach[k] and a in S[k]:
                reach[k + 1] = True
        if reach[m]:
            return True
        for k in range(1, m):
            if reach[k] and a in F[k - 1]:
                reach[k] = False
    return reach[m]


# ---------------------------------------------------------------------------
# Ground truth over the canonical events
# ---------------------------------------------------------------------------

class Groups:
    def __init__(self, ds: str, persp: str):
        g = pc.group_order(pc.load_events(ds), persp)
        self.acts = g["activity"].to_numpy(dtype=object)
        grp = g["group"].astype(str).to_numpy()
        cut = np.flatnonzero(grp[1:] != grp[:-1]) + 1
        self.bounds = list(zip(np.concatenate([[0], cut]), np.concatenate([cut, [len(grp)]])))
        self.names = [grp[s] for s, _ in self.bounds]
        self.sets = [set(self.acts[s:e]) for s, e in self.bounds]
        self.count = len(self.bounds)
        self.frame = g

    def truth_count(self, elements: list[dict]) -> int:
        S, F = automaton(elements)
        need = [s for s in S]
        rel = set().union(*S, *F)
        n = 0
        for (s, e), present in zip(self.bounds, self.sets):
            if not all(present & x for x in need):
                continue
            seq = [a for a in self.acts[s:e] if a in rel]
            if match_sequence(seq, S, F):
                n += 1
        return n


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def _edit(rng: random.Random, window: list[str], freq_acts: list[str]) -> tuple[list[dict], list[str]] | None:
    els = [{"acts": [a], "quant": "one", "neg_after": []} for a in window]
    n_ops = rng.randint(1, 3)
    ops = rng.sample(OPS, n_ops)
    pattern_acts = set(window)
    neg_gap = None
    if "neg" in ops:
        cands = [a for a in freq_acts if a not in pattern_acts]
        if not cands:
            return None
        neg_gap = rng.randrange(len(els) - 1)
        y = rng.choice(cands[:15])
        els[neg_gap]["neg_after"] = [y]
    if "or" in ops:
        i = rng.randrange(len(els))
        others = [a for a in freq_acts if a not in els[i]["acts"]
                  and all(a not in e["neg_after"] for e in els)]
        els[i]["acts"].append(rng.choice(others[:15]))
    if "plus" in ops:
        cands = [i for i in range(len(els)) if neg_gap is None or i not in (neg_gap, neg_gap + 1)]
        i = rng.choice(cands)
        els[i]["quant"] = "plus"
    if "opt" in ops:
        gaps = [j for j in range(len(els) - 1) if j != neg_gap]
        j = rng.choice(gaps)
        forbidden = {y for e in els for y in e["neg_after"]}
        z = rng.choice([a for a in freq_acts[:15] if a not in forbidden])
        els.insert(j + 1, {"acts": [z], "quant": "opt", "neg_after": []})
    return els, sorted(ops)


def generate(persp: str, per_length: int = PER_LENGTH, seed: int = pc.SEED) -> list[dict]:
    rng = random.Random(f"{seed}:{DS}:{persp}:complex")
    G = Groups(DS, persp)
    freq_acts = [a for a, _ in collections.Counter(G.acts.tolist()).most_common()]
    groups = {}
    for (s, e), name in zip(G.bounds, G.names):
        groups[name] = G.frame.iloc[s:e].reset_index(drop=True)
    sizes = {k: len(v) for k, v in groups.items()}
    out = []
    for L in pc.LENGTHS:
        cands = sorted(k for k, n in sizes.items() if n >= L)
        made, attempts = 0, 0
        while made < per_length and attempts < 100 * per_length:
            attempts += 1
            gid = rng.choice(cands)
            win = pc._pick_window(rng, groups[gid], L)
            if win is None:
                continue
            window = groups[gid]["activity"].iloc[win].tolist()
            edited = _edit(rng, window, freq_acts)
            if edited is None:
                continue
            els, ops = edited
            t0 = time.perf_counter()
            n = G.truth_count(els)
            assert n >= 1, (persp, L, els)      # the witness group matches
            out.append({
                "qid": f"{DS}.{pc.safe(persp)}.complex.L{L:02d}.{made:02d}",
                "dataset": DS, "perspective": persp, "grouping_keys": pc.grouping_keys(persp),
                "length": L, "kind": "complex", "ops": ops, "elements": els,
                "events": [{"activity": a, "preds": []} for a in window],
                "siesta_pattern": siesta_pattern(els),
                "truth": {"matched_groups": n, "group_count": G.count,
                          "oracle_s": round(time.perf_counter() - t0, 3)},
                "witness_group": str(gid),
            })
            made += 1
        print(f"  [{DS}/{persp}] L={L}: {made}", flush=True)
    return out


# ---------------------------------------------------------------------------
# Translations used by the competitor adapters
# ---------------------------------------------------------------------------

def es_filter(elements: list[dict]) -> list[dict]:
    """Server-side pruning: every required element's activity (or alternatives) occurs."""
    return [{"nested": {"path": "events", "query": {"terms": {"events.activity": e["acts"]}}}}
            for e in required(elements)]


def cypher(query: dict) -> tuple[str, dict]:
    """
    One Cypher query, stepwise over the required elements (optional ones do
    not change which groups match; ``X+`` needs one X; alternatives become
    ``IN``).  Outside the negated gap the earliest position per entity
    dominates.  For the negated gap A !Y B: for every candidate B after the
    previous position, only the latest A before it can witness a Y-free gap,
    so the step keeps (B, latest A) and checks for a Y strictly between
    them; the earliest surviving B continues the walk.
    """
    req = required(query["elements"])
    params: dict = {"t": query["perspective"]}
    parts = []
    m = len(req)
    i = 1
    prev = None                       # name of the previous position variable
    while i <= m:
        e = req[i - 1]
        params[f"a{i}"] = e["acts"]
        if e["neg_after"] and i < m:
            j = i + 1
            params[f"a{j}"] = req[j - 1]["acts"]
            params[f"f{i}"] = e["neg_after"]
            head = "(n:Entity)" if i == 1 else "(n)"
            lo = f" AND c{j}.gpos > {prev}" if prev else ""
            lo_a = f" AND c{i}.gpos > {prev}" if prev else ""
            first = "n.type = $t AND " if i == 1 else ""
            keep = f"n, {prev}" if prev else "n"
            parts.append(f"MATCH {head}<-[c{j}:CORR]-(e{j}:Event)\n"
                         f"WHERE {first}e{j}.activity IN $a{j}{lo}")
            parts.append(f"MATCH (n)<-[c{i}:CORR]-(e{i}:Event)\n"
                         f"WHERE e{i}.activity IN $a{i} AND c{i}.gpos < c{j}.gpos{lo_a}")
            parts.append(f"WITH {keep}, c{j}.gpos AS b{j}, max(c{i}.gpos) AS la{i}")
            parts.append(f"OPTIONAL MATCH (n)<-[cy{i}:CORR]-(y{i}:Event)\n"
                         f"WHERE y{i}.activity IN $f{i} AND cy{i}.gpos > la{i} AND cy{i}.gpos < b{j}")
            parts.append(f"WITH {keep}, b{j}, count(cy{i}) AS ny{i}\nWHERE ny{i} = 0")
            if j < m:
                parts.append(f"WITH n, min(b{j}) AS p{j}")
                prev = f"p{j}"
            i = j + 1
            continue
        head = "(n:Entity)" if i == 1 else "(n)"
        where = (["n.type = $t"] if i == 1 else []) + [f"e{i}.activity IN $a{i}"]
        if prev:
            where.append(f"c{i}.gpos > {prev}")
        parts.append(f"MATCH {head}<-[c{i}:CORR]-(e{i}:Event)\nWHERE " + " AND ".join(where))
        if i < m:
            parts.append(f"WITH n, min(c{i}.gpos) AS p{i}")
            prev = f"p{i}"
        i += 1
    parts.append("RETURN DISTINCT n.uid AS g")
    return "\n".join(parts), params


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["generate"])
    ap.add_argument("--perspectives", nargs="+", default=PERSPECTIVES)
    ap.add_argument("--per-length", type=int, default=PER_LENGTH)
    args = ap.parse_args()
    for persp in args.perspectives:
        qs = generate(persp, args.per_length)
        p = query_path(DS, persp)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps(q) + "\n" for q in qs))
        ops = collections.Counter(o for q in qs for o in q["ops"])
        print(f"{DS}/{persp}: {len(qs)} queries, ops {dict(ops)} -> {p}", file=sys.stderr)


if __name__ == "__main__":
    main()
