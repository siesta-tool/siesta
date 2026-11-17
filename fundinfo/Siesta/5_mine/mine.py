"""Mine coexistence rules from an indexed log with Siesta's default thresholds."""
import argparse
import csv

import requests

COLUMNS = ["source", "target", "support", "confidence", "interest"]

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("output", help="Rules CSV path")
parser.add_argument("--api", required=True)
parser.add_argument("--log-name", required=True)
args = parser.parse_args()

# Coexistence belongs to the 'unordered' category, which Siesta mines as a whole.
response = requests.post(
    f"{args.api}/mining/run",
    json={"log_name": args.log_name, "categories": ["unordered"]},
    timeout=3600,
)
response.raise_for_status()
reply = response.json()
if reply.get("code") != 200:
    raise SystemExit(f"Mining failed: {reply}")

rules = [row for row in reply["mined"] if row["template"] == "coexistence"]
with open(args.output, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rules)
print(f"{len(rules)} coexistence rules of {len(reply['mined'])} mined -> {args.output}")
