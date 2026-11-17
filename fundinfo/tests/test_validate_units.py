"""Helpers, field compilation and per-cell checks in validate_fundinfo."""

import re
from datetime import date, datetime, time
from decimal import Decimal as D

import pytest

from conftest import field, rule
from fundinfo_expressions import IGNORED, Context, Unknown, Unsupported
from validate_fundinfo import (check_cell, coerce, compile_field, date_bound, empty, number, option_match, parse_date,
                               prepare_cell, text_value, valid_identifier, write_csv_row)

AS_OF = date(2026, 1, 15)
LEI = "529900T8BM49AURSDO55"


def plan_for(definition, overrides=None, skip_relative=False):
    return compile_field(definition["code"], definition, AS_OF, overrides or {}, skip_relative)


def gap_kinds(plan):
    return [g["kind"] for g in plan.gaps]


def run_checks(definition, raw, others=None, overrides=None):
    """Prepare one cell and run every compiled check on it, as validate() does."""
    plan = plan_for(definition, overrides)
    value, items, errors = prepare_cell(raw, plan)
    values = {definition["code"]: value, **(others or {})}
    sensitive = {definition["code"]} if plan.sensitive else set()
    context = Context(values, definition["code"], sensitive)
    findings = [(outcome, check.kind, message, item)
                for check in plan.checks
                for outcome, message, item in check_cell(check, raw, value, items, plan, context)]
    return errors, findings


def outcomes(findings):
    return [(outcome, kind) for outcome, kind, _, _ in findings]


# Scalar helpers

@pytest.mark.parametrize("value, expected", [
    (None, True), ("", True), ("   ", True), ("[N/A]", True), (" [N/A] ", True),
    ("x", False), (0, False), (False, False), ("[IGNORE]", False), ("[n/a]", False),
])
def test_empty(value, expected):
    assert empty(value) is expected


@pytest.mark.parametrize("value, expected", [
    (None, ""), (IGNORED, "[IGNORE]"), (True, "yes"), (False, "no"),
    (date(2024, 1, 2), "2024-01-02"), (datetime(2024, 1, 2, 3, 4), "2024-01-02T03:04:00"),
    (time(9, 30), "09:30:00"), (D("1.50"), "1.50"), (7, "7"),
])
def test_text_value(value, expected):
    assert text_value(value) == expected


@pytest.mark.parametrize("raw, expected", [
    ("1", D(1)), ("-1.5", D("-1.5")), (".5", D("0.5")), ("+2", D(2)), ("1e3", D(1000)),
    ("  3  ", D(3)), (1.25, D("1.25")), (10, D(10)),
])
def test_number_accepts_decimals(raw, expected):
    assert number(raw) == expected


@pytest.mark.parametrize("raw", ["1,000", "abc", "", "1.2.3", "nan", "inf", float("nan"), "5%"])
def test_number_rejects_non_decimals(raw):
    with pytest.raises(ValueError):
        number(raw)


def test_number_rejects_booleans():
    with pytest.raises(ValueError, match="Boolean"):
        number(True)


