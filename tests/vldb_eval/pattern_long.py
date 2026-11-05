"""
tests/vldb_eval/pattern_long.py — long complex-pattern workload (BPIC 2017;
perspectives case, Action, org:resource; lengths 10-20).

Each query is a real window of a perspective group (contiguous, <= 4
occurrences of any activity, >= ceil(L/4) distinct activities), edited with
1-3 operators that keep the witness group matching (non-empty result):

  opt   an extra optional activity ``Z?`` inserted between two events
  plus  one event becomes ``X+`` (one or more)
  or    one event becomes ``(X || W)``
  neg   ``!Y`` between two consecutive events (Y not in the pattern; the
        window is contiguous, so the witness has nothing between them)

At most one negation, never next to an optional or Kleene element.
Half of the queries of each length (kind ``attribute``) also carry 1-3
literal predicates ``attr = "value"`` on plain events, with values taken
from the witness events (never null); the other half (kind ``structural``)
have none.

Existence semantics (what every system counts: matching groups): ``X?`` may
be skipped, ``X+`` is matched by one X, alternatives match either, a
predicate must hold on the matched event, and ``A !Y B`` requires no Y
strictly between the matched A and B.  Ground truth: a one-pass automaton
over each group's events in group order.

Native query of each system (no client-side matching anywhere):
  SIESTA  SeQL (``siesta_pattern``)
  ELK     EQL ``sequence by <key>`` over one document per event; negation as
          a missing event; no optional / one-or-more: such queries are
          ``unsupported`` for ELK (``eql`` returns None)
  LPG     plain Cypher: every pattern event bound to the same entity in one
          MATCH, consecutive gpos ordering, ``NOT EXISTS`` for negation,
          ``IN`` for alternatives, ``OPTIONAL MATCH`` for optional events

Usage:
    python -m tests.vldb_eval.pattern_long generate
"""

from __future__ import annotations

import argparse
import collections
import json
import random
import re
import sys
import time
from pathlib import Path

import numpy as np

from tests.vldb_eval import pattern_common as pc
from tests.vldb_eval.eval_common import quote_label

DS = "bpic2017"
PERSPECTIVES = ["case", "Action", "org:resource"]
SUITE = "long20"
LENGTHS = list(range(10, 21))
PER_LENGTH = 10                 # 5 structural + 5 attribute per length
OPS = ["neg", "opt", "plus", "or"]


def query_path(ds: str, persp: str) -> Path:
    return pc.QUERY_DIR / SUITE / f"{ds}.{pc.safe(persp)}.jsonl"


def load(ds: str, persp: str) -> list[dict]:
    with query_path(ds, persp).open() as f:
        return [json.loads(l) for l in f if l.strip()]


def required(elements: list[dict]) -> list[dict]:
    return [e for e in elements if e["quant"] != "opt"]


# ---------------------------------------------------------------------------
# SeQL
# ---------------------------------------------------------------------------

def siesta_pattern(elements: list[dict]) -> str:
    out = []
    for e in elements:
        acts = e["acts"]
        if len(acts) == 1:
            tok = quote_label(acts[0])
            if e["preds"]:
                tok += "[" + ",".join(f'{p["attr"]}="{p["value"]}"' for p in e["preds"]) + "]"
        else:
            tok = "(" + " || ".join(quote_label(a) for a in acts) + ")"
        tok += {"one": "", "opt": "?", "plus": "+"}[e["quant"]]
        out.append(tok)
        for y in e["neg_after"]:
            out.append("!" + quote_label(y))
    return " ".join(out)


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------

