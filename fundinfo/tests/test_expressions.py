"""Parser, polarity policy and evaluator in fundinfo_expressions."""

from datetime import datetime
from decimal import Decimal as D

import pytest

from fundinfo_expressions import (IGNORED, Context, Parser, Unknown, Unsupported, add_months, bool_value,
                                  combine, mode_for, references)


def parse(text):
    return Parser(text).parse()


def ev(text, values=None, current="X", sensitive=()):
    return Context(values or {}, current, sensitive).evaluate(parse(text))


def is_unknown(value, reason=None, not_applicable=None):
    if not isinstance(value, Unknown): return False
    if reason is not None and reason not in value.reason: return False
    return not_applicable is None or value.not_applicable == not_applicable


# Parser

class TestParser:
    def test_reference(self):
        assert parse("[OFST020000]") == ("ref", "OFST020000")

    @pytest.mark.parametrize("text, value", [
        ('"dormant"', "dormant"),
        ("'single'", "single"),
        (r'"say \"hi\""', 'say "hi"'),
        ("12", D("12")),
        ("1.5", D("1.5")),
        ("1e3", D("1e3")),
        ("TRUE", True),
        ("false", False),
    ])
    def test_literals(self, text, value):
        assert parse(text) == ("literal", value)

    def test_and_binds_tighter_than_or(self):
        node = parse("[A] = 1 or [B] = 2 and [C] = 3")
        assert node[:2] == ("binary", "or")
        assert node[3][:2] == ("binary", "and")

    def test_keywords_are_case_insensitive(self):
        assert parse("IsEmpty([A]) AND isempty([B])")[:2] == ("binary", "and")

    @pytest.mark.parametrize("text, result", [
        ("1 + 2 * 3", D(7)),
        ("(1 + 2) * 3", D(9)),
        ("10 - 2 - 3", D(5)),
        ("8 / 2 / 2", D(2)),
        ("-2 + 5", D(3)),
    ])
    def test_arithmetic_precedence_and_associativity(self, text, result):
        assert ev(text) == result

    def test_parenthesised_list(self):
        node = parse('[A] in ("x", "y")')
        assert node[1] == "in"
        assert node[3] == ("list", (("literal", "x"), ("literal", "y")))

    def test_function_call(self):
        assert parse("IsEqual([A], \"x\")") == ("call", "isequal", (("ref", "A"), ("literal", "x")))

    @pytest.mark.parametrize("name", ["RequiredIf", "RequiredIfNot", "RequiredSetIf", "OnlyIf", "OnlyIfNot", "OnlySetIf"])
    def test_wrappers(self, name):
        assert parse(f"{name}{{IsEmpty([A])}}")[:2] == ("wrapper", name.lower())

    @pytest.mark.parametrize("text, kind", [
        ("[A].Length", "length"),
        ("[A].AddDays(5)", "method"),
        ("[A].AddMonths(-1)", "method"),
        ("[A].addyears(2)", "method"),
    ])
    def test_postfix_members(self, text, kind):
        assert parse(text)[0] == kind

    @pytest.mark.parametrize("text, message", [
        ("ExclusiveIf{IsEmpty([A])}", "Undocumented wrapper"),
        ("HasInvalidUnicodeCharacters([A])", "Unsupported function"),
        ("IsEmpty([A], [B])", "Wrong argument count"),
        ("foo", "Unsupported identifier"),
        ("[A] [B]", "Unexpected token"),
        ("[A] =", "Unexpected end"),
        ("[A] ; 1", "Unrecognised expression syntax"),
        ("[A].Foo", "Unsupported property/method"),
        ("(1, 2", "Unexpected end"),
    ])
    def test_rejects_unsupported_syntax(self, text, message):
        with pytest.raises(Unsupported, match=message):
            parse(text)

    def test_unsupported_is_a_value_error(self):
        assert issubclass(Unsupported, ValueError)


def test_references_collects_every_nested_ref():
    node = parse('RequiredIf{IsEqual([A], "x") and ([B] + 1 > [C].Length or [D] in ([E], "y"))}')
    assert references(node) == {"A", "B", "C", "D", "E"}


# Polarity policy

