"""validate() end to end on generated workbooks, plus the command line."""

import json
import subprocess
import sys
from datetime import date, datetime

import pytest

from conftest import MODES, VALIDATE_DIR, field, rule

ISIN = "LU2055195056"
ID = field("OFST020000", fmt="ISIN")


def sheet(codes, *rows):
    return {"S": (["OFST020000", *codes], [[ISIN, *row] for row in rows])}


class TestRowStatus:
    def test_clean_row_is_valid(self, run_validation):
        r = run_validation(sheet(["A"], ["x"]), [ID, field("A")])
        assert r.code == 0
        assert r.issues == []
        assert r.rows == [{"sheet": "S", "excel_row": "3", "row_id": ISIN, "status": "valid", "fatal": "0", "error": "0",
                           "warning": "0", "suspicious": "0", "unchecked": "0", "coverage_gaps": "0", "ignored_cells": "0"}]
        s = r.summary
        assert (s["valid"], s["coverage_complete"], s["scan_complete"], s["rows_checked"]) == (True, True, True, 1)
        assert s["row_status_counts"] == {"valid": 1}

    def test_type_error_is_fatal(self, run_validation):
        r = run_validation(sheet(["A"], ["abc"]), [ID, field("A", dtype="Double")])
        assert r.code == 1
        [issue] = r.find(check="type_or_format")
        assert (issue["cell"], issue["code"], issue["name"], issue["value"]) == ("B3", "A", "Name A", "abc")
        assert (issue["outcome"], issue["severity"], issue["rule_index"]) == ("violation", "Fatal", "0")
        assert r.rows[0]["status"] == "invalid"
        assert r.summary["violation_counts"] == {"Fatal": 1}

    @pytest.mark.parametrize("severity, status, code", [
        ("Fatal", "invalid", 1),
        ("Error", "invalid", 1),
        ("Warning", "valid_with_warnings", 0),
        ("Suspicious", "valid_with_warnings", 0),
    ])
    def test_severity_decides_status_and_exit_code(self, run_validation, severity, status, code):
        r = run_validation(sheet(["A"], ["z"]), [ID, field("A", rules=[rule(severity=severity, options=["x"])])])
        assert (r.rows[0]["status"], r.code) == (status, code)
        assert r.rows[0][severity.lower()] == "1"

    def test_fatal_beats_warnings_and_gaps(self, run_validation):
        fields = [ID, field("A", dtype="Double"), field("B", rules=[rule(severity="Warning", options=["x"])])]
        r = run_validation(sheet(["A", "B", "NORULES"], ["abc", "z", "1"]), fields)
        assert r.rows[0]["status"] == "invalid"

    def test_coverage_gap_makes_rows_incomplete(self, run_validation):
        r = run_validation(sheet(["NORULES"], ["x"]), [ID])
        assert r.code == 2
        assert r.rows[0]["status"] == "incomplete"
        assert r.rows[0]["coverage_gaps"] == "1"
        assert r.summary["sheets"][0]["missing_rule_codes"] == ["NORULES"]
        assert r.summary["coverage_gaps"] == [{"sheet": "S", "code": "NORULES", "kind": "missing_rules",
                                               "reason": "No field definition in supplied rules JSON"}]
        assert r.summary["valid"] is False

    def test_compile_gaps_reach_the_summary(self, run_validation):
        r = run_validation(sheet(["A"], ["x"]), [ID, field("A", fmt="CitiCode")])
        assert [g["kind"] for g in r.summary["coverage_gaps"]] == ["format"]
        assert r.rows[0]["status"] == "incomplete"


