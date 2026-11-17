"""Run the fundinfo pipeline end to end.

    .venv/bin/python run_pipeline.py [WORKBOOK] [--from N] [--to N]
    .venv/bin/python run_pipeline.py --add NEW_WORKBOOK [--from N] [--to N]

Steps:
  1  scrape the fundinfo rule catalogue
  2  validate the workbook against it
  3  convert the workbook to an event trace
  4  index the trace in Siesta
  5  mine coexistence rules
  6  fetch the violations of those rules
  7  judge each violating cell with an LLM
  8  explain each fundinfo validation failure in plain words with an LLM
  9  build the HTML report

The base run writes to output/.
--add needs a finished base run. It encodes the new workbook the way the base run did,
rebuilds the base log in Siesta, adds the new rows to it incrementally and mines again.
Steps 6 to 9 then cover only the new rows. Results land in output/added/<workbook name>/.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "output"

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("workbook", nargs="?", default=str(HERE / "original.xlsx"))
parser.add_argument("--add", metavar="NEW_WORKBOOK", help="Add this workbook's rows to the base log incrementally")
parser.add_argument("--from", dest="first", type=int, default=1, help="First step to run")
parser.add_argument("--to", dest="last", type=int, default=9, help="Last step to run")
parser.add_argument("--siesta-api", default="http://anaconda.csd.auth.gr:8080")
parser.add_argument("--log-name", default="fundinfo_pipeline", help="Siesta log name; step 4 overwrites it")
parser.add_argument("--llm-url", default="http://localhost:11434/v1", help="OpenAI-compatible endpoint")
parser.add_argument("--llm-model", default="gemma4:e4b")
parser.add_argument("--llm-limit", type=int, help="Maximum new LLM calls in each of steps 7 and 8")
parser.add_argument("--rescrape", action="store_true", help="Run step 1 even when the catalogue exists")
args = parser.parse_args()

fundinfo = OUT / "fundinfo" / "fundinfo.json"
llm = ["--cache", OUT / "llm_cache.json", "--llm-url", args.llm_url, "--model", args.llm_model]
siesta = ["--api", args.siesta_api, "--log-name", args.log_name]
limit = ["--limit", args.llm_limit] if args.llm_limit is not None else []

base_trace = OUT / "trace.csv"
workbook = args.add or args.workbook
out = OUT / "added" / re.sub(r"[^A-Za-z0-9._-]+", "_", Path(args.add).stem) if args.add else OUT
validation = out / "validation"
trace = out / "trace.csv"
rules = out / "rules.csv"
violations = out / "violations.json"
verdicts = out / "verdicts.json"
explanations = out / "explanations.json"
report = out / "report.html"

if args.add:
    # The base log is rebuilt and mined first, so the incremental run starts from a known state.
    encode = ["Siesta/3_trace/form_analysis.py", workbook, trace,
              "--like", base_trace.with_suffix(".summary.json")]
    index = [["Siesta/4_index/index.py", base_trace, *siesta],
             ["Siesta/5_mine/mine.py", out / "rules_base.csv", *siesta],
             ["Siesta/4_index/index.py", trace, *siesta, "--append"]]
else:
    encode = ["Siesta/3_trace/form_analysis.py", workbook, trace]
    index = [["Siesta/4_index/index.py", trace, *siesta]]

# The scraper and the validator use exit codes 1 and 2 for findings, which are normal here.
STEPS = {
    1: ("Scrape fundinfo", (0, 1, 2),
        [["Fundinfo/1_scrape/scrape_fundinfo.py", "--output", fundinfo.parent]]),
    2: ("Validate against fundinfo", (0, 1, 2),
        [["Fundinfo/2_validate/validate_fundinfo.py", workbook, "--rules", fundinfo, "--output", validation,
          "--skip-relative-dates", "--expression-modes", "Fundinfo/2_validate/expression_modes.json"]]),
    3: ("Convert spreadsheet to event trace", (0,), [encode]),
    4: ("Index with Siesta", (0,), index),
    5: ("Mine with Siesta", (0,),
        [["Siesta/5_mine/mine.py", rules, *siesta]]),
    6: ("Get violations from Siesta", (0,),
        [["Siesta/6_violations/violations.py", rules, trace, violations, *siesta]]),
    7: ("Evaluate violation cells with the LLM", (0,),
        [["LLM/evaluate.py", violations, workbook, fundinfo, verdicts, *llm,
          *limit]]),
    8: ("Explain validation failures with the LLM", (0,),
        [["LLM/explain.py", validation / "issues.csv", workbook, fundinfo, explanations, *llm, *limit]]),
    9: ("Produce the report", (0,),
        [["Report/report.py", report, "--workbook", workbook, "--trace", trace, "--violations", violations,
          "--verdicts", verdicts, "--validation", validation, "--fundinfo", fundinfo,
          "--explanations", explanations]]),
}

if args.add and not base_trace.with_suffix(".summary.json").exists():
    sys.exit("--add needs a finished base run; run the pipeline on the base workbook first")

out.mkdir(parents=True, exist_ok=True)
for number in range(args.first, args.last + 1):
    title, accepted, commands = STEPS[number]
    print(f"\n== Step {number}: {title}", flush=True)
    if number == 1 and fundinfo.exists() and not args.rescrape:
        print(f"Using the catalogue at {fundinfo}. Pass --rescrape to refresh it.")
        continue
    for command in commands:
        code = subprocess.run([sys.executable, *map(str, command)], cwd=HERE).returncode
        if code not in accepted:
            sys.exit(f"Step {number} failed with exit code {code}")