class TestParseDate:
    @pytest.mark.parametrize("raw, fmt, expected", [
        ("2024-02-29", "yyyy-MM-dd", datetime(2024, 2, 29)),
        ("09:30", "HH:mm", datetime(2000, 1, 1, 9, 30)),
        ("02-29", "MM-dd", datetime(2000, 2, 29)),
        ("2024-01-02 13:45", "yyyy-MM-dd HH:mm", datetime(2024, 1, 2, 13, 45)),
        ("2024", "yyyy", datetime(2024, 1, 1)),
        (date(2024, 3, 4), "yyyy-MM-dd", datetime(2024, 3, 4)),
        (date(2024, 3, 4), "MM-dd", datetime(2000, 3, 4)),
        (date(2024, 3, 4), "yyyy", datetime(2024, 1, 1)),
        (datetime(2024, 3, 4), "yyyy-MM-dd", datetime(2024, 3, 4)),
        (datetime(2024, 3, 4, 10, 15), "yyyy-MM-dd HH:mm", datetime(2024, 3, 4, 10, 15)),
        (datetime(1900, 1, 1, 10, 15), "HH:mm", datetime(2000, 1, 1, 10, 15)),
        (time(10, 15), "HH:mm", datetime(2000, 1, 1, 10, 15)),
    ])
    def test_accepted(self, raw, fmt, expected):
        assert parse_date(raw, fmt) == expected

    @pytest.mark.parametrize("raw, fmt, message", [
        ("2024-02-30", "yyyy-MM-dd", "Invalid calendar value"),
        ("2024/01/01", "yyyy-MM-dd", "Expected format"),
        ("2024-1-1", "yyyy-MM-dd", "Expected format"),
        ("24:00", "HH:mm", "Invalid calendar value"),
        (datetime(2024, 1, 1, 12, 0), "yyyy-MM-dd", "without a time"),
        (datetime(2024, 1, 1, 12, 0, 5), "yyyy-MM-dd HH:mm", "minute precision"),
        (date(2024, 1, 1), "HH:mm", "Expected a time"),
        (time(10, 15, 30), "HH:mm", "no seconds"),
        (time(10, 15), "yyyy-MM-dd", "Expected HH:mm"),
    ])
    def test_rejected(self, raw, fmt, message):
        with pytest.raises(ValueError, match=message):
            parse_date(raw, fmt)

    def test_unknown_format_is_unsupported(self):
        with pytest.raises(Unsupported):
            parse_date("2024-01-01 10:00", "yyyy-MM-dd hh:mm")


@pytest.mark.parametrize("raw, fmt, expected", [
    ("-", "yyyy-MM-dd", None),
    ("inf", "yyyy-MM-dd", None),
    ("2050-01-01", "yyyy-MM-dd", datetime(2050, 1, 1)),
    ("23:59", "HH:mm", datetime(2000, 1, 1, 23, 59)),
    ("Today", "yyyy-MM-dd", datetime(2026, 1, 15)),
    ("Today - 15 Months", "yyyy-MM-dd", datetime(2024, 10, 15)),
    ("Today + 14Days", "yyyy-MM-dd", datetime(2026, 1, 29)),
    ("BeginOfYear + 1 Days", "yyyy-MM-dd", datetime(2026, 1, 2)),
    ("today - 1 year + 2 days", "yyyy-MM-dd", datetime(2025, 1, 17)),
    ("+5 Years", "yyyy-MM-dd", datetime(2031, 1, 15)),
])
def test_date_bound(raw, fmt, expected):
    assert date_bound(raw, fmt, AS_OF) == expected


class TestIdentifiers:
    @pytest.mark.parametrize("value", ["US0378331005", "LU2055195056", "us0378331005"])
    def test_valid_isin(self, value):
        assert valid_identifier(value, "ISIN")

    @pytest.mark.parametrize("value", ["US0378331006", "US037833100", "US03783310055", "1S0378331005", "US037833100X"])
    def test_invalid_isin(self, value):
        assert not valid_identifier(value, "ISIN")

    def test_valid_lei(self):
        assert valid_identifier(LEI, "LegalEntityIdentifier")
        assert valid_identifier(LEI.lower(), "LegalEntityIdentifier")

    @pytest.mark.parametrize("value", [LEI[:-1] + "6", LEI[:-1], LEI[:-2] + "5X"])
    def test_invalid_lei(self, value):
        assert not valid_identifier(value, "LegalEntityIdentifier")

    def test_unknown_format(self):
        with pytest.raises(Unsupported):
            valid_identifier("x", "CitiCode")