class TestLayout:
    def test_duplicate_headers_are_excluded(self, run_validation):
        r = run_validation(sheet(["A", "A"], ["abc", "def"]), [ID, field("A", dtype="Double")])
        assert r.find(code="A") == []
        assert r.summary["sheets"][0]["duplicate_codes"] == ["A"]
        assert any(g["kind"] == "duplicate_header" for g in r.summary["coverage_gaps"])

    @pytest.mark.parametrize("codes, row", [(["A", None, "B"], ["x", "y", "z"]), (["A", None], ["x", "y"])])
    def test_populated_column_without_header_is_unchecked(self, run_validation, codes, row):
        r = run_validation(sheet(codes, row, row), [ID, field("A"), field("B")])
        assert r.rows[0]["unchecked"] == "1"
        [gap] = r.summary["unevaluated_checks"]
        assert (gap["column"], gap["occurrences"], gap["first_excel_row"]) == ("C", 2, 3)
        assert r.code == 2

    def test_blank_rows_are_skipped(self, run_validation):
        rows = [[ISIN, "x"], [None, None], [" ", "[N/A]"], [ISIN, "y"]]
        r = run_validation({"S": (["OFST020000", "A"], rows)}, [ID, field("A")])
        assert [row["excel_row"] for row in r.rows] == ["3", "6"]
        assert r.summary["sheets"][0]["blank_rows_skipped"] == 2

    def test_row_id_comes_from_isin_column(self, run_validation):
        r = run_validation({"S": (["A", "OFST020000"], [["abc", ISIN]])}, [ID, field("A", dtype="Double")])
        assert r.issues[0]["row_id"] == ISIN
        assert r.issues[0]["cell"] == "A3"

    def test_row_id_is_blank_without_isin_column(self, run_validation):
        r = run_validation({"S": (["A"], [["abc"]])}, [field("A", dtype="Double")])
        assert r.issues[0]["row_id"] == ""

    def test_header_without_codes_is_rejected(self, run_validation):
        with pytest.raises(ValueError, match="No field codes"):
            run_validation({"S": ([None, None], [["x", "y"]])}, [ID])

    def test_sheet_selection(self, run_validation):
        sheets = {"S1": (["A"], [["abc"]]), "S2": (["A"], [["def"]])}
        r = run_validation(sheets, [field("A", dtype="Double")], sheet=["S2"])
        assert [i["sheet"] for i in r.issues] == ["S2"]
        assert [s["sheet"] for s in r.summary["sheets"]] == ["S2"]

    def test_all_sheets_by_default(self, run_validation):
        sheets = {"S1": (["A"], [["abc"]]), "S2": (["A"], [["def"]])}
        r = run_validation(sheets, [field("A", dtype="Double")])
        assert [i["sheet"] for i in r.issues] == ["S1", "S2"]

    def test_unknown_sheet_is_rejected(self, run_validation):
        with pytest.raises(ValueError, match="Unknown sheet"):
            run_validation(sheet(["A"], ["x"]), [ID, field("A")], sheet=["Nope"])


class TestLimit:
    def test_limit_stops_the_scan(self, run_validation):
        r = run_validation(sheet(["A"], ["x"], ["x"], ["x"]), [ID, field("A")], limit=2)
        s = r.summary
        assert (s["rows_checked"], s["sheets"][0]["limited"], s["scan_complete"], s["valid"]) == (2, True, False, False)
        assert r.code == 2

    def test_limit_equal_to_row_count_is_complete(self, run_validation):
        r = run_validation(sheet(["A"], ["x"], ["x"]), [ID, field("A")], limit=2)
        assert r.summary["scan_complete"] is True
        assert r.code == 0


class TestCellKinds:
    def test_formula_cells_are_not_executed(self, run_validation):
        r = run_validation(sheet(["A"], ["=1+1"]), [ID, field("A", dtype="Double")])
        [issue] = r.issues
        assert (issue["outcome"], issue["check"]) == ("unchecked", "cell_value")
        assert issue["message"].startswith("Formula cell")
        assert issue["value"] == "'=1+1"
        assert r.rows[0]["status"] == "incomplete"

    def test_excel_error_cells(self, run_validation):
        r = run_validation(sheet(["A"], ["#N/A"]), [ID, field("A", dtype="Double")])
        assert r.issues[0]["message"] == "Excel error cell: #N/A"

    def test_ignore_marker_skips_the_cell(self, run_validation):
        r = run_validation(sheet(["A"], ["[IGNORE]"]), [ID, field("A", dtype="Double", required=["B"])])
        assert r.issues == []
        assert r.rows[0]["ignored_cells"] == "1"
        assert r.summary["ignored_cells"] == 1

    def test_ignore_marker_blocks_dependent_checks(self, run_validation):
        fields = [ID, field("A"), field("B", rules=[rule(expressions=["OnlyIf{IsNotEmpty([A])}"])])]
        r = run_validation(sheet(["A", "B"], ["[IGNORE]", "y"]), fields)
        [issue] = r.issues
        assert (issue["code"], issue["outcome"]) == ("B", "unchecked")
        assert "[IGNORE]" in issue["message"]

    def test_excel_booleans_dates_and_percentages(self, run_validation):
        fields = [ID, field("FLAG", dtype="Boolean"), field("DAY", dtype="DateTime", fmt="yyyy-MM-dd"),
                  field("PCT", dtype="Double", rules=[rule(constraints={"Min": "0", "Max": "1"})])]
        r = run_validation(sheet(["FLAG", "DAY", "PCT"], [True, date(2024, 5, 6), 0.05]), fields)
        assert r.issues == []

    def test_datetime_with_time_on_date_field(self, run_validation):
        fields = [ID, field("DAY", dtype="DateTime", fmt="yyyy-MM-dd")]
        r = run_validation(sheet(["DAY"], [datetime(2024, 5, 6, 13, 0)]), fields)
        assert r.issues[0]["message"] == "Expected a date without a time component"

    def test_report_escapes_formula_like_text(self, run_validation):
        r = run_validation(sheet(["A"], ["-abc"]), [ID, field("A", rules=[rule(options=["x"])])])
        assert r.issues[0]["value"] == "'-abc"


