import csv
import json
import os
import sys
from argparse import Namespace
from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook

ROOT = Path(os.environ.get("FUNDINFO_ROOT", Path(__file__).resolve().parents[1]))
VALIDATE_DIR = ROOT / "Fundinfo" / "2_validate"
sys.path.insert(0, str(VALIDATE_DIR))

CATALOGUE = ROOT / "output" / "fundinfo" / "fundinfo.json"
MODES = VALIDATE_DIR / "expression_modes.json"


def rule(severity="Fatal", constraints=None, options=(), nested="-", expressions=()):
    return {
        "severity": severity,
        "constraints": {"Regex": "-"} if constraints is None else constraints,
        "data_options": list(options) if options is not None else None,
        "nested_data_option_keys": nested,
        "expressions": [e if isinstance(e, dict) else {"expression": e, "error_message": f"failed: {e}"} for e in expressions],
    }


def field(code, dtype="String", fmt="-", multi=False, sensitive=False, rules=None, required=(), warnings=()):
    return {
        "code": code,
        "metadata": {"Code": code, "Name": f"Name {code}", "Type": dtype, "ValueFormats": fmt,
                     "IsMultiItem": multi, "IsCaseSensitive": sensitive},
        "rules": [rule()] if rules is None else rules,
        "required_fields": list(required),
        "parse_warnings": list(warnings),
    }


class Run:
    def __init__(self, code, output):
        self.code = code
        self.output = output
        self.issues = read_csv(output / "issues.csv")
        self.rows = read_csv(output / "rows.csv")
        self.summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))

    def find(self, **match):
        return [i for i in self.issues if all(i[k] == str(v) for k, v in match.items())]


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture
def run_validation(tmp_path):
    """Write a workbook and a rules file, run validate(), and return the parsed outputs.

    `sheets` maps sheet name to (codes, rows). Row 1 holds codes, row 2 names, data starts on row 3.
    """
    from validate_fundinfo import validate

    def run(sheets, fields, modes=None, **options):
        book = Workbook()
        book.remove(book.active)
        for name, (codes, rows) in sheets.items():
            sheet = book.create_sheet(name)
            sheet.append(codes)
            sheet.append([f"Name {c}" if c else None for c in codes])
            for row in rows:
                sheet.append(row)
        workbook = tmp_path / "book.xlsx"
        book.save(workbook)
        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps({"fields": {f["code"]: f for f in fields}}), encoding="utf-8")
        modes_path = None
        if modes is not None:
            modes_path = tmp_path / "modes.json"
            modes_path.write_text(json.dumps(modes), encoding="utf-8")
        output = tmp_path / "out"
        args = Namespace(workbook=workbook, rules=rules, output=output, sheet=options.get("sheet"),
                         header_row=1, data_start_row=3, as_of=options.get("as_of", date(2026, 1, 15)),
                         timezone="Europe/Athens", skip_relative_dates=options.get("skip_relative_dates", False),
                         limit=options.get("limit"), expression_modes=modes_path)
        return Run(validate(args), output)

    return run