class Groups:
    def __init__(self, ds: str, persp: str):
        g = pc.group_order(pc.load_events(ds), persp)
        self.frame = g
        self.acts = g["activity"].to_numpy(dtype=object)
        self.cols = {c: g[c].to_numpy(dtype=object) for c in pc.attribute_columns(g)}
        grp = g["group"].astype(str).to_numpy()
        cut = np.flatnonzero(grp[1:] != grp[:-1]) + 1
        self.bounds = list(zip(np.concatenate([[0], cut]), np.concatenate([cut, [len(grp)]])))
        self.names = [grp[s] for s, _ in self.bounds]
        self.sets = [set(self.acts[s:e]) for s, e in self.bounds]
        self.count = len(self.bounds)

    def _hit(self, el: dict, i: int) -> bool:
        if self.acts[i] not in el["acts"]:
            return False
        for p in el["preds"]:
            col = self.cols.get(p["attr"])
            if col is None or col[i] != p["value"]:
                return False
        return True

    def matches(self, s: int, e: int, req: list[dict], rel: set) -> bool:
        m = len(req)
        reach = [True] + [False] * m
        for i in range(s, e):
            a = self.acts[i]
            if a not in rel:
                continue
            nxt = list(reach)
            for k in range(1, m):
                if reach[k] and a in req[k - 1]["neg_after"]:
                    nxt[k] = False
            for k in range(m):
                if reach[k] and not nxt[k + 1] and self._hit(req[k], i):
                    nxt[k + 1] = True
            reach = nxt
            if reach[m]:
                return True
        return False

    def truth_count(self, elements: list[dict]) -> int:
        req = required(elements)
        rel = {a for e in req for a in e["acts"]} | {y for e in req for y in e["neg_after"]}
        n = 0
        for (s, e), present in zip(self.bounds, self.sets):
            if all(present & set(r["acts"]) for r in req) and self.matches(s, e, req, rel):
                n += 1
        return n


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def _edit(rng, window, freq_acts):
    els = [{"acts": [a], "quant": "one", "neg_after": [], "preds": []} for a in window]
    ops = rng.sample(OPS, rng.randint(1, 3))
    pattern_acts = set(window)
    neg_gap = None
    if "neg" in ops:
        cands = [a for a in freq_acts if a not in pattern_acts]
        if not cands:
            return None
        neg_gap = rng.randrange(len(els) - 1)
        els[neg_gap]["neg_after"] = [rng.choice(cands[:15])]
    forbidden = {y for e in els for y in e["neg_after"]}
    if "or" in ops:
        i = rng.randrange(len(els))
        others = [a for a in freq_acts if a not in els[i]["acts"] and a not in forbidden]
        els[i]["acts"].append(rng.choice(others[:15]))
    if "plus" in ops:
        cands = [i for i in range(len(els)) if neg_gap is None or i not in (neg_gap, neg_gap + 1)]
        els[rng.choice(cands)]["quant"] = "plus"
    if "opt" in ops:
        j = rng.choice([j for j in range(len(els) - 1) if j != neg_gap])
        z = rng.choice([a for a in freq_acts[:15] if a not in forbidden])
        els.insert(j + 1, {"acts": [z], "quant": "opt", "neg_after": [], "preds": []})
    return els, sorted(ops)


def _add_preds(rng, els, window_rows, win_idx, cat) -> bool:
    """1-3 literal predicates on plain window events, values from the witness."""
    plain = [k for k, e in enumerate(els) if len(e["acts"]) == 1 and e["quant"] != "opt" and "w" in e]
    want, added, tries = rng.randint(1, 3), 0, 0
    while added < want and tries < 100:
        tries += 1
        k = rng.choice(plain)
        attr = rng.choice(cat)
        v = window_rows.iloc[els[k]["w"]][attr]
        if not pc._usable(v) or any(p["attr"] == attr for p in els[k]["preds"]):
            continue
        els[k]["preds"].append({"attr": attr, "value": v})
        added += 1
    return added > 0


