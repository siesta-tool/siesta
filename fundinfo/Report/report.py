"""Build the self-contained HTML report.

The page shows the spreadsheet as a grid and marks, per cell:
  - mined coexistence rules the cell breaks, with the LLM verdict
  - fundinfo validation failures, with the LLM's plain-words explanation
  - checks the validator could not run
Columns whose fundinfo rules are missing or only partly usable are marked in the header.
"""
import argparse
import base64
import csv
import json
import re
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("output", help="Report HTML path")
parser.add_argument("--workbook", required=True)
parser.add_argument("--trace", required=True)
parser.add_argument("--violations", required=True)
parser.add_argument("--verdicts", required=True)
parser.add_argument("--validation", required=True, type=Path, help="Folder with issues.csv and summary.json")
parser.add_argument("--fundinfo", required=True)
parser.add_argument("--explanations", required=True)
args = parser.parse_args()

SEPARATOR = "§"
ROW_OFFSET = 3


def clean(value):
    return str(value).replace(",", "/").replace("\n", " ").strip()


def cell_text(value):
    if pd.isna(value):
        return ""
    return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value).strip()


def column_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def interned(table):
    """Return a function mapping a string to its index in `table`, appending new strings."""
    index = {}

    def intern(text):
        if text not in index:
            index[text] = len(table)
            table.append(text)
        return index[text]
    return intern


# ── spreadsheet ──────────────────────────────────────────────────────

# Literal texts such as "N/A" stay visible in the grid.
raw = pd.read_excel(args.workbook, header=None, keep_default_na=False, na_values=[""])
codes = [cell_text(v) for v in raw.iloc[0]]
heads = [cell_text(v) for v in raw.iloc[1]]
texts = raw.iloc[2:].map(cell_text).to_numpy()
n_rows, n_cols = texts.shape

vals = []
val = interned(vals)
grid = np.array([[val(t) + 1 if t else 0 for t in row] for row in texts], dtype="<u2")
assert len(vals) < 65535, "too many distinct cell values for the 16-bit grid"

lengths = np.vectorize(len)(texts)
widths = [int(min(240, max(64, min(123, 3.75 * len(heads[c]) + 18), 7.2 * np.percentile(lengths[:, c], 90) + 14)))
          for c in range(n_cols)]

col_of_code = {code: c for c, code in enumerate(codes)}
col_of_head = {clean(head): c for c, head in enumerate(heads)}

# ── mined rules and their violations ─────────────────────────────────

rows_of = {int(tid): rows for tid, rows in json.loads(Path(args.trace).with_suffix(".rows.json").read_text(encoding="utf-8")).items()}
logged = defaultdict(dict)
with open(args.trace, newline="", encoding="utf-8") as f:
    for event in csv.DictReader(f):
        column, _, value = event["activity"].partition(SEPARATOR)
        for row in rows_of.get(int(event["trace_id"]), []):
            logged[row][column] = value

verdicts = {(c["excel_row"], c["field"]): c for c in json.loads(Path(args.verdicts).read_text(encoding="utf-8"))}

labels, log_vals, ruling_texts = [], [], []
label, log_val, ruling_text = interned(labels), interned(log_vals), interned(ruling_texts)
rules, viol, ruling = [], [], []
for k, rule in enumerate(json.loads(Path(args.violations).read_text(encoding="utf-8"))["rules"]):
    rules.append([0, label(rule["source"]), label(rule["target"]), rule["support"], rule["confidence"], rule["interest"]])
    for side, missing, trace_ids in ((0, rule["target"], rule["missing_target"]), (1, rule["source"], rule["missing_source"])):
        column = missing.split(SEPARATOR, 1)[0].strip()
        for tid in trace_ids:
            actual = logged[tid].get(column)
            viol += [tid, k, side, -1 if actual is None else log_val(actual)]
            verdict = verdicts.get((tid + ROW_OFFSET, column))
            ruling += [2, -1] if verdict is None else [int(verdict["anomaly"]), ruling_text(verdict["reason"])]

label_col = [col_of_head.get(s.split(SEPARATOR, 1)[0].strip(), -1) for s in labels]

# ── fundinfo validation ──────────────────────────────────────────────

explained = {(cell["excel_row"], cell["column"], rule["message"]): rule["explanation"]
             for cell in json.loads(Path(args.explanations).read_text(encoding="utf-8")) for rule in cell["rules"]}

failed, unchecked, unchk_texts = defaultdict(list), defaultdict(list), []
unchk_text = interned(unchk_texts)
sheet_name = pd.ExcelFile(args.workbook).sheet_names[0]
with open(args.validation / "issues.csv", newline="", encoding="utf-8-sig") as f:
    for issue in csv.DictReader(f):
        if issue["sheet"] != sheet_name:
            continue
        key = (int(issue["excel_row"]) - ROW_OFFSET) * n_cols + column_index(issue["column"])
        if issue["outcome"] == "violation":
            explanation = explained.get((int(issue["excel_row"]), issue["column"], issue["message"]), "")
            failed[key].append([issue["message"], explanation])
        elif unchk_text(issue["message"]) not in unchecked[key]:
            unchecked[key].append(unchk_text(issue["message"]))

scrape_failures = json.loads(Path(args.fundinfo).read_text(encoding="utf-8"))["failures"]
col_gaps = defaultdict(list)
for gap in json.loads((args.validation / "summary.json").read_text(encoding="utf-8"))["coverage_gaps"]:
    if gap["code"] not in col_of_code:
        continue
    reason = gap["reason"]
    if gap["kind"] == "missing_rules":
        reason = f"No rules, scrape failed: {scrape_failures[gap['code']]}" if gap["code"] in scrape_failures \
            else "No rules, code is not in the fundinfo catalogue"
    elif gap.get("expression"):
        reason += f" ({gap['expression']})"
    if reason not in col_gaps[col_of_code[gap["code"]]]:
        col_gaps[col_of_code[gap["code"]]].append(reason)

# ── page ─────────────────────────────────────────────────────────────

payload = {
    "row_offset": ROW_OFFSET, "n_rows": n_rows, "n_cols": n_cols,
    "codes": codes, "heads": heads, "widths": widths, "vals": vals,
    "grid": base64.b64encode(grid.tobytes()).decode("ascii"),
    "labels": labels, "log_vals": log_vals, "label_col": label_col,
    "rules": rules, "viol": viol, "ruling": ruling, "ruling_texts": ruling_texts,
    "expr": list(failed.items()), "unchk": list(unchecked.items()), "unchk_texts": unchk_texts,
    "col_gaps": col_gaps,
}
data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
page = (Path(__file__).with_name("template.html").read_text(encoding="utf-8")
        .replace("__SRC__", Path(args.workbook).name)
        .replace("__DATA__", data))
Path(args.output).write_text(page, encoding="utf-8")

cells = {(viol[i], label_col[rules[viol[i + 1]][2 if viol[i + 2] == 0 else 1]]) for i in range(0, len(viol), 4)}
print(f"{len(rules)} rules, {len(viol) // 4} violations in {len(cells)} cells, "
      f"{len(verdicts)} cells with a verdict, {len(failed)} validation cells, "
      f"{len(unchecked)} unchecked cells, {len(col_gaps)} columns with gaps -> {args.output}")
