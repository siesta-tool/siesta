"""
Linear-time validator for binding-free sequence patterns.

The general validator (OpenCEP via ``find_occurrences_dsl``) builds an NFA
per group and backtracks; on groups with many candidate events (long traces,
coarse perspectives) it dominates detection time and memory.  Detection only
needs to know *whether* a group matches (and the positions of one match), and
for the common binding-free fragment that is a single left-to-right pass over
the group's events with a state of size O(pattern length):

    pattern  ::= element+                                     (no top-level ||)
    element  ::= activity | (a || b || ...) with quantifier ONE, + (one or
                 more: one occurrence suffices), ? or * (may be skipped)
               | !x or !(x || y)  between two quantifier-ONE elements
    activity ::= LABEL ['[' attr = "literal", ... ']']

A negation forbids its activities strictly between the events matched by the
two elements around it.  Everything else ($N bindings, != predicates, nested
sequences, top-level OR, negation next to an optional or repeated element,
leading / trailing negation) is left to the general validator:
``compile_automaton`` returns None for it.

Semantics are existence semantics over the group's events in group order,
identical to the general validator's answer for this fragment: ``x?``/``x*``
may be skipped, ``x+`` is matched by its first occurrence, alternatives
match any branch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from siesta.modules.query.parse_seql import (
    ActivityNode, ElementNode, NegatedNode, OrNode, Quantifier, SeqNode, StringLiteral,
    parse_pattern,
)

Alternative = Tuple[str, Tuple[Tuple[str, str], ...]]   # (label, ((attr, value), ...))


@dataclass
class AutomatonSpec:
    """Required elements (alternatives each) and the activities forbidden after each."""
    required: List[List[Alternative]] = field(default_factory=list)
    forbidden: List[frozenset] = field(default_factory=list)

    def relevant(self) -> frozenset:
        labels = {lab for alts in self.required for lab, _ in alts}
        for f in self.forbidden:
            labels |= f
        return frozenset(labels)


def _alternative(node: ActivityNode) -> Optional[Alternative]:
    lits = []
    for c in node.constraints:
        if c.op != "=" or not isinstance(c.value, StringLiteral):
            return None                       # bindings and != stay with CEP
        lits.append((c.name, c.value.value))
    return node.label, tuple(lits)


def _alternatives(atom) -> Optional[List[Alternative]]:
    if isinstance(atom, ActivityNode):
        a = _alternative(atom)
        return None if a is None else [a]
    if isinstance(atom, OrNode):
        out = []
        for br in atom.branches:
            if (not isinstance(br, SeqNode) or len(br.elements) != 1
                    or br.elements[0].quantifier != Quantifier.ONE
                    or not isinstance(br.elements[0].atom, ActivityNode)):
                return None
            a = _alternative(br.elements[0].atom)
            if a is None:
                return None
            out.append(a)
        return out
    return None


def _forbidden(atom: NegatedNode) -> Optional[frozenset]:
    labels = []
    for act in atom.forbidden_activities():
        if act.constraints:
            return None                       # a constrained negation stays with CEP
        labels.append(act.label)
    return frozenset(labels)


def compile_automaton(pattern: str) -> Optional[AutomatonSpec]:
    """The automaton for ``pattern``, or None when it is outside the fragment."""
    try:
        root = parse_pattern(pattern)
    except Exception:
        return None
    if isinstance(root, ActivityNode):
        root = SeqNode([ElementNode(root)])
    if not isinstance(root, SeqNode):
        return None                           # top-level OR: branches stay with CEP
    els = root.elements
    spec = AutomatonSpec()
    pending_neg: Optional[frozenset] = None
    prev_kind = None                          # "one" | "loose" | None (start)
    for el in els:
        if isinstance(el.atom, NegatedNode):
            f = _forbidden(el.atom)
            if f is None or prev_kind != "one":
                return None                   # leading negation, or next to ?/*/+
            pending_neg = f if pending_neg is None else pending_neg | f
            continue
        alts = _alternatives(el.atom)
        if alts is None:
            return None
        q = el.quantifier
        if pending_neg is not None:
            if q != Quantifier.ONE:
                return None                   # negation next to ?/*/+
            spec.forbidden[-1] = spec.forbidden[-1] | pending_neg
            pending_neg = None
        if q in (Quantifier.OPT, Quantifier.STAR):
            prev_kind = "loose"               # may be skipped: no effect on existence
            continue
        spec.required.append(alts)
        spec.forbidden.append(frozenset())
        prev_kind = "one" if q == Quantifier.ONE else "loose"
    if pending_neg is not None or not spec.required:
        return None                           # trailing negation / nothing required
    return spec


def _hit(alts: Sequence[Alternative], name, event: dict) -> bool:
    for label, lits in alts:
        if name == label and all(event.get(a) == v for a, v in lits):
            return True
    return False


def match_events(events: Sequence[dict], spec: AutomatonSpec) -> List[int]:
    """
    Positions of one match of ``spec`` in ``events`` (dicts with ``name`` and
    ``position``, in group order), or [] when the group does not match.
    One pass; ``alive[k]`` holds the positions of a match of the first k
    required elements whose last event has no forbidden event after it.
    """
    req, forb = spec.required, spec.forbidden
    m = len(req)
    alive: List[Optional[List[int]]] = [[]] + [None] * m
    for ev in events:
        name = ev.get("name")
        nxt = list(alive)
        # a forbidden event ends the partial matches that precede it ...
        for k in range(1, m):
            if alive[k] is not None and name in forb[k - 1]:
                nxt[k] = None
        # ... and the event itself may extend any partial match (from the
        # state before it), including as the element its gap forbids
        for k in range(m):
            if alive[k] is not None and _hit(req[k], name, ev) and nxt[k + 1] is None:
                nxt[k + 1] = alive[k] + [int(ev["position"])]
        alive = nxt
        if alive[m] is not None:
            return alive[m]
    return []