class TestModeFor:
    def test_override_wins(self):
        text = "IsEmpty([A])"
        assert mode_for(parse(text), text, {text: "invalid_when_true"}) == "invalid_when_true"

    def test_bad_override_is_rejected(self):
        text = "IsEmpty([A])"
        with pytest.raises(Unsupported, match="Invalid expression mode"):
            mode_for(parse(text), text, {text: "sometimes"})

    @pytest.mark.parametrize("text", [
        "OnlyIf{IsEmpty([A])}",
        "[A] < [B]",
        "[A] = 1 and [B] <> 2",
        "[A] >= 1 or [B] != 2",
        "IsEmpty([A]) or IsNotEqual([A], [B])",
    ])
    def test_valid_when_true(self, text):
        assert mode_for(parse(text), text, {}) == "valid_when_true"

    @pytest.mark.parametrize("text", [
        "IsNotSet([A]) and IsNotEmpty([B])",
        "IsSet([A]) and IsEmpty([B])",
        "isset( [A] )   AND   isset([B])",
        "IsEmpty([A]) and IsEmpty([B]) and (IsNotEmpty([C]) or IsNotEmpty([D]))",
        "[A].Length < 10",
    ])
    def test_known_conflict_shapes_are_invalid_when_true(self, text):
        assert mode_for(parse(text), text, {}) == "invalid_when_true"

    @pytest.mark.parametrize("text", ["IsEmpty([A])", "[A].Length >= 10", "IsEqual([A], \"x\") and IsNotEmpty([B])"])
    def test_ambiguous_polarity_raises(self, text):
        with pytest.raises(Unsupported, match="Ambiguous expression polarity"):
            mode_for(parse(text), text, {})


# Helpers

@pytest.mark.parametrize("start, months, expected", [
    (datetime(2024, 1, 31), 1, datetime(2024, 2, 29)),
    (datetime(2023, 1, 31), 1, datetime(2023, 2, 28)),
    (datetime(2024, 1, 15), -1, datetime(2023, 12, 15)),
    (datetime(2024, 2, 29), 12, datetime(2025, 2, 28)),
    (datetime(2024, 3, 31), -13, datetime(2023, 2, 28)),
    (datetime(2024, 5, 10), 0, datetime(2024, 5, 10)),
])
def test_add_months_clamps_to_month_end(start, months, expected):
    assert add_months(start, months) == expected


U = Unknown("gap")
NA = Unknown("n/a", not_applicable=True)


@pytest.mark.parametrize("left, right, operator, expected", [
    (True, True, "and", True),
    (True, False, "and", False),
    (False, U, "and", False),
    (U, False, "and", False),
    (True, U, "and", U),
    (True, True, "or", True),
    (False, False, "or", False),
    (True, U, "or", True),
    (U, True, "or", True),
    (False, U, "or", U),
    (NA, U, "and", U),
    (NA, NA, "or", NA),
])
def test_three_valued_logic(left, right, operator, expected):
    assert combine(left, right, operator) == expected


def test_bool_value_rejects_non_booleans():
    assert bool_value(True) is True
    assert is_unknown(bool_value(D(1)), "did not produce a Boolean")


# Evaluator

