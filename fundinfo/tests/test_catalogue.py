"""Invariants over the scraped rule catalogue. Skipped until step 1 has run."""

import json
from collections import Counter
from datetime import date

import pytest

from conftest import CATALOGUE, MODES
from fundinfo_expressions import Parser, mode_for
from validate_fundinfo import CONSTRAINTS, RELATIVE_DATE, UNBOUNDED, compile_field

pytestmark = pytest.mark.skipif(not CATALOGUE.exists(), reason="no scraped catalogue; run step 1")
AS_OF = date(2026, 1, 15)
GAP_KINDS = {"metadata", "type", "format", "source_parse_warning", "rules", "severity", "constraint", "data_options", "expression"}


@pytest.fixture(scope="module")
def fields():
    return json.loads(CATALOGUE.read_text(encoding="utf-8"))["fields"]


@pytest.fixture(scope="module")
def modes():
    return json.loads(MODES.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def plans(fields, modes):
    return {code: compile_field(code, d, AS_OF, modes, skip_relative=True) for code, d in fields.items()}


def test_every_field_compiles(plans):
    kinds = Counter(g["kind"] for p in plans.values() for g in p.gaps)
    assert set(kinds) <= GAP_KINDS


def test_every_expression_is_checked_or_reported(fields, plans):
    for code, definition in fields.items():
        written = sum(len(r.get("expressions", [])) for r in definition.get("rules", []))
        checked = sum(c.kind == "expression" for c in plans[code].checks)
        reported = sum(g["kind"] == "expression" for g in plans[code].gaps)
        assert written == checked + reported, code


def test_every_override_matches_a_catalogue_expression(fields, modes):
    catalogue = {e["expression"] for d in fields.values() for r in d.get("rules", []) for e in r.get("expressions", [])}
    assert [e for e in modes if e not in catalogue] == []


def test_overrides_are_needed(modes):
    # An override that matches the built-in policy is dead weight.
    from fundinfo_expressions import Unsupported
    for expression, mode in modes.items():
        try:
            default = mode_for(Parser(expression).parse(), expression, {})
        except Unsupported:
            continue
        assert default != mode, expression


def test_skip_relative_dates_drops_every_relative_bound(fields, plans):
    # A bound that date_bound reads relative to the run date must not survive --skip-relative-dates.
    leaked = []
    for code, plan in plans.items():
        for check in plan.checks:
            if check.kind == "bound" and check.value[2] == "DateTime":
                raw = str(check.value[4]).strip()
                if raw[:1] in "+-" or RELATIVE_DATE.match(raw):
                    leaked.append((code, raw))
    assert leaked == []


def test_constraint_keys_are_known(fields):
    keys = Counter(k for d in fields.values() for r in d.get("rules", []) for k, raw in r["constraints"].items()
                   if raw is not None and str(raw).lower().strip() not in UNBOUNDED)
    assert set(keys) <= CONSTRAINTS


def test_real_lifecycle_rules(run_validation, fields, modes):
    codes = ["OFST020545", "OFST020562", "OFST020563"]
    rows = [["dormant", date(2024, 1, 1), date(2024, 6, 1)],
            ["dormant", date(2024, 1, 1), None],
            ["active", date(2024, 1, 1), date(2023, 6, 1)],
            ["active", None, None]]
    sheets = {"S": (["OFST020000", *codes], [["LU2055195056", *row] for row in rows])}
    r = run_validation(sheets, [fields[c] for c in ["OFST020000", *codes]], modes=modes, skip_relative_dates=True)
    lifecycle = {i["excel_row"] for i in r.issues
                 if i["code"] == "OFST020545" and i["check"] == "expression" and i["outcome"] == "violation"}
    assert lifecycle == {"3", "5"}