class TestCoerce:
    @pytest.mark.parametrize("raw, expected", [(True, True), (False, False), ("Yes", True), (" no ", False)])
    def test_boolean(self, raw, expected):
        assert coerce(raw, "Boolean") is expected

    @pytest.mark.parametrize("raw", ["y", "1", 1, "true"])
    def test_boolean_rejects_other_spellings(self, raw):
        with pytest.raises(ValueError, match="yes/no"):
            coerce(raw, "Boolean")

    def test_int_range(self):
        assert coerce(2 ** 31 - 1, "Int") == D(2 ** 31 - 1)
        assert coerce(-(2 ** 31), "Int") == D(-(2 ** 31))
        with pytest.raises(ValueError, match="32-bit"):
            coerce(2 ** 31, "Int")
        assert coerce(2 ** 63 - 1, "Long") == D(2 ** 63 - 1)
        with pytest.raises(ValueError, match="64-bit"):
            coerce(2 ** 63, "Long")

    def test_int_requires_whole_number(self):
        assert coerce("3.0", "Int") == D(3)
        with pytest.raises(ValueError, match="whole number"):
            coerce("3.5", "Int")

    def test_double(self):
        assert coerce(0.05, "Double") == D("0.05")

    def test_datetime_defaults_to_iso_date(self):
        assert coerce("2024-05-06", "DateTime") == datetime(2024, 5, 6)
        assert coerce("10:00", "DateTime", "HH:mm") == datetime(2000, 1, 1, 10, 0)

    def test_string_with_date_format_is_parsed(self):
        assert coerce("2024-05-06", "String", "yyyy-MM-dd") == datetime(2024, 5, 6)

    def test_string(self):
        assert coerce("abc", "String") == "abc"
        assert coerce(12, "String") == "12"

    def test_markers(self):
        assert coerce("[N/A]", "Double") is None
        assert coerce("   ", "Double") is None
        assert coerce("[IGNORE]", "Double") is IGNORED

    def test_unknown_type(self):
        with pytest.raises(Unsupported, match="Unsupported type"):
            coerce("x", "Currency")


# Field compilation

