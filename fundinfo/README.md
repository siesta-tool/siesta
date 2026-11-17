# Fundinfo pipeline

Checks a fund spreadsheet in two ways and shows both results in one HTML report.

- Hard rules: the published FE fundinfo validation rules.
- Soft rules: coexistence rules mined from the spreadsheet itself by Siesta. An LLM judges each cell that breaks them.

## Run

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python run_pipeline.py                  # all steps
.venv/bin/python run_pipeline.py other.xlsx       # another workbook
.venv/bin/python run_pipeline.py --from 7         # only the LLM steps and the report
.venv/bin/python run_pipeline.py --from 7 --llm-limit 20      # at most 20 new LLM calls in each of steps 7 and 8
```

Requirements:

- The Siesta API is reachable (`--siesta-api`, default `http://anaconda.csd.auth.gr:8080`).
- An OpenAI-compatible LLM endpoint is reachable (`--llm-url`, default local Ollama, model `gemma4:e4b`).

The report is written to `output/report.html`.

## API

```sh
docker compose up -d --build                        # http://localhost:8000/docs
.venv/bin/pip install -r api/requirements.txt       # or run it without Docker:
.venv/bin/uvicorn api.app:app --host 0.0.0.0 --port 8000
```

| Method | Path | Does |
|---|---|---|
| POST | `/runs` | Queues a run. Form fields: `workbook` (file), `mode` (`base` or `add`), `first`, `last`, `llm_limit`, `rescrape` |
| GET | `/runs` | Lists runs, newest first |
| GET | `/runs/{id}` | Status, current step, error and result names |
| DELETE | `/runs/{id}` | Cancels a queued or running run |
| GET | `/runs/{id}/log` | Pipeline output; `?tail=N` for the last lines |
| GET | `/runs/{id}/report` | The HTML report |
| GET | `/runs/{id}/files/{name}` | One result, such as `validation/issues.csv` |
| GET | `/health` | Running run and queue length |

```sh
curl -F workbook=@original.xlsx -F first=7 -F llm_limit=0 localhost:8000/runs
```

- **One at a time:** runs share `output/`, the LLM cache and the Siesta log, so they queue.
- **Run folder:** `output/runs/<id>/` holds the upload, `log.txt`, `run.json` and `results/`. A result is copied only when the run wrote it, so a `first=7` run has no `validation/` results.
- **Restarts:** queued runs resume. A run that was running is marked failed.
- **Settings:** `SIESTA_API`, `SIESTA_LOG_NAME`, `LLM_URL` and `LLM_MODEL` apply to every run. Compose reads them from the shell or `.env`.
- **Networking:** Compose uses host networking, so the container reaches an Ollama bound to `127.0.0.1` and listens on `API_PORT` (default 8000) of the host.
- **Volume:** `./output` is mounted, so the API and command-line runs share caches and the base run.

## Tests

```sh
.venv/bin/python -m pytest tests
```

The tests cover step 2. `tests/test_catalogue.py` checks the scraped catalogue and is skipped until step 1 has run.

## Adding a workbook incrementally

```sh
.venv/bin/python run_pipeline.py --add new.xlsx                  # all steps for the new rows
.venv/bin/python run_pipeline.py --add new.xlsx --llm-limit 0    # report without new LLM calls
```

`--add` needs a finished base run. It writes to `output/added/<workbook name>/`.

| Step | What `--add` does |
|---|---|
| 2 | Validates the new workbook. |
| 3 | Encodes the new workbook with the base run's column decisions from `output/trace.summary.json`. Timestamps start after the base trace's last one. |
| 4 | Rebuilds the base log, mines it into `rules_base.csv`, then adds the new trace with `clear_existing: false`. |
| 5 | Mines again. Siesta mines only the new traces and keeps the base traces' constraints. Thresholds apply to all traces. |
| 6–9 | Cover only the new rows, renumbered from 0 to match the workbook. |

- **Encoding:** columns match the base by cleaned header name. Numbers fall into the base bins, and a number outside them gets an open-ended bin such as `(1000.0/ inf)`.
- **New columns:** columns the base workbook lacks are dropped, so they take no part in mining.
- **Missing columns:** a rule about a column the new workbook lacks is ignored for its rows.
- **Repeated share classes:** a row whose ISIN is already in the base becomes a second, separate trace.
- **Rerunning:** step 4 rebuilds the base log every time, so a rerun never adds the new rows twice.

## Steps