class TestCrossField:
    def test_expression_on_excel_boolean(self, run_validation):
        fields = [ID, field("A", rules=[rule(expressions=["OnlyIf{IsSet([B])}"])]), field("B", dtype="Boolean")]
        r = run_validation(sheet(["A", "B"], ["x", True], ["x", False], [None, False]), fields)
        assert [(i["excel_row"], i["check"], i["expression"]) for i in r.issues] == [("4", "expression", "OnlyIf{IsSet([B])}")]

    def test_required_fields(self, run_validation):
        fields = [ID, field("A", required=["A", "B"]), field("B")]
        r = run_validation(sheet(["A", "B"], ["x", None], ["x", "y"], [None, None]), fields)
        assert [(i["excel_row"], i["check"], i["severity"]) for i in r.issues] == [("3", "required", "Fatal")]
        assert r.issues[0]["message"] == "Value required in [B] while this field is set"

    def test_required_field_column_absent(self, run_validation):
        r = run_validation(sheet(["A"], ["x"]), [ID, field("A", required=["B"])])
        assert (r.issues[0]["outcome"], r.issues[0]["message"]) == ("unchecked", "Referenced column B is absent")

    def test_column_without_rules_still_feeds_expressions(self, run_validation):
        fields = [ID, field("A", rules=[rule(expressions=["[A] > [N]"])], dtype="Double")]
        r = run_validation(sheet(["A", "N"], [5, 3], [2, 3]), fields)
        assert [i["excel_row"] for i in r.find(check="expression")] == ["4"]

    def test_case_sensitive_reference(self, run_validation):
        fields = [ID, field("A", rules=[rule(expressions=['OnlyIf{IsEqual([B], "X")}'])]), field("B", sensitive=True)]
        r = run_validation(sheet(["A", "B"], ["v", "X"], ["v", "x"]), fields)
        assert [i["excel_row"] for i in r.issues] == ["4"]

    def test_unchecked_reasons_are_aggregated(self, run_validation):
        fields = [ID, field("A", rules=[rule(expressions=["OnlyIf{IsSet([MISSING])}"])])]
        # Row 5 is empty, so OnlyIf passes without reading the absent column.
        r = run_validation(sheet(["A"], ["x"], ["x"], [None]), fields)
        [gap] = r.summary["unevaluated_checks"]
        assert (gap["code"], gap["occurrences"], gap["first_excel_row"]) == ("A", 2, 3)
        assert r.summary["unchecked_occurrences"] == 2
        assert [row["status"] for row in r.rows] == ["incomplete", "incomplete", "valid"]