class TestCompileField:
    def test_plain_field(self):
        plan = plan_for(field("A"))
        assert (plan.name, plan.dtype, plan.fmt, plan.sensitive, plan.separator) == ("Name A", "String", "-", False, None)
        assert plan.gaps == [] and plan.checks == []

    def test_catalogue_default_length_bounds_are_harmless(self):
        plan = plan_for(field("A", rules=[rule(constraints={"MinLength": "0", "MaxLength": "1073741824", "Regex": "-"})]))
        assert plan.gaps == []
        assert [c.value[0] for c in plan.checks] == ["MinLength", "MaxLength"]

    def test_no_rules_is_a_gap(self):
        assert gap_kinds(plan_for(field("A", rules=[]))) == ["rules"]

    def test_multi_item_metadata(self):
        plan = plan_for(field("A", multi="Separator: '|' \n String"))
        assert (plan.separator, plan.item_type) == ("|", "String")

    def test_bad_multi_item_metadata(self):
        assert "metadata" in gap_kinds(plan_for(field("A", multi="comma separated")))

    def test_unsupported_types_and_formats(self):
        assert "type" in gap_kinds(plan_for(field("A", dtype="Currency")))
        assert "type" in gap_kinds(plan_for(field("A", multi="Separator: ';' Money")))
        assert "format" in gap_kinds(plan_for(field("A", fmt="CitiCode")))
        assert "format" in gap_kinds(plan_for(field("A", fmt="yyyy-MM-dd hh:mm")))
        assert gap_kinds(plan_for(field("A", fmt="ISIN"))) == []
        assert gap_kinds(plan_for(field("A", fmt="HH:mm"))) == []

    def test_source_parse_warnings_become_gaps(self):
        plan = plan_for(field("A", warnings=["options table truncated"]))
        assert plan.gaps[0]["kind"] == "source_parse_warning"
        assert plan.gaps[0]["reason"] == "options table truncated"

    def test_unknown_severity_and_constraint(self):
        plan = plan_for(field("A", rules=[rule(severity="Info", constraints={"Step": "1"})]))
        assert gap_kinds(plan) == ["severity", "constraint"]

    @pytest.mark.parametrize("raw", ["-", "", "inf", "+inf", "-inf", None])
    def test_unbounded_constraints_add_no_check(self, raw):
        plan = plan_for(field("A", dtype="Double", rules=[rule(constraints={"Min": raw, "Max": raw})]))
        assert plan.checks == [] and plan.gaps == []

    def test_numeric_and_length_bounds(self):
        plan = plan_for(field("A", dtype="Double", rules=[rule(constraints={"Min": "0", "MaxLength": "10"})]))
        bounds = {c.value[0]: c.value for c in plan.checks}
        assert bounds["Min"][:4] == ("Min", "scalar", "Double", D(0))
        assert bounds["MaxLength"][:4] == ("MaxLength", "text", "String", D(10))

    def test_bad_length_bound(self):
        for raw in ("-1", "2.5"):
            plan = plan_for(field("A", rules=[rule(constraints={"MaxLength": raw})]))
            assert gap_kinds(plan) == ["constraint"], raw

    def test_non_numeric_bound(self):
        plan = plan_for(field("A", dtype="Double", rules=[rule(constraints={"Max": "lots"})]))
        assert gap_kinds(plan) == ["constraint"]

    def test_date_bounds_use_field_format(self):
        plan = plan_for(field("A", dtype="DateTime", fmt="HH:mm", rules=[rule(constraints={"MinDate": "00:00", "MaxDate": "23:59"})]))
        assert [c.value[3] for c in plan.checks] == [datetime(2000, 1, 1, 0, 0), datetime(2000, 1, 1, 23, 59)]

    def test_relative_date_bounds_follow_skip_flag(self):
        definition = field("A", dtype="DateTime", fmt="yyyy-MM-dd",
                           rules=[rule(constraints={"MinDate": "Today - 15 Months", "MaxDate": "BeginOfYear"})])
        assert [c.value[3] for c in plan_for(definition).checks] == [datetime(2024, 10, 15), datetime(2026, 1, 1)]
        assert plan_for(definition, skip_relative=True).checks == []

    def test_skip_relative_covers_bounds_without_an_anchor(self):
        # The catalogue has "+5 Years" bounds. date_bound reads them relative to as_of,
        # so they must be skipped too, or step 2 depends on the run date.
        definition = field("A", dtype="DateTime", fmt="yyyy-MM-dd", rules=[rule(constraints={"MaxDate": "+5 Years"})])
        assert plan_for(definition, skip_relative=True).checks == []

    def test_regex_compilation(self):
        insensitive = plan_for(field("A", rules=[rule(constraints={"Regex": "^[A-Z]+$"})])).checks[0].value
        sensitive = plan_for(field("A", sensitive=True, rules=[rule(constraints={"Regex": "^[A-Z]+$"})])).checks[0].value
        assert insensitive.flags & re.IGNORECASE
        assert not sensitive.flags & re.IGNORECASE

    @pytest.mark.parametrize("pattern", [r"^\\d+$", r"(?<name>x)", r"\p{L}+", "("])
    def test_unusable_regex_is_a_gap(self, pattern):
        assert gap_kinds(plan_for(field("A", rules=[rule(constraints={"Regex": pattern})]))) == ["constraint"]

    def test_multi_item_bound_needs_multi_item_field(self):
        assert gap_kinds(plan_for(field("A", rules=[rule(constraints={"MultiItemMaxLength": "3"})]))) == ["constraint"]
        plan = plan_for(field("A", multi="Separator: '|' String", rules=[rule(constraints={"MultiItemMaxLength": "3"})]))
        assert plan.checks[0].value[:3] == ("MultiItemMaxLength", "item", "String")

    def test_data_options(self):
        assert gap_kinds(plan_for(field("A", rules=[rule(options=None)]))) == ["data_options"]
        assert gap_kinds(plan_for(field("A", rules=[rule(options=[{"LU": ["x"]}])]))) == ["data_options"]
        assert gap_kinds(plan_for(field("A", rules=[rule(options=["x", ["y"]])]))) == ["data_options"]
        flat = plan_for(field("A", rules=[rule(options=["x", "y"])])).checks[0]
        assert (flat.kind, flat.value) == ("options", (["x", "y"], []))
        nested = plan_for(field("A", rules=[rule(options=[{"LU": ["x"]}], nested="OFST010010")])).checks[0]
        assert nested.value == ({"LU": ["x"]}, ["OFST010010"])

    def test_expressions(self):
        plan = plan_for(field("A", rules=[rule(expressions=[
            "OnlyIf{IsSet([B])}",
            "IsEmpty([B])",
            "Weird{x}",
        ])]))
        assert [(c.kind, c.value[1]) for c in plan.checks] == [("expression", "valid_when_true")]
        assert [g["expression"] for g in plan.gaps] == ["IsEmpty([B])", "Weird{x}"]

    def test_expression_override(self):
        text = "IsEmpty([B])"
        plan = plan_for(field("A", rules=[rule(expressions=[text])]), overrides={text: "invalid_when_true"})
        assert plan.checks[0].value[1] == "invalid_when_true"

    def test_expression_message_falls_back_to_expression(self):
        plan = plan_for(field("A", rules=[rule(expressions=[{"expression": "OnlyIf{IsSet([B])}", "error_message": ""}])]))
        assert plan.checks[0].message == "OnlyIf{IsSet([B])}"

    def test_required_fields_skip_self(self):
        plan = plan_for(field("A", required=["A", "B", "C"]))
        assert [(c.kind, c.severity, c.value) for c in plan.checks] == [("required", "Fatal", "B"), ("required", "Fatal", "C")]

    def test_rule_index_is_one_based(self):
        plan = plan_for(field("A", dtype="Double", rules=[rule(), rule(severity="Warning", constraints={"Max": "5"})]))
        assert (plan.checks[0].rule_index, plan.checks[0].severity) == (2, "Warning")