| # | Folder | Script | Reads | Writes (in `output/`) |
|---|---|---|---|---|
| 1 | `Fundinfo/1_scrape` | `scrape_fundinfo.py` | validate.fundinfo.com | `fundinfo/fundinfo.json`, `fundinfo/failures.json`, `fundinfo/raw/` |
| 2 | `Fundinfo/2_validate` | `validate_fundinfo.py` | workbook, `fundinfo.json` | `validation/issues.csv`, `rows.csv`, `summary.json` |
| 3 | `Siesta/3_trace` | `form_analysis.py` | workbook | `trace.csv`, `trace.summary.json` |
| 4 | `Siesta/4_index` | `index.py` | `trace.csv` | log in Siesta (`POST /indexing/run`) |
| 5 | `Siesta/5_mine` | `mine.py` | Siesta (`POST /mining/run`) | `rules.csv` |
| 6 | `Siesta/6_violations` | `violations.py` | `rules.csv`, `trace.csv`, Siesta (`POST /mining/violations`) | `violations.json` |
| 7 | `LLM` | `evaluate.py` | `violations.json`, workbook, `fundinfo.json` | `verdicts.json` |
| 8 | `LLM` | `explain.py` | `validation/issues.csv`, workbook, `fundinfo.json` | `explanations.json` |
| 9 | `Report` | `report.py`, `template.html` | steps 2, 3, 6, 7, 8 | `report.html` |

Every step is a standalone script with its own `--help`. `run_pipeline.py` only wires the paths together.

## Memoization

| Step | Cache | Behaviour |
|---|---|---|
| 1 | `output/fundinfo/raw/*.html` | A scrape reuses saved detail pages. |
| 1 | `output/fundinfo/fundinfo.json` | The runner skips the step when the catalogue exists. `--rescrape` forces it. |
| 6 | `output/violations.cache.json` | One answer per rule. The cache resets when `trace.csv` changes. |
| 7, 8 | `output/llm_cache.json` | One answer per prompt text. An interrupted run resumes. |

## Where the code comes from

| Step | Origin | Changes |
|---|---|---|
| 1 | `~/projects/scrapefundinfo/scrape_fundinfo.py` | parses the "Required Fields" section into `required_fields` |
| 2 | `~/projects/scrapefundinfo/validate_fundinfo.py`, `fundinfo_expressions.py` | `--skip-relative-dates` added, `required_fields` checked, `expression_modes.json` added |
| 3 | `siesta-framework/siesta/modules/analyse/form_analysis.py`, `countries.txt` | command line added, pandas deprecation fixed, encoding saved and reusable (`--like`) |
| 4, 5 | new | thin API calls with the settings `form_analysis` used internally; `--append` for incremental indexing |
| 6 | rule filters from `build_violations_html.py` | violations come from the API |
| 7 | `llmtest/cell_review.py` | prompt and parsing unchanged, HTML writers removed |
| 8 | new | one prompt per failing cell |
| 9 | grid page of `form_log_nodates_violations_2026-11-02_cells_grid.html` | data is built from scratch, three additions below |

## Behaviour worth knowing

- **Relative dates:** step 2 skips bounds relative to `Today` or `BeginOfYear` (for example `MinDate = Today - 15 Months`), so results do not depend on the run date.
- **Required fields:** when a field is filled, every field in its "Required Fields" list must be filled too. A miss is reported as Fatal.
- **Expression modes:** [expression_modes.json](Fundinfo/2_validate/expression_modes.json) marks five expressions as `invalid_when_true`, each read from its error message. Four are lifecycle and dormancy-date conflicts on OFST020545, and one is SICAR with a SICAV/SICAF legal form on OFST160150.
- **Trace conversion:** each row is a trace and each kept cell an event `column§value`. Constant, unique and date columns are dropped. Numbers go into 5 buckets. Columns with more than 5% distinct values collapse to `filled`. A `no` in a country column is left out.
- **Mining:** Siesta defaults, category `unordered`. The API mines the whole category, so `mine.py` keeps only the `coexistence` rows.
- **Rule filter:** step 6 skips rules that link two values of one column or two different countries.
- **Violations:** XOR mode. A trace breaks `A <-> B` when it holds exactly one of the two events. The trace log tells which side is missing.
- **Cells:** step 7 groups violations by Excel row and the column of the missing event. One prompt per cell.
- **Verdict parsing:** an answer that does not end in `ANOMALY` counts as OK.

## Report

- Yellow cell: breaks mined rules and the LLM said ANOMALY, or the LLM has not reviewed it yet.
- Green hatched cell: breaks mined rules and the LLM said OK.
- Red cell: fails fundinfo validation. Deeper red when it also breaks mined rules as an anomaly. The tooltip lists each failed rule with its plain-words explanation.
- Grey hatched cell: the validator could not run a check on it.
- Amber underlined field code in the header: the column has no fundinfo rules, or some of its rules are unusable. The hover text gives the reason, including scrape failures.

Additions over the earlier grid page:

1. All validation failures from `issues.csv` are shown, not only the hand-picked expression failures.
2. Unchecked cells and column gaps are shown.
3. Cells without a verdict (`--llm-limit`) show as "not reviewed".
