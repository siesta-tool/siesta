"""Fetch the traces that break each coexistence rule (XOR) from Siesta.

A trace breaks A <-> B when it holds exactly one of the two events.
Rules that link two values of one column, or two different countries, are skipped.
Answers are cached per rule, and the cache resets when the trace log changes.
"""
import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "3_trace"))
from form_analysis import SEPARATOR, _country_matcher

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("rules_csv")
parser.add_argument("trace_csv")
parser.add_argument("output", help="Violations JSON path")
parser.add_argument("--api", required=True)
parser.add_argument("--log-name", required=True)
parser.add_argument("--workers", type=int, default=4)
args = parser.parse_args()

country_rx = _country_matcher()


def column(label):
    return label.split(SEPARATOR, 1)[0].strip()


def country(label):
    match = country_rx.search(label)
    return match.group(0) if match else None


def informative(rule):
    source, target = rule["source"], rule["target"]
    if column(source) == column(target):
        return False
    return country(source) is None or country(target) is None or country(source) == country(target)


def fetch(rule):
    response = requests.post(
        f"{args.api}/mining/violations",
        json={"log_name": args.log_name, "source": rule["source"], "target": rule["target"],
              "template": "coexistence", "mode": "XOR"},
        timeout=600,
    )
    response.raise_for_status()
    reply = response.json()
    if reply.get("code") != 200:
        raise RuntimeError(f"Violations query failed: {reply}")
    return sorted(int(tid) for tid in reply["trace_ids"])


with open(args.rules_csv, newline="", encoding="utf-8") as f:
    rules = [rule for rule in csv.DictReader(f) if informative(rule)]

holders = defaultdict(set)
with open(args.trace_csv, newline="", encoding="utf-8") as f:
    for event in csv.DictReader(f):
        holders[event["activity"]].add(int(event["trace_id"]))

rows_of = {int(tid): rows for tid, rows in json.loads(Path(args.trace_csv).with_suffix(".rows.json").read_text(encoding="utf-8")).items()}
trace_hash = hashlib.sha256(Path(args.trace_csv).read_bytes()).hexdigest()
cache_path = Path(args.output).with_suffix(".cache.json")
cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
if cache.get("trace_hash") != trace_hash:
    cache = {"trace_hash": trace_hash, "pairs": {}}
pairs = cache["pairs"]

todo = [rule for rule in rules if rule["source"] + "\n" + rule["target"] not in pairs]
print(f"{len(rules)} rules, {len(todo)} to fetch", flush=True)
with ThreadPoolExecutor(max_workers=args.workers) as pool:
    for done, (rule, trace_ids) in enumerate(zip(todo, pool.map(fetch, todo)), 1):
        pairs[rule["source"] + "\n" + rule["target"]] = trace_ids
        if done % 25 == 0 or done == len(todo):
            cache_path.write_text(json.dumps(cache))
            print(f"  {done}/{len(todo)}", flush=True)

result = []
for rule in rules:
    trace_ids = pairs[rule["source"] + "\n" + rule["target"]]
    has_source = holders[rule["source"]]
    result.append({
        "source": rule["source"],
        "target": rule["target"],
        "support": float(rule["support"]),
        "confidence": float(rule["confidence"] or 0),
        "interest": float(rule["interest"] or 0),
        "missing_target": [row for tid in trace_ids if tid in has_source for row in rows_of.get(tid, [])],
        "missing_source": [row for tid in trace_ids if tid not in has_source for row in rows_of.get(tid, [])],
    })

Path(args.output).write_text(json.dumps({"rules": result}), encoding="utf-8")
print(f"{sum(len(r['missing_target']) + len(r['missing_source']) for r in result)} violations "
      f"across {len(result)} rules -> {args.output}")