# Cell preparation

class TestPrepareCell:
    def test_markers(self):
        plan = plan_for(field("A"))
        reason = Unknown("formula")
        assert prepare_cell(reason, plan) == (reason, [], [])
        assert prepare_cell(None, plan) == (None, [], [])
        assert prepare_cell("[N/A]", plan) == (None, [], [])
        assert prepare_cell("[IGNORE]", plan) == (IGNORED, [], [])

    def test_scalar(self):
        assert prepare_cell("2.5", plan_for(field("A", dtype="Double"))) == (D("2.5"), [("2.5", D("2.5"))], [])

    def test_scalar_type_error(self):
        value, items, errors = prepare_cell("abc", plan_for(field("A", dtype="Double")))
        assert isinstance(value, Unknown)
        assert errors == [(None, "Expected a decimal number (percentage cells use their underlying decimal value)")]

    def test_multi_item_splits_and_strips(self):
        plan = plan_for(field("A", multi="Separator: '|' Double"))
        value, items, errors = prepare_cell("1 | 2|3", plan)
        assert value == "1 | 2|3"
        assert [typed for _, typed in items] == [D(1), D(2), D(3)]
        assert errors == []

    def test_multi_item_errors_carry_item_numbers(self):
        plan = plan_for(field("A", multi="Separator: '|' Double"))
        _, _, errors = prepare_cell("1||x", plan)
        assert [item for item, _ in errors] == [2, 3]
        assert "Empty item" in errors[0][1]

    def test_identifier_checksum(self):
        plan = plan_for(field("A", fmt="ISIN"))
        assert prepare_cell("US0378331005", plan)[2] == []
        assert prepare_cell("US0378331006", plan)[2] == [(None, "Invalid ISIN syntax or checksum")]

    def test_unsupported_type_is_unknown_without_errors(self):
        value, _, errors = prepare_cell("x", plan_for(field("A", dtype="Currency")))
        assert isinstance(value, Unknown) and errors == []


@pytest.mark.parametrize("value, allowed, sensitive, expected", [
    ("Active", ["active"], False, True),
    ("Active", ["active"], True, False),
    (True, ["yes", "no"], False, True),
    (5, ["5"], False, True),
    ("x", [], False, False),
])
def test_option_match(value, allowed, sensitive, expected):
    assert option_match(value, allowed, sensitive) is expected


# Per-cell checks

