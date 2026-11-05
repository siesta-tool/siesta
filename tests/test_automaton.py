"""The linear validator (adaptive_query/automaton.py) against brute force."""

import itertools
import random

from siesta.modules.adaptive_query.automaton import compile_automaton, match_events


def _brute(acts, spec):
    m, n = len(spec.required), len(acts)
    for pos in itertools.combinations(range(n), m):
        if all(any(acts[p] == lab for lab, _ in spec.required[k]) for k, p in enumerate(pos)):
            if all(not any(a in spec.forbidden[k] for a in acts[pos[k] + 1:pos[k + 1]]) for k in range(m - 1)):
                return True
    return False


def test_random_patterns_match_brute_force():
    rng = random.Random(7)
    checked = 0
    for _ in range(3000):
        els = []
        for _ in range(rng.randint(1, 5)):
            if rng.random() < 0.15:
                els.append(f"({rng.choice('ABCD')} || {rng.choice('ABCD')})")
            else:
                els.append(rng.choice("ABCD") + rng.choice(["", "", "", "?", "+", "*"]))
            if rng.random() < 0.25:
                els.append("!" + rng.choice("ABCY"))
        spec = compile_automaton(" ".join(els))
        if spec is None:
            continue
        checked += 1
        acts = [rng.choice("ABCDY") for _ in range(rng.randint(0, 9))]
        evs = [{"name": a, "position": i} for i, a in enumerate(acts)]
        assert bool(match_events(evs, spec)) == _brute(acts, spec)
    assert checked > 1000


def test_outside_fragment_falls_back():
    for p in ['A B[r=$1]', 'A || B', '!A B', 'A !B', 'A? !B C', 'A B[r!="x"]', 'A (B C)']:
        assert compile_automaton(p) is None


def test_literal_predicates_and_positions():
    spec = compile_automaton('A B[r="x"] C')
    evs = [{"name": "A", "position": 0}, {"name": "B", "position": 1, "r": "y"},
           {"name": "B", "position": 2, "r": "x"}, {"name": "C", "position": 3}]
    assert match_events(evs, spec) == [0, 2, 3]
    assert match_events(evs[:3], spec) == []


def test_negation_between_neighbours_only():
    spec = compile_automaton("A !Y B C")
    ev = lambda s: [{"name": a, "position": i} for i, a in enumerate(s)]
    assert match_events(ev("AYBC"), spec) == []
    assert match_events(ev("AYABC"), spec) == [2, 3, 4]      # a later A after the Y
    assert match_events(ev("ABYC"), spec) == [0, 1, 3]       # Y after B is allowed