class TestExpressionModes:
    def test_override_file_sets_polarity(self, run_validation):
        text = "IsEmpty([B])"
        fields = [ID, field("A", rules=[rule(expressions=[text])]), field("B")]
        r = run_validation(sheet(["A", "B"], ["x", None], ["x", "y"]), fields, modes={text: "invalid_when_true"})
        assert [i["excel_row"] for i in r.issues] == ["3"]
        assert r.summary["expression_mode_overrides"] == {text: "invalid_when_true"}

    def test_without_override_ambiguous_expression_is_a_gap(self, run_validation):
        fields = [ID, field("A", rules=[rule(expressions=["IsEmpty([B])"])]), field("B")]
        r = run_validation(sheet(["A", "B"], ["x", None]), fields)
        assert r.issues == []
        assert r.summary["coverage_gaps"][0]["kind"] == "expression"

    @pytest.mark.parametrize("modes", [{"x": "maybe"}, ["x"]])
    def test_bad_override_file_is_rejected(self, run_validation, modes):
        with pytest.raises(ValueError, match="Expression modes"):
            run_validation(sheet(["A"], ["x"]), [ID, field("A")], modes=modes)

    @pytest.mark.parametrize("lifecycle, start, end, fires", [
        ("dormant", date(2024, 1, 1), date(2024, 6, 1), True),
        ("dormant", date(2024, 1, 1), None, False),
        ("Dormant", date(2024, 6, 1), date(2024, 1, 1), False),
        ("active", date(2024, 1, 1), date(2024, 6, 1), False),
    ])
    def test_shipped_lifecycle_override(self, run_validation, lifecycle, start, end, fires):
        modes = json.loads(MODES.read_text(encoding="utf-8"))
        dormant = next(e for e in modes if e.startswith('IsEqual([OFST020545],"dormant")'))
        fields = [ID, field("OFST020545", rules=[rule(expressions=[dormant])]),
                  field("OFST020562", dtype="DateTime", fmt="yyyy-MM-dd"),
                  field("OFST020563", dtype="DateTime", fmt="yyyy-MM-dd")]
        r = run_validation(sheet(["OFST020545", "OFST020562", "OFST020563"], [lifecycle, start, end]), fields, modes=modes)
        assert bool(r.find(code="OFST020545", outcome="violation")) is fires


class TestRelativeDates:
    def test_today_bound_without_skip(self, run_validation):
        fields = [ID, field("D", dtype="DateTime", fmt="yyyy-MM-dd", rules=[rule(constraints={"MaxDate": "Today"})])]
        r = run_validation(sheet(["D"], [date(2026, 1, 16)], [date(2026, 1, 15)]), fields, as_of=date(2026, 1, 15))
        assert [i["excel_row"] for i in r.issues] == ["3"]
        assert r.summary["as_of"] == "2026-01-15"

    def test_today_bound_with_skip(self, run_validation):
        fields = [ID, field("D", dtype="DateTime", fmt="yyyy-MM-dd", rules=[rule(constraints={"MaxDate": "Today"})])]
        r = run_validation(sheet(["D"], [date(2026, 1, 16)]), fields, skip_relative_dates=True)
        assert r.issues == []
        assert r.summary["skip_relative_dates"] is True


class TestOutputs:
    def test_summary_metadata(self, run_validation, tmp_path):
        import hashlib
        r = run_validation(sheet(["A"], ["x"]), [ID, field("A")])
        s = r.summary
        assert s["rules_sha256"] == hashlib.sha256((tmp_path / "rules.json").read_bytes()).hexdigest()
        assert (s["header_row"], s["data_start_row"]) == (1, 3)
        assert len(s["notes"]) >= 1

    def test_no_temporary_files_remain(self, run_validation):
        r = run_validation(sheet(["A"], ["x"]), [ID, field("A")])
        assert sorted(p.name for p in r.output.iterdir()) == ["issues.csv", "rows.csv", "summary.json"]

    def test_rerun_overwrites(self, run_validation):
        run_validation(sheet(["A"], ["abc"]), [ID, field("A", dtype="Double")])
        r = run_validation(sheet(["A"], ["1"]), [ID, field("A", dtype="Double")])
        assert r.issues == []


class TestCommandLine:
    def cli(self, *args, cwd):
        script = VALIDATE_DIR / "validate_fundinfo.py"
        return subprocess.run([sys.executable, str(script), *map(str, args)], cwd=cwd, capture_output=True, text=True)

    def test_bad_rules_file_exits_3(self, tmp_path):
        rules = tmp_path / "rules.json"
        rules.write_text('{"fields": {}}', encoding="utf-8")
        result = self.cli(tmp_path / "missing.xlsx", "--rules", rules, "--output", tmp_path / "out", cwd=tmp_path)
        assert result.returncode == 3
        assert "Cannot validate" in result.stderr

    def test_missing_workbook_exits_3(self, tmp_path):
        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps({"fields": {"A": field("A")}}), encoding="utf-8")
        result = self.cli(tmp_path / "missing.xlsx", "--rules", rules, "--output", tmp_path / "out", cwd=tmp_path)
        assert result.returncode == 3

    @pytest.mark.parametrize("args", [["--header-row", "0"], ["--data-start-row", "1"], ["--limit", "0"],
                                      ["--as-of", "15/01/2026"]])
    def test_argument_errors_exit_2(self, tmp_path, args):
        result = self.cli(*args, cwd=tmp_path)
        assert result.returncode == 2
        assert "usage" in result.stderr