def generate(persp: str, per_length: int = PER_LENGTH, seed: int = pc.SEED) -> list[dict]:
    rng = random.Random(f"{seed}:{DS}:{persp}:{SUITE}")
    G = Groups(DS, persp)
    freq_acts = [a for a, _ in collections.Counter(G.acts.tolist()).most_common()]
    cat = pc.categorical_attrs(G.frame, {persp} if persp != "case" else set())
    groups = {name: G.frame.iloc[s:e].reset_index(drop=True) for (s, e), name in zip(G.bounds, G.names)}
    sizes = {k: len(v) for k, v in groups.items()}
    out = []
    for L in LENGTHS:
        cands = sorted(k for k, n in sizes.items() if n >= L)
        made = {"structural": 0, "attribute": 0}
        half = per_length // 2
        attempts = 0
        while min(made.values()) < half and attempts < 200 * per_length:
            attempts += 1
            kind = "structural" if made["structural"] < half else "attribute"
            gid = rng.choice(cands)
            win = pc._pick_window(rng, groups[gid], L)
            if win is None:
                continue
            rows = groups[gid].iloc[win].reset_index(drop=True)
            edited = _edit(rng, rows["activity"].tolist(), freq_acts)
            if edited is None:
                continue
            els, ops = edited
            # remember which window row each element came from (the only
            # optional element is the inserted one, which is not a window row)
            w = 0
            for e in els:
                if e["quant"] == "opt":
                    continue
                e["w"] = w
                w += 1
            if kind == "attribute" and not _add_preds(rng, els, rows, win, cat):
                continue
            for e in els:
                e.pop("w", None)
            n = G.truth_count(els)
            assert n >= 1, (persp, L, els)      # the witness group matches
            out.append({
                "qid": f"{DS}.{pc.safe(persp)}.{SUITE}.L{L:02d}.{kind[:6]}.{made[kind]:02d}",
                "dataset": DS, "perspective": persp, "grouping_keys": pc.grouping_keys(persp),
                "length": L, "kind": kind, "ops": ops, "elements": els,
                "siesta_pattern": siesta_pattern(els),
                "truth": {"matched_groups": n, "group_count": G.count},
                "witness_group": str(gid),
            })
            made[kind] += 1
        print(f"  [{DS}/{persp}] L={L}: {made}", flush=True)
    return out


# ---------------------------------------------------------------------------
# Native translations
# ---------------------------------------------------------------------------

def es_field(attr: str) -> str:
    return "a_" + re.sub(r"[^A-Za-z0-9_]", "_", attr)


