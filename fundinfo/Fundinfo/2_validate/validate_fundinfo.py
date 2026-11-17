#!/usr/bin/env python3
"""Validate XLSX rows against the structured FE fundinfo rule export."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from fundinfo_expressions import Context, IGNORED, Parser, Unknown, Unsupported, add_months, bool_value, mode_for

DATE_FORMATS = {
    "yyyy-MM-dd": (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d"),
    "HH:mm": (r"\d{2}:\d{2}", "%H:%M"),
    "MM-dd": (r"\d{2}-\d{2}", "%m-%d"),
    "yyyy-MM-dd HH:mm": (r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", "%Y-%m-%d %H:%M"),
    "yyyy": (r"\d{4}", "%Y"),
}
NAMED_FORMATS = {"ISIN", "LegalEntityIdentifier"}
TYPES = {"String", "Double", "Int", "Long", "Boolean", "DateTime"}
UNBOUNDED = {"", "-", "inf", "+inf", "-inf"}

# A bound with no anchor, such as "+5 Years", counts from Today too.
RELATIVE_DATE = re.compile(r"\s*(?:(?:Today|BeginOfYear)\b|[+-]\s*\d+\s*(?:Years?|Months?|Days?)\b)", re.I)

CONSTRAINTS = {"Min", "Max", "MinLength", "MaxLength", "MinDate", "MaxDate", "Regex", "MultiItemMinLength", "MultiItemMaxLength"}


def empty(value):
    return value is None or (isinstance(value, str) and (not value.strip() or value.strip() == "[N/A]"))


def text_value(value):
    if value is None: return ""
    if value is IGNORED: return "[IGNORE]"
    if isinstance(value, (date, datetime, time)): return value.isoformat()
    if isinstance(value, bool): return "yes" if value else "no"
    return str(value)


def number(value):
    if isinstance(value, bool): raise ValueError("Expected a number, received a Boolean")
    raw = str(value).strip()
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?", raw):
        raise ValueError("Expected a decimal number (percentage cells use their underlying decimal value)")
    result = Decimal(raw)
    if not result.is_finite(): raise ValueError("Number must be finite")
    return result


def parse_date(value, fmt):
    if fmt not in DATE_FORMATS: raise Unsupported(f"Unsupported date format: {fmt}")
    if isinstance(value, datetime):
        if fmt == "yyyy-MM-dd" and value.time() != time():
            raise ValueError("Expected a date without a time component")
        if fmt in ("HH:mm", "yyyy-MM-dd HH:mm") and (value.second or value.microsecond):
            raise ValueError("Expected minute precision with no seconds")
        if fmt == "HH:mm": value = value.time()
        elif fmt == "MM-dd": return datetime(2000, value.month, value.day)
        elif fmt == "yyyy": return datetime(value.year, 1, 1)
        else: return value.replace(tzinfo=None)
    if isinstance(value, date):
        if fmt == "HH:mm": raise ValueError("Expected a time")
        if fmt == "MM-dd": return datetime(2000, value.month, value.day)
        if fmt == "yyyy": return datetime(value.year, 1, 1)
        return datetime(value.year, value.month, value.day)
    if isinstance(value, time):
        if fmt != "HH:mm" or value.second or value.microsecond:
            raise ValueError("Expected HH:mm with no seconds")
        return datetime(2000, 1, 1, value.hour, value.minute)
    raw = str(value).strip()
    pattern, python_format = DATE_FORMATS[fmt]
    if not re.fullmatch(pattern, raw): raise ValueError(f"Expected format {fmt}")
    try:
        if fmt == "MM-dd": return datetime.strptime("2000-" + raw, "%Y-%m-%d")
        result = datetime.strptime(raw, python_format)
        if fmt == "HH:mm": result = result.replace(year=2000)
        return result
    except ValueError as exc:
        raise ValueError(f"Invalid calendar value for {fmt}: {raw}") from exc


def date_bound(raw, fmt, as_of):
    raw = raw.strip()
    if raw.lower() in UNBOUNDED: return None
    relative = re.fullmatch(r"(Today|BeginOfYear)?\s*((?:[+-]\s*\d+\s*(?:Years?|Months?|Days?)\s*)*)", raw, re.I)
    if relative and (relative[1] or relative[2]):
        value = datetime.combine(as_of, time())
        if (relative[1] or "").lower() == "beginofyear": value = value.replace(month=1, day=1)
        for sign, count, unit in re.findall(r"([+-])\s*(\d+)\s*(Years?|Months?|Days?)", relative[2], re.I):
            count = int(count) * (-1 if sign == "-" else 1)
            if unit.lower().startswith("day"): value += timedelta(days=count)
            else: value = add_months(value, count * (12 if unit.lower().startswith("year") else 1))
        return value
    return parse_date(raw, fmt)


def valid_identifier(value, fmt):
    # Syntax + checksum only. No registry lookup or existence assertion.
    value = text_value(value).upper()
    if fmt == "ISIN":
        if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", value): return False
        digits = "".join(str(ord(c) - 55) if c.isalpha() else c for c in value)
        total = 0
        for i, digit in enumerate(reversed(digits)):
            n = int(digit) * (2 if i % 2 else 1)
            total += n // 10 + n % 10
        return total % 10 == 0
    if fmt == "LegalEntityIdentifier":
        if not re.fullmatch(r"[A-Z0-9]{18}[0-9]{2}", value): return False
        digits = "".join(str(ord(c) - 55) if c.isalpha() else c for c in value)
        return int(digits) % 97 == 1
    raise Unsupported(f"Unsupported identifier format: {fmt}")


def coerce(value, dtype, fmt="-"):
    if empty(value): return None
    if value == "[IGNORE]": return IGNORED
    if dtype not in TYPES: raise Unsupported(f"Unsupported type: {dtype}")
    if dtype == "Boolean":
        if isinstance(value, bool): return value
        if isinstance(value, str) and value.strip().casefold() in ("yes", "no"):
            return value.strip().casefold() == "yes"
        raise ValueError("Expected yes/no or an Excel Boolean")
    if dtype in ("Double", "Int", "Long"):
        result = number(value)
        if dtype in ("Int", "Long"):
            bits = 32 if dtype == "Int" else 64
            if result != result.to_integral_value(): raise ValueError("Expected a whole number")
            if not -(2 ** (bits-1)) <= result < 2 ** (bits-1): raise ValueError(f"Integer outside signed {bits}-bit range")
        return result
    if dtype == "DateTime" or fmt in DATE_FORMATS:
        return parse_date(value, fmt if fmt != "-" else "yyyy-MM-dd")
    return text_value(value)


@dataclass
class Check:
    kind: str
    severity: str
    rule_index: int
    value: object
    message: str = ""
    expression: str = ""


@dataclass
class FieldPlan:
    code: str
    name: str
    dtype: str
    fmt: str
    sensitive: bool
    separator: str | None = None
    item_type: str | None = None
    checks: list = field(default_factory=list)
    gaps: list = field(default_factory=list)


def compile_field(code, definition, as_of, overrides, skip_relative=False):
    meta = definition["metadata"]
    plan = FieldPlan(code, meta.get("Name", code), meta["Type"], meta.get("ValueFormats", "-"), meta.get("IsCaseSensitive") is True)

    def gap(kind, reason, rule_index=None, expression=""):
        plan.gaps.append({"code": code, "kind": kind, "rule_index": rule_index, "reason": reason, "expression": expression})

    multi = meta.get("IsMultiItem", False)
    if multi is not False:
        match = re.fullmatch(r"Separator:\s*'(.+)'\s+(\w+)", str(multi))
        if match:
            plan.separator, plan.item_type = match.groups()
        else: gap("metadata", f"Unsupported IsMultiItem metadata: {multi}")
    if plan.dtype not in TYPES: gap("type", f"Unsupported type: {plan.dtype}")
    if plan.item_type and plan.item_type not in TYPES: gap("type", f"Unsupported item type: {plan.item_type}")
    if plan.fmt not in ("-", "", *DATE_FORMATS, *NAMED_FORMATS):
        gap("format", f"Named format has no implemented validator: {plan.fmt}")
    for warning in definition.get("parse_warnings", []): gap("source_parse_warning", warning)
    if not definition.get("rules"): gap("rules", "No validation rule groups in source")

    for index, rule in enumerate(definition.get("rules", []), 1):
        severity = rule["severity"]
        if severity not in ("Fatal", "Error", "Warning", "Suspicious"):
            gap("severity", f"Unknown severity: {severity}", index)
        for key, raw in rule["constraints"].items():
            if key not in CONSTRAINTS:
                gap("constraint", f"Unknown constraint: {key}", index)
                continue
            if raw is None or str(raw).lower().strip() in UNBOUNDED: continue
            if skip_relative and RELATIVE_DATE.match(str(raw)): continue
            try:
                if key == "Regex":
                    if "\\\\" in raw or "(?<" in raw or "\\p{" in raw:
                        raise Unsupported("Regex has ambiguous escaping or unsupported .NET constructs")
                    compiled = re.compile(raw, 0 if plan.sensitive else re.IGNORECASE)
                    plan.checks.append(Check("regex", severity, index, compiled))
                    continue
                if key.startswith("MultiItem"):
                    if not plan.item_type: raise Unsupported("Item bound without supported multi-item metadata")
                    target = "item"
                    dtype = plan.item_type
                elif key in ("MinDate", "MaxDate"):
                    target, dtype = "scalar", "DateTime"
                elif key.endswith("Length"):
                    target, dtype = "text", "String"
                else:
                    target, dtype = "scalar", plan.dtype
                value = date_bound(raw, plan.fmt if plan.fmt not in ("-", "") else "yyyy-MM-dd", as_of) if dtype == "DateTime" else number(raw)
                if dtype == "String" and (value < 0 or value != value.to_integral_value()):
                    raise Unsupported("String length bound must be a nonnegative integer")
                plan.checks.append(Check("bound", severity, index, (key, target, dtype, value, raw)))
            except (ValueError, ArithmeticError, re.error) as exc:
                gap("constraint", f"{key}: {exc}", index)

        options = rule.get("data_options")
        parents = re.findall(r"OF[A-Za-z0-9]+(?:\([^)]*\))?", rule.get("nested_data_option_keys") or "")
        if options is None: gap("data_options", "Allowed values could not be parsed from source", index)
        elif options:
            nested = len(options) == 1 and isinstance(options[0], dict)
            if nested and not parents:
                gap("data_options", "Nested options have no parent field keys", index)
            elif not nested and any(isinstance(item, (list, dict)) for item in options):
                gap("data_options", "Unrecognised allowed-values structure", index)
            else:
                plan.checks.append(Check("options", severity, index, (options[0] if nested else options, parents)))
        for entry in rule.get("expressions", []):
            expression = entry["expression"]
            try:
                node = Parser(expression).parse()
                mode = mode_for(node, expression, overrides)
                plan.checks.append(Check("expression", severity, index, (node, mode), entry.get("error_message") or expression, expression))
            except (ValueError, SyntaxError, RecursionError) as exc:
                gap("expression", str(exc), index, expression)

    # The page lists the field itself among its required fields, so it is skipped.
    for other in definition.get("required_fields", []):
        if other != code:
            plan.checks.append(Check("required", "Fatal", 0, other, f"Value required in [{other}] while this field is set"))
    return plan


def prepare_cell(raw, plan):
    """Return the expression value, item values, and type/format findings."""
    if isinstance(raw, Unknown): return raw, [], []
    if empty(raw): return None, [], []
    if raw == "[IGNORE]": return IGNORED, [], []
    pieces = str(raw).split(plan.separator) if plan.separator else [raw]
    items, errors = [], []
    for i, piece in enumerate(pieces, 1):
        item = piece.strip() if plan.separator and isinstance(piece, str) else piece
        try:
            if empty(item) and plan.separator:
                raise ValueError("Empty item inside a multi-value cell")
            typed = coerce(item, plan.item_type or plan.dtype, plan.fmt)
            if plan.fmt in NAMED_FORMATS and not valid_identifier(item, plan.fmt):
                raise ValueError(f"Invalid {plan.fmt} syntax or checksum")
        except Unsupported as exc:
            typed = Unknown(str(exc))
        except (ValueError, InvalidOperation) as exc:
            errors.append((i if plan.separator else None, str(exc)))
            typed = Unknown("Cell has an invalid type or format")
        items.append((item, typed))
    expression_value = text_value(raw) if plan.separator else items[0][1]
    return expression_value, items, errors


def option_match(value, allowed, sensitive):
    value = text_value(value)
    if not sensitive: value = value.casefold()
    return any(value == (text_value(item) if sensitive else text_value(item).casefold()) for item in allowed)


def check_cell(check, raw, value, items, plan, context):
    """Yield (outcome, message, item_number); outcome is violation or unchecked."""
    if check.kind == "expression":
        node, mode = check.value
        if empty(raw) and node[0] == "binary" and node[2][0] == "length":
            return  # Optional text is checked for minimum length only when supplied.
        result = bool_value(context.evaluate(node))
        if isinstance(result, Unknown):
            if not result.not_applicable: yield "unchecked", result.reason, None
        elif result != (mode == "valid_when_true"):
            yield "violation", check.message, None
        return
    if empty(raw) or value is IGNORED: return
    if check.kind == "required":
        other = context.get(check.value)
        if isinstance(other, Unknown): yield "unchecked", other.reason, None
        elif other is None: yield "violation", check.message, None
        return
    if isinstance(value, Unknown):
        yield "unchecked", value.reason, None
        return
    if check.kind == "regex":
        if not check.value.search(text_value(raw)):
            yield "violation", f"Value does not match regex {check.value.pattern}", None
    elif check.kind == "bound":
        key, target, dtype, bound, displayed = check.value
        candidates = items if target == "item" else [(raw, value)]
        for i, (item_raw, typed) in enumerate(candidates, 1):
            item_number = i if target == "item" else None
            if isinstance(typed, Unknown):
                yield "unchecked", typed.reason, item_number
                continue
            if dtype == "String" or target == "text":
                actual = Decimal(len(text_value(item_raw).encode("utf-16-le", errors="surrogatepass")) // 2)
            else: actual = typed
            if bound is None: continue
            try:
                failed = actual < bound if "Min" in key else actual > bound
            except TypeError:
                yield "unchecked", f"Cannot compare {key} with cell type", item_number
                continue
            if failed:
                yield "violation", f"{key} is {displayed}; got {text_value(actual)}", item_number
    elif check.kind == "options":
        allowed, parents = check.value
        for parent in parents:
            if not isinstance(allowed, dict):
                yield "unchecked", "Nested allowed-values structure does not match parent keys", None
                return
            parent_value = context.get(parent)
            if isinstance(parent_value, Unknown):
                yield "unchecked", parent_value.reason, None
                return
            if parent_value is None:
                yield "unchecked", f"Nested allowed values require nonempty {parent}", None
                return
            key = next((key for key in allowed if option_match(parent_value, [key], parent in context.case_sensitive)), None)
            if key is None:
                if parent_value is IGNORED:
                    yield "unchecked", f"Nested parent {parent} is [IGNORE]", None
                else:
                    yield "violation", f"No allowed-value branch for {parent}={text_value(parent_value)!r}", None
                return
            allowed = allowed[key]
        if not isinstance(allowed, list) or any(isinstance(v, (dict, list)) for v in allowed):
            yield "unchecked", "Allowed-values leaf is not a scalar list", None
            return
        for i, (item_raw, typed) in enumerate(items, 1):
            if isinstance(typed, Unknown): continue  # Type finding already recorded.
            if not option_match(item_raw, allowed, plan.sensitive):
                shown = ", ".join(repr(v) for v in allowed[:12])
                if len(allowed) > 12: shown += f", … ({len(allowed)} values)"
                yield "violation", f"Value is not in the allowed options: {shown}", i if plan.separator else None


ISSUE_COLUMNS = ["sheet", "excel_row", "row_id", "column", "cell", "code", "name", "value",
                 "outcome", "severity", "check", "rule_index", "item_number", "message", "expression"]
ROW_COLUMNS = ["sheet", "excel_row", "row_id", "status", "fatal", "error", "warning", "suspicious",
               "unchecked", "coverage_gaps", "ignored_cells"]


def write_csv_row(writer, record):
    # Prevent formula interpretation when a report is opened in Excel. JSON
    # summary and source data are not altered by this presentation escaping.
    writer.writerow({key: ("'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")) else value)
                     for key, value in record.items()})


def validate(args):
    source = json.loads(args.rules.read_text(encoding="utf-8"))
    definitions = source.get("fields")
    if not isinstance(definitions, dict) or not definitions:
        raise ValueError("Rules JSON must contain a nonempty 'fields' object")
    overrides = json.loads(args.expression_modes.read_text(encoding="utf-8")) if args.expression_modes else {}
    if not isinstance(overrides, dict) or any(mode not in ("valid_when_true", "invalid_when_true") for mode in overrides.values()):
        raise ValueError("Expression modes must map expression strings to valid_when_true or invalid_when_true")
    as_of = args.as_of or datetime.now(ZoneInfo(args.timezone)).date()
    workbook = load_workbook(args.workbook, read_only=True, data_only=False, keep_links=False)
    args.output.mkdir(parents=True, exist_ok=True)
    plans = {}
    global_gaps, dynamic_gaps, sheets = [], {}, []
    totals, statuses, issue_counts = Counter(), Counter(), Counter()
    issues_path, rows_path = args.output / "issues.csv", args.output / "rows.csv"
    try:
        names = list(dict.fromkeys(args.sheet or workbook.sheetnames))
        if any(name not in workbook.sheetnames for name in names):
            raise ValueError(f"Unknown sheet; available sheets: {workbook.sheetnames}")
        with issues_path.with_suffix(".csv.tmp").open("w", newline="", encoding="utf-8-sig") as issue_file, rows_path.with_suffix(".csv.tmp").open("w", newline="", encoding="utf-8-sig") as row_file:
            issue_writer = csv.DictWriter(issue_file, fieldnames=ISSUE_COLUMNS)
            row_writer = csv.DictWriter(row_file, fieldnames=ROW_COLUMNS)
            issue_writer.writeheader()
            row_writer.writeheader()
            for sheet_name in names:
                sheet = workbook[sheet_name]
                header_cells = next(sheet.iter_rows(min_row=args.header_row, max_row=args.header_row), ())
                codes = [text_value(cell.value).strip() for cell in header_cells]
                if not any(codes): raise ValueError(f"No field codes on {sheet_name} row {args.header_row}")
                duplicate_codes = {code for code, count in Counter(codes).items() if code and count > 1}
                columns = {code: i for i, code in enumerate(codes) if code and code not in duplicate_codes}
                sheet_gaps = []
                for code in duplicate_codes:
                    sheet_gaps.append({"code": code, "kind": "duplicate_header", "reason": "Duplicate column code; all occurrences excluded to avoid ambiguous dependencies"})
                for code in columns:
                    if code not in definitions:
                        sheet_gaps.append({"code": code, "kind": "missing_rules", "reason": "No field definition in supplied rules JSON"})
                    else:
                        if code not in plans: plans[code] = compile_field(code, definitions[code], as_of, overrides, args.skip_relative_dates)
                        sheet_gaps.extend(plans[code].gaps)
                for gap in sheet_gaps: global_gaps.append({"sheet": sheet_name, **gap})
                sensitive = {code for code in columns if code in plans and plans[code].sensitive}
                sheet_summary = {"sheet": sheet_name, "columns": len(columns), "columns_with_rules": sum(code in definitions for code in columns),
                                 "missing_rule_codes": [code for code in columns if code not in definitions],
                                 "duplicate_codes": sorted(duplicate_codes), "rows_checked": 0, "blank_rows_skipped": 0,
                                 "limited": False}
                sheets.append(sheet_summary)
                print(f"{sheet_name}: {len(columns)} columns, {sheet_summary['columns_with_rules']} with rules; validating rows…", flush=True)

                for cells in sheet.iter_rows(min_row=args.data_start_row):
                    if all(empty(cell.value) for cell in cells):
                        sheet_summary["blank_rows_skipped"] += 1
                        continue
                    if args.limit and sheet_summary["rows_checked"] >= args.limit:
                        sheet_summary["limited"] = True
                        break
                    row_number = next(cell.row for cell in cells if cell.value is not None)
                    raw_values, values, prepared = {}, {}, {}
                    for code, position in columns.items():
                        cell = cells[position] if position < len(cells) else None
                        raw = cell.value if cell else None
                        raw_values[code] = raw
                        if cell and cell.data_type == "f": raw = Unknown("Formula cell requires a recalculated value export; formulas are not executed")
                        elif cell and cell.data_type == "e": raw = Unknown(f"Excel error cell: {cell.value}")
                        if code in plans:
                            prepared[code] = prepare_cell(raw, plans[code])
                            values[code] = prepared[code][0]
                        else:
                            if isinstance(raw, Unknown): values[code] = raw
                            elif empty(raw): values[code] = None
                            elif raw == "[IGNORE]": values[code] = IGNORED
                            elif isinstance(raw, (int, float)) and not isinstance(raw, bool): values[code] = Decimal(str(raw))
                            elif isinstance(raw, date) and not isinstance(raw, datetime): values[code] = datetime.combine(raw, time())
                            else: values[code] = raw
                    row_id = text_value(raw_values.get("OFST020000"))
                    counts = Counter()

                    def emit(code, outcome, severity, kind, rule_index, message, item=None, expression=""):
                        position = columns[code]
                        column = get_column_letter(position + 1)
                        plan = plans.get(code)
                        record = dict(sheet=sheet_name, excel_row=row_number, row_id=row_id,
                                      column=column, cell=f"{column}{row_number}", code=code,
                                      name=plan.name if plan else code, value=text_value(raw_values[code]),
                                      outcome=outcome, severity=severity, check=kind, rule_index=rule_index,
                                      item_number=item, message=message, expression=expression)
                        write_csv_row(issue_writer, record)
                        if outcome == "unchecked":
                            counts["unchecked"] += 1
                            key = (sheet_name, code, kind, rule_index, message, expression)
                            if key not in dynamic_gaps:
                                dynamic_gaps[key] = dict(sheet=sheet_name, code=code, check=kind, rule_index=rule_index,
                                                         reason=message, expression=expression, occurrences=0, first_excel_row=row_number)
                            dynamic_gaps[key]["occurrences"] += 1
                        else:
                            counts[severity.lower()] += 1
                            issue_counts[severity] += 1

                    for code, (value, items, errors) in prepared.items():
                        plan, raw = plans[code], raw_values[code]
                        if value is IGNORED:
                            counts["ignored_cells"] += 1
                            continue
                        if isinstance(value, Unknown) and not errors:
                            emit(code, "unchecked", "", "cell_value", 0, value.reason)
                        for item, message in errors: emit(code, "violation", "Fatal", "type_or_format", 0, message, item)
                        context = Context(values, code, sensitive)
                        for check in plan.checks:
                            for outcome, message, item in check_cell(check, raw, value, items, plan, context):
                                emit(code, outcome, check.severity, check.kind, check.rule_index, message, item, check.expression)
                    # A populated column with no header cannot be validated.
                    for i, code in enumerate(codes):
                        if not code and i < len(cells) and not empty(cells[i].value):
                            counts["unchecked"] += 1
                            key = (sheet_name, "", "missing_header", i)
                            if key not in dynamic_gaps:
                                dynamic_gaps[key] = dict(sheet=sheet_name, column=get_column_letter(i+1), reason="Populated column has no field-code header", occurrences=0, first_excel_row=row_number)
                            dynamic_gaps[key]["occurrences"] += 1
                    if counts["fatal"] or counts["error"]: status = "invalid"
                    elif sheet_gaps or counts["unchecked"]: status = "incomplete"
                    elif counts["warning"] or counts["suspicious"]: status = "valid_with_warnings"
                    else: status = "valid"
                    write_csv_row(row_writer, {"sheet": sheet_name, "excel_row": row_number, "row_id": row_id,
                                              "status": status, "coverage_gaps": len(sheet_gaps),
                                              **{key: counts[key] for key in ("fatal", "error", "warning", "suspicious", "unchecked", "ignored_cells")}})
                    totals.update(counts)
                    statuses[status] += 1
                    sheet_summary["rows_checked"] += 1
                    if sheet_summary["rows_checked"] % 100 == 0:
                        print(f"  {sheet_summary['rows_checked']} rows checked", flush=True)
    finally:
        workbook.close()
    issues_path.with_suffix(".csv.tmp").replace(issues_path)
    rows_path.with_suffix(".csv.tmp").replace(rows_path)
    total_rows = sum(s["rows_checked"] for s in sheets)
    coverage_complete = not global_gaps and not dynamic_gaps
    summary = {
        "workbook": str(args.workbook), "rules_file": str(args.rules),
        "rules_sha256": hashlib.sha256(args.rules.read_bytes()).hexdigest(),
        "generated_at": datetime.now(timezone.utc).isoformat(), "as_of": as_of.isoformat(),
        "skip_relative_dates": args.skip_relative_dates,
        "header_row": args.header_row, "data_start_row": args.data_start_row,
        "rows_checked": total_rows, "row_status_counts": dict(statuses), "violation_counts": dict(issue_counts),
        "unchecked_occurrences": totals["unchecked"], "ignored_cells": totals["ignored_cells"],
        "coverage_complete": coverage_complete,
        "scan_complete": not any(s["limited"] for s in sheets),
        "valid": total_rows > 0 and coverage_complete and not any(s["limited"] for s in sheets) and not totals["fatal"] and not totals["error"],
        "sheets": sheets, "coverage_gaps": global_gaps, "unevaluated_checks": list(dynamic_gaps.values()),
        "expression_mode_overrides": overrides,
        "notes": [
            "Local interpretation of exported rules; not certified as equivalent to FE fundinfo's server.",
            "Optional blank values skip scalar constraints. Missing columns are unknown, not empty.",
            "Ambiguous expressions and unsupported formats are reported as coverage gaps.",
            "[N/A] is treated as empty; [IGNORE] skips the target and may prevent dependent checks.",
            "Formula cells are not executed. Export recalculated values to validate their results.",
            "Report CSV text starting with formula-significant characters is prefixed with an apostrophe.",
        ],
    }
    summary_path = args.output / "summary.json"
    temporary = summary_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(summary_path)
    print(f"Checked {total_rows} rows: {dict(statuses)}. Violations: {dict(issue_counts)}.")
    print(f"Reports: {args.output}. Coverage complete: {coverage_complete}.")
    if totals["fatal"] or totals["error"]: return 1
    if not summary["valid"]: return 2
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path, nargs="?", default=Path("original.xlsx"))
    parser.add_argument("--rules", type=Path, default=Path("fundinfo.filtered.json"))
    parser.add_argument("--output", type=Path, default=Path("validation"))
    parser.add_argument("--sheet", action="append", help="Sheet name; repeat to select several (default: all sheets)")
    parser.add_argument("--header-row", type=int, default=1, help="Row containing field codes")
    parser.add_argument("--data-start-row", type=int, default=3, help="First data row; default skips code and name headers")
    parser.add_argument("--as-of", type=date.fromisoformat, help="YYYY-MM-DD used for Today/date bounds")
    parser.add_argument("--timezone", default="Europe/Athens", help="Timezone used for default as-of date")
    parser.add_argument("--skip-relative-dates", action="store_true", help="Skip bounds relative to Today or BeginOfYear")
    parser.add_argument("--limit", type=int, help="Maximum nonempty data rows per sheet")
    parser.add_argument("--expression-modes", type=Path, help="JSON mapping exact expressions to valid_when_true or invalid_when_true")
    args = parser.parse_args()
    if args.header_row < 1 or args.data_start_row <= args.header_row or (args.limit is not None and args.limit < 1):
        parser.error("Use a positive header row/limit and a data-start row after the header")
    try:
        return validate(args)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Cannot validate: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