class TestEvaluate:
    def test_absent_column_is_unknown(self):
        assert is_unknown(ev("IsEmpty([A])"), "Referenced column A is absent", not_applicable=False)

    @pytest.mark.parametrize("value, empty", [(None, True), ("x", False), (D(0), False), (False, False)])
    def test_isempty_and_isnotempty(self, value, empty):
        assert ev("IsEmpty([A])", {"A": value}) is empty
        assert ev("IsNotEmpty([A])", {"A": value}) is (not empty)

    @pytest.mark.parametrize("value, is_set", [(True, True), (False, False), (None, False), ("Yes", True), ("no", False)])
    def test_isset_and_isnotset(self, value, is_set):
        assert ev("IsSet([A])", {"A": value}) is is_set
        assert ev("IsNotSet([A])", {"A": value}) is (not is_set)

    def test_isset_requires_boolean(self):
        assert is_unknown(ev("IsSet([A])", {"A": D(1)}), "requires a Boolean")

    def test_isequal_with_empty_operands(self):
        assert ev("IsEqual([A], [B])", {"A": None, "B": None}) is True
        assert ev('IsEqual([A], "x")', {"A": None}) is False
        assert ev('IsNotEqual([A], "x")', {"A": None}) is True

    def test_text_comparison_is_case_insensitive_by_default(self):
        assert ev('IsEqual([A], "DORMANT")', {"A": "dormant"}) is True
        assert ev('[A] = "DORMANT"', {"A": "dormant"}) is True

    def test_case_sensitive_reference(self):
        assert ev('IsEqual([A], "DORMANT")', {"A": "dormant"}, sensitive={"A"}) is False
        assert ev('Contains([A], "ORM")', {"A": "dormant"}, sensitive={"A"}) is False

    def test_contains(self):
        assert ev('Contains([A], "ORM")', {"A": "dormant"}) is True
        assert ev('Contains([A], "x")', {"A": None}) is False
        assert is_unknown(ev('Contains([A], "1")', {"A": D(1)}), "requires text")

    def test_ignored_values(self):
        assert ev("IsIgnored([A])", {"A": IGNORED}) is True
        assert ev("IsIgnored([A])", {"A": "x"}) is False
        assert is_unknown(ev("IsEmpty([A])", {"A": IGNORED}), "[IGNORE]")
        assert is_unknown(ev("[A] = 1", {"A": IGNORED}), "[IGNORE]")
        assert is_unknown(ev("[A] + 1", {"A": IGNORED}), "[IGNORE]")

    def test_boolean_compares_with_yes_no_text(self):
        assert ev('[A] = "yes"', {"A": True}) is True
        assert ev('"no" = [A]', {"A": False}) is True

    def test_empty_operand_comparison_is_not_applicable(self):
        assert is_unknown(ev("[A] < [B]", {"A": None, "B": D(1)}), not_applicable=True)

    def test_incompatible_types(self):
        assert is_unknown(ev("[A] < [B]", {"A": D(1), "B": datetime(2024, 1, 1)}), "Incompatible types")

    def test_date_comparison(self):
        assert ev("[A] < [B]", {"A": datetime(2024, 1, 1), "B": datetime(2024, 6, 1)}) is True

    def test_in_list(self):
        assert ev('[A] in ("x", "y")', {"A": "Y"}) is True
        assert ev('[A] in ("x", "y")', {"A": "z"}) is False
        assert ev('[A] in "z"', {"A": "z"}) is True
        assert is_unknown(ev('[A] in ("x", "y")', {"A": None}), not_applicable=True)

    def test_arithmetic_edge_cases(self):
        assert ev("[A] * 2", {"A": D("1.5")}) == D(3)
        assert is_unknown(ev("[A] + 1", {"A": None}), not_applicable=True)
        assert is_unknown(ev("[A] + 1", {"A": "x"}), "numeric operands")
        assert is_unknown(ev("[A] / 0", {"A": D(1)}), "Invalid arithmetic")

    def test_unary(self):
        assert ev("not IsEmpty([A])", {"A": "x"}) is True
        assert ev("-[A]", {"A": D(2)}) == D(-2)
        assert is_unknown(ev("not [A]", {"A": D(1)}), "did not produce a Boolean")
        assert is_unknown(ev("-[A]", {"A": "x"}), "Numeric unary")

    def test_length_counts_utf16_code_units(self):
        assert ev("[A].Length", {"A": "abc"}) == D(3)
        assert ev("[A].Length", {"A": "a😀"}) == D(3)
        assert ev("[A].Length", {"A": None}) == D(0)
        assert is_unknown(ev("[A].Length", {"A": D(5)}), "requires a string")

    def test_date_methods(self):
        start = datetime(2024, 1, 31)
        assert ev("[A].AddDays(1)", {"A": start}) == datetime(2024, 2, 1)
        assert ev("[A].AddMonths(1)", {"A": start}) == datetime(2024, 2, 29)
        assert ev("[A].AddYears(-1)", {"A": start}) == datetime(2023, 1, 31)
        assert is_unknown(ev("[A].AddDays(1)", {"A": None}), not_applicable=True)
        assert is_unknown(ev("[A].AddDays(1.5)", {"A": start}), "integer")
        assert is_unknown(ev("[A].AddDays(1)", {"A": "2024-01-01"}), "integer")

    def test_short_circuit_hides_absent_columns(self):
        assert ev("IsEmpty([A]) and [Missing] = 1", {"A": "x"}) is False
        assert ev("IsNotEmpty([A]) or [Missing] = 1", {"A": "x"}) is True

    def test_unknown_propagates_when_it_decides_the_result(self):
        assert is_unknown(ev("IsNotEmpty([A]) and [Missing] = 1", {"A": "x"}), "Missing")


# Wrappers. X is the current field, C drives the condition.

@pytest.mark.parametrize("wrapper, current, condition, expected", [
    ("RequiredIf", "v", True, True),
    ("RequiredIf", None, True, False),
    ("RequiredIf", None, False, True),
    ("RequiredIfNot", "v", False, True),
    ("RequiredIfNot", None, False, False),
    ("RequiredIfNot", None, True, True),
    ("RequiredSetIf", True, True, True),
    ("RequiredSetIf", False, True, False),
    ("RequiredSetIf", None, True, False),
    ("RequiredSetIf", False, False, True),
    ("OnlyIf", None, False, True),
    ("OnlyIf", "v", True, True),
    ("OnlyIf", "v", False, False),
    ("OnlyIfNot", None, True, True),
    ("OnlyIfNot", "v", True, False),
    ("OnlyIfNot", "v", False, True),
    ("OnlySetIf", None, False, True),
    ("OnlySetIf", False, False, True),
    ("OnlySetIf", True, False, False),
    ("OnlySetIf", True, True, True),
])
def test_wrapper_truth_table(wrapper, current, condition, expected):
    assert ev(f"{wrapper}{{IsSet([C])}}", {"X": current, "C": condition}) is expected


def test_wrapper_on_ignored_target_is_unknown():
    assert is_unknown(ev("OnlyIf{IsSet([C])}", {"X": IGNORED, "C": True}), "Target value is [IGNORE]")


def test_wrapper_on_unknown_target_returns_it():
    reason = Unknown("formula")
    assert ev("OnlyIf{IsSet([C])}", {"X": reason, "C": True}) is reason


def test_wrapper_with_unknown_condition():
    assert is_unknown(ev("OnlyIf{IsSet([C])}", {"X": "v"}), "Referenced column C is absent")
