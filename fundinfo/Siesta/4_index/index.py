"""Index a trace CSV in Siesta, from scratch or added to an existing log."""
import argparse
import json
from pathlib import Path

import requests

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("trace_csv")
parser.add_argument("--api", required=True)
parser.add_argument("--log-name", required=True)
parser.add_argument("--append", action="store_true", help="Add the traces to the existing log instead of replacing it")
args = parser.parse_args()

with open(args.trace_csv, "rb") as f:
    response = requests.post(
        f"{args.api}/indexing/run",
        data={"index_config": json.dumps({"log_name": args.log_name, "clear_existing": not args.append})},

        # Siesta stores each upload by file name, so every trace file needs a distinct one.
        files={"log_file": (f"{Path(args.trace_csv).parent.name}_{Path(args.trace_csv).name}", f, "text/csv")},
        timeout=3600,
    )
response.raise_for_status()
reply = response.json()

# Siesta reports failures in the body with HTTP 200.
if reply.get("code") != 200:
    raise SystemExit(f"Indexing failed: {reply}")
print(f"{'Added to' if args.append else 'Indexed'} '{args.log_name}' in {float(reply['time']):.0f}s")