def _eql_str(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def eql(query: dict) -> str | None:
    """EQL sequence query, or None when the pattern uses ``?`` / ``+`` (not in EQL)."""
    els = query["elements"]
    if any(e["quant"] != "one" for e in els):
        return None
    key = "trace_id" if query["perspective"] == "case" else es_field(query["perspective"])
    # a binding X[attr=$1] on every element is EQL's per-stage join key (missing-event stages too)
    by = f" by {es_field(query['binding'])}" if query.get("binding") else ""
    stages = []
    for e in els:
        cond = "activity in (" + ", ".join(_eql_str(a) for a in e["acts"]) + ")"
        for p in e["preds"]:
            cond += f" and {es_field(p['attr'])} == {_eql_str(p['value'])}"
        stages.append(f"[any where {cond}]{by}")
        for y in e["neg_after"]:
            stages.append(f"![any where activity == {_eql_str(y)}]{by}")
    return f"sequence by {key} with maxspan=3650d " + " ".join(stages)


def _before(x: str, y: str) -> str:
    """Group order (ts, trace_id, position) on Event nodes: idx is the canonical row."""
    return f"({x}.ts < {y}.ts OR ({x}.ts = {y}.ts AND {x}.idx < {y}.idx))"


def cypher_attr_join(query: dict) -> tuple[str, dict]:
    """
    Plain Cypher for a non-case perspective on a case-only graph: the
    grouping is not materialised, so all pattern events are Event nodes with
    equal values of the perspective attribute, ordered by (ts, idx).
    """
    attr = query["perspective"]
    els = query["elements"]
    params: dict = {}
    match, where, optional, req_idx = [], [], [], []
    for i, e in enumerate(els, 1):
        params[f"a{i}"] = e["acts"]
        cond = [f"e{i}.activity IN $a{i}"]
        for k, p in enumerate(e["preds"]):
            params[f"v{i}_{k}"] = p["value"]
            cond.append(f"e{i}.`{p['attr']}` = $v{i}_{k}")
        if e["quant"] == "opt":
            optional.append((i, cond))
            continue
        match.append(f"(e{i}:Event)")
        where += cond
        req_idx.append(i)
    g = f"e{req_idx[0]}.`{attr}`"
    where.append(f"{g} IS NOT NULL")
    for r in req_idx[1:]:
        where.append(f"e{r}.`{attr}` = {g}")
    for a, b in zip(req_idx, req_idx[1:]):
        where.append(_before(f"e{a}", f"e{b}"))
        ya = els[a - 1]["neg_after"]
        if ya:
            params[f"f{a}"] = ya
            where.append(f"NOT EXISTS {{ MATCH (y{a}:Event) WHERE y{a}.activity IN $f{a} AND y{a}.`{attr}` = {g} "
                         f"AND {_before(f'e{a}', f'y{a}')} AND {_before(f'y{a}', f'e{b}')} }}")
    text = "MATCH " + ", ".join(match) + "\nWHERE " + "\n  AND ".join(where)
    for i, cond in optional:
        prev = max([r for r in req_idx if r < i], default=None)
        nxt = min([r for r in req_idx if r > i], default=None)
        bounds = [f"e{i}.`{attr}` = {g}"]
        bounds += ([_before(f"e{prev}", f"e{i}")] if prev else []) + ([_before(f"e{i}", f"e{nxt}")] if nxt else [])
        text += f"\nOPTIONAL MATCH (e{i}:Event) WHERE " + " AND ".join(cond + bounds)
    return text + f"\nRETURN DISTINCT {g} AS g", params


def cypher_plain(query: dict) -> tuple[str, dict]:
    """The straightforward Cypher: one MATCH over the shared entity."""
    if query["perspective"] != "case":
        return cypher_attr_join(query)
    els = query["elements"]
    params: dict = {"t": query["perspective"]}
    match, where, optional = ["(n:Entity)"], ["n.type = $t"], []
    req_idx = []
    for i, e in enumerate(els, 1):
        params[f"a{i}"] = e["acts"]
        cond = [f"e{i}.activity IN $a{i}"]
        for k, p in enumerate(e["preds"]):
            params[f"v{i}_{k}"] = p["value"]
            cond.append(f"e{i}.`{p['attr']}` = $v{i}_{k}")
        if e["quant"] == "opt":
            optional.append((i, cond))
            continue
        match.append(f"(e{i}:Event)-[c{i}:CORR]->(n)")
        where += cond
        req_idx.append(i)
    for a, b in zip(req_idx, req_idx[1:]):
        where.append(f"c{a}.gpos < c{b}.gpos")
        ya = els[a - 1]["neg_after"]
        if ya:
            params[f"f{a}"] = ya
            where.append(f"NOT EXISTS {{ MATCH (y{a}:Event)-[cy{a}:CORR]->(n) WHERE y{a}.activity IN $f{a} "
                         f"AND cy{a}.gpos > c{a}.gpos AND cy{a}.gpos < c{b}.gpos }}")
    text = "MATCH " + ", ".join(match) + "\nWHERE " + "\n  AND ".join(where)
    for i, cond in optional:
        prev = max([r for r in req_idx if r < i], default=None)
        nxt = min([r for r in req_idx if r > i], default=None)
        bounds = ([f"c{i}.gpos > c{prev}.gpos"] if prev else []) + ([f"c{i}.gpos < c{nxt}.gpos"] if nxt else [])
        text += f"\nOPTIONAL MATCH (e{i}:Event)-[c{i}:CORR]->(n) WHERE " + " AND ".join(cond + bounds)
    return text + "\nRETURN DISTINCT n.uid AS g", params


IDX_SPAN = 1 << 21              # > canonical rows: ts * IDX_SPAN + idx orders events by (ts, idx)


def cypher_stepwise(query: dict) -> tuple[str, dict]:
    """
    The better Cypher: one query that walks the required elements step by
    step (``WITH`` between steps), keeping per group only what can still
    lead to a match.  Optional elements do not change which groups match,
    ``X+`` needs one X, alternatives become ``IN``.  Before a plain gap only
    the earliest position of the previous element matters (``min``); before
    a negated gap ``A !Y B`` every valid A position is kept, and a candidate
    B survives when the latest valid A before it has no Y strictly between.

    case: events of the group are reached through the case entity (CORR,
    gpos).  Other perspectives: the graph has no entity for them, so the
    group is the attribute value, joined on Event nodes at query time and
    ordered by (ts, idx).
    """
    persp = query["perspective"]
    req = required(query["elements"])
    m = len(req)
    params: dict = {}
    case = persp == "case"
    if case:
        params["t"] = persp

    def step(i: int, var: str, rel: str, first: bool, extra: list[str]) -> tuple[str, str]:
        """MATCH clause binding event ``var`` of the group; returns (clause, position expr)."""
        if case:
            head = "(n:Entity)" if first else "(n)"
            cond = (["n.type = $t"] if first else []) + extra
            return f"MATCH {head}<-[{rel}:CORR]-({var}:Event)\nWHERE " + " AND ".join(cond), f"{rel}.gpos"
        g = f"{var}.`{persp}`"
        cond = ([f"{g} IS NOT NULL"] if first else [f"{g} = g"]) + extra
        return f"MATCH ({var}:Event)\nWHERE " + " AND ".join(cond), f"{var}.ts * {IDX_SPAN} + {var}.idx"

    def conds(i: int, var: str) -> list[str]:
        e = req[i - 1]
        params[f"a{i}"] = e["acts"]
        out = [f"{var}.activity IN $a{i}"]
        for k, p in enumerate(e["preds"]):
            params[f"v{i}_{k}"] = p["value"]
            out.append(f"{var}.`{p['attr']}` = $v{i}_{k}")
        return out

    key = "n" if case else "g"
    parts = []
    prev = None                                   # position variable(s) of the previous element
    for i in range(1, m + 1):
        gap_neg = i > 1 and req[i - 2]["neg_after"]
        keep_all = i < m and bool(req[i - 1]["neg_after"])   # the next gap is negated
        extra = conds(i, f"e{i}")
        clause, pos = step(i, f"e{i}", f"c{i}", i == 1, extra)
        if prev:
            clause += f" AND {pos} > {prev}"
        parts.append(clause)
        head = f"e1.`{persp}` AS g" if not case and i == 1 else key   # the group, bound once
        if gap_neg:
            # (B, latest valid A before it), then no Y strictly between them
            params[f"f{i}"] = req[i - 2]["neg_after"]
            parts.append(f"WITH {head}, {pos} AS b{i}, max({prev}) AS la{i}")
            y, ypos = step(i, f"y{i}", f"cy{i}", False, [f"y{i}.activity IN $f{i}"])
            parts.append("OPTIONAL " + y + f" AND {ypos} > la{i} AND {ypos} < b{i}")
            parts.append(f"WITH {key}, b{i}, count(y{i}) AS ny{i}\nWHERE ny{i} = 0")
            pos = f"b{i}"
        if i == m:
            break
        if keep_all:
            parts.append(f"WITH DISTINCT {head}, {pos} AS p{i}")
        else:
            parts.append(f"WITH {head}, min({pos}) AS p{i}")
        prev = f"p{i}"
    parts.append(f"RETURN DISTINCT {'n.uid' if case else 'g'} AS g")
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
        print(f"{DS}/{persp}: {len(qs)} queries, ops {dict(ops)}, "
              f"ELK-supported {sum(1 for q in qs if eql(q))} -> {p}", file=sys.stderr)


if __name__ == "__main__":
    main()
