"""Ask an LLM whether each cell that breaks mined rules is a likely soft violation.

Violations are grouped per cell, an Excel row plus a column.
Each cell gets one prompt with the rules it breaks and the fundinfo links to other fields in its row.
Answers are cached by prompt text, so a rerun only asks about new prompts.
"""
import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

import pandas as pd
import requests

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("violations_json")
parser.add_argument("workbook")
parser.add_argument("fundinfo_json")
parser.add_argument("output", help="Verdicts JSON path")
parser.add_argument("--cache", required=True, type=Path)
parser.add_argument("--llm-url", default="http://localhost:11434/v1")
parser.add_argument("--model", default="gemma4:e4b")
parser.add_argument("--limit", type=int, help="Maximum new LLM calls; cells left unanswered get no verdict")
parser.add_argument("--timeout", type=float, default=600, help="Seconds per LLM call; a cell whose call fails gets no verdict")
args = parser.parse_args()

ALL = json.loads(Path(args.fundinfo_json).read_text(encoding="utf-8"))["fields"]
FIELDS = {f["metadata"]["Name"].lower(): f for f in ALL.values()}
REFERENCED_BY = defaultdict(set)
for f in ALL.values():
    for code in f["field_references_in"]:
        REFERENCED_BY[code].add(f["code"])

SHEET = pd.read_excel(args.workbook, header=1)
COLUMNS = {str(c).lower(): c for c in SHEET.columns}
LOG_COLUMNS = {str(c).replace(",", "/").replace("\n", " ").strip(): c for c in SHEET.columns}
ROW_OFFSET = 3

PROMPT = """You are reviewing one cell of a fund registration form.
Rules mined from the dataset expect this cell to hold a different value, given other answers in the same row.

Every value in the form already passed validation against the field definitions below.
So do not look for typos or invalid formats. Judge only whether the cell's value fits with the row's other answers.
Official links come from the form definition and are real dependencies. Mined rules are only correlations.
A rare but valid value is not an anomaly by itself.
Call it an ANOMALY only when the fields are genuinely tied and the value, or its absence, contradicts the row.

Field definitions:
{docs}

Official links to this cell, with this row's values:
{links}

Rules this cell breaks:
{rules}

Cell under review:
  {field} = {value}

Answer with one sentence of reasoning, then the word ANOMALY or OK as the verdict."""


def ask(prompt):
    response = requests.post(f"{args.llm_url}/chat/completions", timeout=args.timeout, json={
        "model": args.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
    })
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def is_date(code):
    return code in ALL and ALL[code]["metadata"]["Type"] == "DateTime"


def expressions(f):
    return [e for r in f["rules"] for e in r["expressions"]
            if not any(is_date(c) for c in re.findall(r"\[(\w+)\]", e["expression"]))]


def cell_text(value):
    if pd.isna(value):
        return "(empty)"
    return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)


def field_doc(name):
    f = FIELDS.get(name.lower())
    if f is None:
        return None
    m = f["metadata"]
    return "; ".join([f"  {m['Name']}: {m['Level']}-level {m['Type']}"] + [e["error_message"] for e in expressions(f)][:3])


def links(row, field):
    f = FIELDS.get(field.lower())
    if f is None:
        return []
    lines = []
    for code in sorted(set(f["field_references_in"]) | REFERENCED_BY[f["code"]]):
        other = ALL.get(code)
        col = other and COLUMNS.get(other["metadata"]["Name"].lower())
        if col is None or is_date(code):
            continue
        why = [e["error_message"] for e in expressions(f) if f"[{code}]" in e["expression"]] + \
              [e["error_message"] for e in expressions(other) if f"[{f['code']}]" in e["expression"]]
        if why:
            lines.append(f"  {other['metadata']['Name']} = {cell_text(SHEET.at[row - ROW_OFFSET, col])}: {'; '.join(why)}")
    return lines


def build_prompt(row, field, value, rules):
    names = [field] + sorted({holds.partition(" = ")[0] for holds, _ in rules} - {field})
    docs = [d for d in map(field_doc, names) if d]
    return PROMPT.format(docs="\n".join(docs) or "  (none available)",
                         links="\n".join(links(row, field)) or "  (none)",
                         rules="\n".join(f"  Row has {h}, so the rule expects {e}" for h, e in rules),
                         field=field, value=value)


def load_cells():
    """Group the violations by the cell that lacks the expected event."""
    cells = defaultdict(set)
    for rule in json.loads(Path(args.violations_json).read_text(encoding="utf-8"))["rules"]:
        source, target = rule["source"].replace("§", " = "), rule["target"].replace("§", " = ")
        for held, expected, trace_ids in ((source, target, rule["missing_target"]), (target, source, rule["missing_source"])):
            for tid in trace_ids:
                cells[tid + ROW_OFFSET, expected.partition(" = ")[0]].add((held, expected))
    for (row, field), rules in sorted(cells.items()):
        column = LOG_COLUMNS.get(field)

        # A rule about a column this workbook lacks says nothing about its rows.
        if column is None:
            continue
        yield row, field, cell_text(SHEET.at[row - ROW_OFFSET, column]), sorted(rules)


cache = json.loads(args.cache.read_text()) if args.cache.exists() else {}
calls = skipped = failed = 0
results = []
for row, field, value, rules in load_cells():
    prompt = build_prompt(row, field, value, rules)
    if prompt not in cache:
        if args.limit is not None and calls >= args.limit:
            skipped += 1
            continue
        try:
            cache[prompt] = ask(prompt).strip()
        except requests.RequestException as exc:
            failed += 1
            print(f"row {row} | {field} = {value} | {len(rules)} rules | LLM call failed: {exc}", flush=True)
            continue
        args.cache.write_text(json.dumps(cache))
        calls += 1
        print(f"row {row} | {field} = {value} | {len(rules)} rules", flush=True)
    answer = cache[prompt]
    results.append({
        "excel_row": row, "field": field, "value": value, "rules": rules,
        "anomaly": answer.strip(".*! \n").upper().endswith("ANOMALY"),
        "reason": re.sub(r"[\s.*!]*(ANOMALY|OK)[\s.*!]*$", "", answer, flags=re.I),
    })

Path(args.output).write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
print(f"{len(results)} cells judged, {sum(c['anomaly'] for c in results)} anomalies, "
      f"{calls} new calls, {failed} failed calls, {skipped + failed} cells left without a verdict -> {args.output}")
