"""Ask an LLM to put each fundinfo validation failure into plain words.

Each failed rule gets its own prompt, with the cell's value
and this row's values of the fields the rule refers to.
Answers are cached by prompt text, in the same cache as evaluate.py.
"""
import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path

import pandas as pd
import requests

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("issues_csv")
parser.add_argument("workbook")
parser.add_argument("fundinfo_json")
parser.add_argument("output", help="Explanations JSON path")
parser.add_argument("--cache", required=True, type=Path)
parser.add_argument("--llm-url", default="http://localhost:11434/v1")
parser.add_argument("--model", default="gemma4:e4b")
parser.add_argument("--limit", type=int, help="Maximum new LLM calls; rules left unanswered get no explanation")
args = parser.parse_args()

FIELDS = json.loads(Path(args.fundinfo_json).read_text(encoding="utf-8"))["fields"]
SHEET = pd.read_excel(args.workbook, header=None)
COLUMN_OF = {str(code).strip(): c for c, code in enumerate(SHEET.iloc[0])}
CODE_RE = re.compile(r"\[(OF\w+(?:\([^)]*\))?)\]")

PROMPT = """You explain validation errors to the person who filled in a fund registration form.
The cell below broke an official rule of the form.

Cell under review:
  {name} = {value}

Rule it broke:
  {rule}

Other fields this rule refers to, with this row's values:
{context}

Say in one plain sentence what is wrong with the cell and why.
Do not tell the person what to do to fix it.
Use the full field names exactly as written above, never field codes or formulas."""


def ask(prompt):
    response = requests.post(f"{args.llm_url}/chat/completions", timeout=600, json={
        "model": args.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
    })
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def cell_text(value):
    if pd.isna(value):
        return "(empty)"
    return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)


def field_name(code):
    return FIELDS[code]["metadata"]["Name"] if code in FIELDS else code


def referenced(excel_row, issue):
    """This row's values of the other fields named in the failed expression."""
    codes = sorted(set(CODE_RE.findall(issue["expression"])) - {issue["code"]})
    return [f"  {field_name(c)} = {cell_text(SHEET.iat[excel_row - 1, COLUMN_OF[c]])}" for c in codes if c in COLUMN_OF]


cells = defaultdict(list)
with open(args.issues_csv, newline="", encoding="utf-8-sig") as f:
    for issue in csv.DictReader(f):
        if issue["outcome"] == "violation":
            cells[int(issue["excel_row"]), issue["column"]].append(issue)

cache = json.loads(args.cache.read_text()) if args.cache.exists() else {}
calls = skipped = 0
results = []
for (excel_row, column), issues in cells.items():
    first = issues[0]
    explained = []
    for issue in issues:
        prompt = PROMPT.format(
            name=issue["name"], value=issue["value"] or "(empty)", rule=issue["message"],
            context="\n".join(referenced(excel_row, issue)) or "  (none)",
        )
        if prompt not in cache and args.limit is not None and calls >= args.limit:
            skipped += 1
            explained.append({"message": issue["message"], "explanation": ""})
            continue
        if prompt not in cache:
            cache[prompt] = ask(prompt).strip()
            args.cache.write_text(json.dumps(cache))
            calls += 1
        explained.append({"message": issue["message"], "explanation": cache[prompt]})
    results.append({
        "excel_row": excel_row, "column": column, "cell": first["cell"], "code": first["code"],
        "name": first["name"], "value": first["value"], "rules": explained,
    })
    print(f"{first['cell']} | {first['name']}", flush=True)
    for rule in explained:
        print(f"  rule: {rule['message']}\n  says: {rule['explanation']}", flush=True)
    print(flush=True)

Path(args.output).write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
print(f"{len(results)} cells, {sum(len(r['rules']) for r in results)} failed rules explained, "
      f"{calls} new calls, {skipped} rules left without an explanation -> {args.output}")