class TestChecks:
    def test_regex_uses_search(self):
        definition = field("A", rules=[rule(constraints={"Regex": "[0-9]"})])
        assert run_checks(definition, "abc1")[1] == []
        assert outcomes(run_checks(definition, "abc")[1]) == [("violation", "regex")]

    def test_regex_case_sensitivity(self):
        loose = field("A", rules=[rule(constraints={"Regex": "^[A-Z]+$"})])
        strict = field("A", sensitive=True, rules=[rule(constraints={"Regex": "^[A-Z]+$"})])
        assert run_checks(loose, "abc")[1] == []
        assert outcomes(run_checks(strict, "abc")[1]) == [("violation", "regex")]

    def test_length_bounds_count_utf16_units(self):
        definition = field("A", rules=[rule(constraints={"MaxLength": "2"})])
        assert run_checks(definition, "ab")[1] == []
        _, findings = run_checks(definition, "a😀")
        assert findings == [("violation", "bound", "MaxLength is 2; got 3", None)]

    def test_numeric_bounds(self):
        definition = field("A", dtype="Double", rules=[rule(constraints={"Min": "0", "Max": "1"})])
        assert run_checks(definition, 0.5)[1] == []
        assert run_checks(definition, 0)[1] == []
        assert [m for _, _, m, _ in run_checks(definition, -0.1)[1]] == ["Min is 0; got -0.1"]
        assert [m for _, _, m, _ in run_checks(definition, 1.5)[1]] == ["Max is 1; got 1.5"]

    def test_date_bounds(self):
        definition = field("A", dtype="DateTime", fmt="yyyy-MM-dd",
                           rules=[rule(constraints={"MinDate": "1901-01-01", "MaxDate": "2050-01-01"})])
        assert run_checks(definition, datetime(2024, 1, 1))[1] == []
        assert outcomes(run_checks(definition, "1900-12-31")[1]) == [("violation", "bound")]
        assert outcomes(run_checks(definition, "2050-01-02")[1]) == [("violation", "bound")]

    def test_empty_cell_skips_scalar_checks(self):
        definition = field("A", dtype="Double", rules=[rule(constraints={"Min": "0", "Regex": "x"}, options=["1"])],
                           required=["B"])
        assert run_checks(definition, None, {"B": None}) == ([], [])

    def test_multi_item_bounds_report_item_numbers(self):
        definition = field("A", multi="Separator: '|' String", rules=[rule(constraints={"MultiItemMaxLength": "2"})])
        _, findings = run_checks(definition, "ab|abc|x|abcd")
        assert [(o, item) for o, _, _, item in findings] == [("violation", 2), ("violation", 4)]

    def test_multi_item_bound_on_bad_item_is_unchecked(self):
        definition = field("A", multi="Separator: '|' String", rules=[rule(constraints={"MultiItemMaxLength": "2"})])
        errors, findings = run_checks(definition, "ab||abc")
        assert errors == [(2, "Empty item inside a multi-value cell")]
        assert [(o, item) for o, _, _, item in findings] == [("unchecked", 2), ("violation", 3)]

    def test_type_error_marks_scalar_checks_unchecked(self):
        definition = field("A", dtype="Double", rules=[rule(constraints={"Max": "5"})])
        errors, findings = run_checks(definition, "abc")
        assert len(errors) == 1
        assert outcomes(findings) == [("unchecked", "bound")]

    def test_flat_options(self):
        definition = field("A", rules=[rule(options=["active", "dormant"])])
        assert run_checks(definition, "Active")[1] == []
        _, findings = run_checks(definition, "closed")
        assert findings == [("violation", "options", "Value is not in the allowed options: 'active', 'dormant'", None)]

    def test_case_sensitive_options(self):
        definition = field("A", sensitive=True, rules=[rule(options=["active"])])
        assert outcomes(run_checks(definition, "Active")[1]) == [("violation", "options")]

    def test_long_option_lists_are_truncated_in_message(self):
        options = [f"v{i}" for i in range(20)]
        _, findings = run_checks(field("A", rules=[rule(options=options)]), "zz")
        assert findings[0][2].endswith("'v11', … (20 values)")

    def test_multi_item_options_check_each_item(self):
        definition = field("A", multi="Separator: '|' String", rules=[rule(options=["x", "y"])])
        _, findings = run_checks(definition, "x|z|y|w")
        assert [(o, item) for o, _, _, item in findings] == [("violation", 2), ("violation", 4)]

    @pytest.mark.parametrize("parent, expected", [
        ("LU", []),
        ("lu", []),
        ("DE", [("violation", "options")]),
        ("FR", [("violation", "options")]),
        (None, [("unchecked", "options")]),
        (IGNORED, [("unchecked", "options")]),
        (Unknown("formula"), [("unchecked", "options")]),
    ])
    def test_nested_options(self, parent, expected):
        definition = field("A", rules=[rule(options=[{"LU": ["SICAR", "partie I"], "DE": ["other"]}], nested="OFST010010")])
        assert outcomes(run_checks(definition, "SICAR", {"OFST010010": parent})[1]) == expected

    def test_nested_options_with_absent_parent_column(self):
        definition = field("A", rules=[rule(options=[{"LU": ["SICAR"]}], nested="OFST010010")])
        _, findings = run_checks(definition, "SICAR")
        assert findings == [("unchecked", "options", "Referenced column OFST010010 is absent", None)]

    def test_nested_options_with_two_parent_levels(self):
        options = [{"LU": {"UCITS": ["a"], "AIF": ["b"]}}]
        definition = field("A", rules=[rule(options=options, nested="OFST010010 OFST010020")])
        assert run_checks(definition, "a", {"OFST010010": "LU", "OFST010020": "UCITS"})[1] == []
        assert outcomes(run_checks(definition, "a", {"OFST010010": "LU", "OFST010020": "AIF"})[1]) == [("violation", "options")]

    def test_nested_options_with_too_many_parent_keys(self):
        definition = field("A", rules=[rule(options=[{"LU": ["a"]}], nested="OFST010010 OFST010020")])
        findings = run_checks(definition, "a", {"OFST010010": "LU", "OFST010020": "x"})[1]
        assert outcomes(findings) == [("unchecked", "options")]

    def test_required_fields(self):
        definition = field("A", required=["B"])
        assert outcomes(run_checks(definition, "x", {"B": None})[1]) == [("violation", "required")]
        assert run_checks(definition, "x", {"B": "y"})[1] == []
        assert run_checks(definition, "x", {"B": IGNORED})[1] == []
        assert outcomes(run_checks(definition, "x")[1]) == [("unchecked", "required")]

    def test_valid_when_true_expression(self):
        definition = field("A", rules=[rule(expressions=["OnlyIf{IsSet([B])}"])])
        assert run_checks(definition, "x", {"B": True})[1] == []
        assert run_checks(definition, None, {"B": False})[1] == []
        _, findings = run_checks(definition, "x", {"B": False})
        assert findings == [("violation", "expression", "failed: OnlyIf{IsSet([B])}", None)]

    def test_invalid_when_true_expression(self):
        text = "IsSet([A]) and IsEmpty([B])"
        definition = field("A", dtype="Boolean", rules=[rule(expressions=[text])])
        assert outcomes(run_checks(definition, "yes", {"B": None})[1]) == [("violation", "expression")]
        assert run_checks(definition, "yes", {"B": "x"})[1] == []

    def test_expression_runs_on_empty_cells(self):
        definition = field("A", rules=[rule(expressions=["RequiredIf{IsSet([B])}"])])
        assert outcomes(run_checks(definition, None, {"B": True})[1]) == [("violation", "expression")]

    def test_length_expression_skips_empty_cells(self):
        definition = field("A", rules=[rule(expressions=["[A].Length < 10"])])
        assert run_checks(definition, None)[1] == []
        assert outcomes(run_checks(definition, "short")[1]) == [("violation", "expression")]
        assert run_checks(definition, "long enough text")[1] == []

    def test_not_applicable_expression_is_silent(self):
        definition = field("A", dtype="DateTime", rules=[rule(expressions=["[A] < [B]"])])
        assert run_checks(definition, "2024-01-01", {"B": None})[1] == []

    def test_unknown_expression_is_unchecked(self):
        definition = field("A", rules=[rule(expressions=["OnlyIf{IsSet([B])}"])])
        _, findings = run_checks(definition, "x")
        assert findings == [("unchecked", "expression", "Referenced column B is absent", None)]


# CSV escaping

def test_csv_escaping_neutralises_formula_prefixes():
    rows = []

    class Writer:
        def writerow(self, record): rows.append(record)

    write_csv_row(Writer(), {"a": "=SUM(A1)", "b": "-5", "c": "@x", "d": "\tx", "e": "plain", "f": 3})
    assert rows == [{"a": "'=SUM(A1)", "b": "'-5", "c": "'@x", "d": "'\tx", "e": "plain", "f": 3}]
