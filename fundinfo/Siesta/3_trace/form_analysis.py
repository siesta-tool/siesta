import csv
import hashlib
import json
import re
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import IO, Any, Dict

import pandas as pd

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?$")
DIGIT_RE = re.compile(r"\d")
DATE_GROUPINGS = ("dow", "month", "quarter", "year", "hour", "date", "none")
SEPARATOR = "§"
BASE_TIME = datetime(2025, 1, 2, 9, 0, 0)
COUNTRY_LIST = Path(__file__).with_name("countries.txt")

"""
RULES:
100% - skip
>5% distinct - filled/unfilled
dates - dropped (or bucketed via date_grouping)
numeric - Bucket + unfilled
<5% distinct - keep
"""


def _looks_like_dates(series: pd.Series) -> bool:
    values = [value for value in series.unique() if pd.notna(value)]
    if not values:
        return False
    return all(isinstance(value, str) and DATE_RE.fullmatch(value) for value in values)


def _bucket_dates(series: pd.Series, mode: str) -> pd.Series:
    match mode:
        case "dow":
            bucketed = series.dt.day_name()
        case "month":
            bucketed = series.dt.month_name()
        case "quarter":
            bucketed = "Q" + series.dt.quarter.astype("Int64").astype(str)
        case "year":
            bucketed = series.dt.year.astype("Int64").astype(str)
        case "hour":
            bucketed = series.dt.hour.astype("Int64").astype(str) + ":00"
        case "date":
            bucketed = series.dt.strftime("%Y-%m-%d")
        case _:
            raise ValueError(f"Unknown date_grouping: '{mode}'")
    return bucketed.where(series.notna())


def _clean(value: Any) -> str:
    return str(value).replace(",", "/").replace("\n", " ").strip()


@lru_cache(maxsize=1)
def _country_matcher() -> re.Pattern:
    """Regex finding a country name anywhere in a label.

    Names are matched longest-first so the most specific wins ("Guinea-Bissau"
    over "Guinea", "South Sudan" over "Sudan"), and only on letter boundaries so
    a name embedded in a longer word is ignored ("Oman" inside "Romania",
    "India" inside "British Indian Ocean Territory").
    """
    names = {line.strip() for line in COUNTRY_LIST.read_text(encoding="utf-8").splitlines() if line.strip()}
    letter = r"[^\W\d_]"
    alt = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    return re.compile(r"(?<!%s)(?:%s)(?!%s)" % (letter, alt, letter))


def _is_country_noise(value: Any) -> bool:
    text = _clean(value)
    # return text.lower() != "yes" and not DIGIT_RE.search(text)
    return _clean(value).strip().lower() == "no"


def run_form_analysis(
    source: str | Path | IO[bytes],
    output_path: str,
    *,
    header_row: int = 1,
    sheet_name: str | int = 0,
    date_grouping: str = "none",
    numeric_bins: int = 5,
    retain_threshold: float = 0.05,
    drop_constant: bool = True,
    drop_unique: bool = True,
) -> Dict[str, Any]:
    """Turn a form-style spreadsheet into a trace CSV the indexer can consume.

    Each row becomes a trace, each filled cell an event named `column§value`.
    Cells of country-scoped columns are only kept when they carry a `yes` or a digit,
    so the bulk of "not registered here" answers stays out of the traces.
    Returns a summary containing the output path, the trace / event counts and the
    per-column decisions taken while preprocessing.
    """
    if date_grouping not in DATE_GROUPINGS:
        raise ValueError(f"Unknown date_grouping '{date_grouping}'. Expected one of {list(DATE_GROUPINGS)}.")

    df = pd.read_excel(source, header=header_row, sheet_name=sheet_name)
    if df.empty:
        raise ValueError("The uploaded spreadsheet contains no data rows.")
    trace_ids = _trace_ids(df)

    row_count = len(df)
    columns: list[Dict[str, Any]] = []
    country_rx = _country_matcher()
    country_columns = {col for col in df.columns if country_rx.search(str(col))}

    for col in df.columns:
        if _looks_like_dates(df[col]):
            df[col] = pd.to_datetime(df[col], format="ISO8601")

        distinct = df[col].nunique(dropna=False)

        if drop_constant and distinct == 1:
            df = df.drop(columns=col)
            columns.append({"column": str(col), "action": "dropped_constant"})
            continue

        if drop_unique and distinct == row_count and pd.api.types.is_string_dtype(df[col]):
            df = df.drop(columns=col)
            columns.append({"column": str(col), "action": "dropped_unique"})
            continue

        if pd.api.types.is_datetime64_any_dtype(df[col]):
            if date_grouping == "none":
                df = df.drop(columns=col)
                columns.append({"column": str(col), "action": "dropped_date"})
                continue
            df[col] = _bucket_dates(df[col], date_grouping)
            columns.append({"column": str(col), "action": "date_bucket", "detail": date_grouping})
            continue

        if pd.api.types.is_numeric_dtype(df[col]):
            df[col], edges = pd.cut(df[col], bins=numeric_bins, retbins=True)
            columns.append({"column": str(col), "action": "numeric_bucket", "detail": f"{numeric_bins} bins",
                            "edges": edges.tolist(), "labels": [str(c) for c in df[col].cat.categories]})
            continue

        if distinct / row_count > retain_threshold:
            df[col] = df[col].apply(lambda x: "filled" if pd.notna(x) else x)
            columns.append({"column": str(col), "action": "filled_flag", "detail": f"{distinct} distinct values"})
            continue

        columns.append({"column": str(col), "action": "kept", "detail": f"{distinct} distinct values"})

    country_names = {str(col) for col in country_columns}
    for entry in columns:
        if entry["column"] in country_names:
            entry["country"] = True

    return {"output_path": output_path, **_write_traces(df, trace_ids, country_columns, output_path), "columns": columns}


def apply_encoding(
    source: str | Path,
    output_path: str,
    base_summary: Dict[str, Any],
    *,
    header_row: int = 1,
    sheet_name: str | int = 0,
) -> Dict[str, Any]:
    """Encode another spreadsheet with the column decisions of an earlier run, for incremental indexing.

    Columns match the earlier run by cleaned name, and columns it never saw are dropped.
    Numbers fall into the earlier bins. A number outside them gets an open-ended bin.
    Timestamps start after the earlier run's last one, so Siesta adds every row as a new trace
    that evolved after the last mining run.
    """
    df = pd.read_excel(source, header=header_row, sheet_name=sheet_name)
    trace_ids = _trace_ids(df)
    plan = {_clean(entry["column"]): entry for entry in base_summary["columns"]}
    unseen = []
    for col in list(df.columns):
        entry = plan.get(_clean(col))
        action = entry["action"] if entry else None
        if action in (None, "dropped_constant", "dropped_unique", "dropped_date"):
            if entry is None:
                unseen.append(str(col))
            df = df.drop(columns=col)
        elif action == "date_bucket":
            df[col] = _bucket_dates(pd.to_datetime(df[col], format="ISO8601", errors="coerce"), entry["detail"])
        elif action == "numeric_bucket":
            df[col] = df[col].map(lambda value: _bin(value, entry["edges"], entry["labels"]))
        elif action == "filled_flag":
            df[col] = df[col].apply(lambda x: "filled" if pd.notna(x) else x)

    present = {_clean(col) for col in df.columns}
    kept = [name for name, entry in plan.items() if not entry["action"].startswith("dropped")]
    country_columns = {col for col in df.columns if _country_matcher().search(str(col))}
    start = datetime.fromisoformat(base_summary["last_timestamp"]) + timedelta(minutes=1)
    return {
        "output_path": output_path,
        **_write_traces(df, trace_ids, country_columns, output_path, start),
        "unseen_columns": unseen,
        "missing_columns": [name for name in kept if name not in present],
    }


def _bin(value: Any, edges: list[float], labels: list[str]) -> Any:
    """Return the earlier run's bin label for a number. Non-numeric text passes through.

    The exact edges place the value, and the labels keep pandas' rounded text.
    """
    if pd.isna(value):
        return value
    try:
        x = float(value)
    except (TypeError, ValueError):
        return value
    if x < edges[0]:
        return f"(-inf, {labels[0][1:-1].split(', ')[0]}]"
    if x > edges[-1]:
        return f"({labels[-1][1:-1].split(', ')[1]}, inf)"
    for i, right in enumerate(edges[1:]):
        if x <= right:
            return labels[i]


def _trace_ids(df: pd.DataFrame) -> Dict[Any, int]:
    """Hash each raw row's cells and row number into a positive 63-bit trace id."""
    ids = {}
    for row_number, row in df.iterrows():
        cells = [f"{_clean(column)}{SEPARATOR}{'' if pd.isna(value) else _clean(value)}" for column, value in row.items()]
        digest = hashlib.sha256("\n".join([str(row_number), *cells]).encode("utf-8")).digest()
        ids[row_number] = int.from_bytes(digest[:8], "big") >> 1
    return ids


def _write_traces(
    df: pd.DataFrame, trace_ids: Dict[Any, int], country_columns: set, output_path: str, start: datetime = BASE_TIME
) -> Dict[str, Any]:
    """Write one trace per row and one event per kept cell.

    `trace_ids` holds each row's id, hashed from its raw cells and row number.
    `<output>.rows.json` maps each trace id to the workbook row it came from.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    trace_count = 0
    event_count = 0
    country_events_dropped = 0
    last = start
    rows_of: Dict[int, list[int]] = {}
    with open(output_path, "w", newline="", encoding="utf-8") as trace_file:
        writer = csv.writer(trace_file)
        writer.writerow(["trace_id", "position", "activity", "timestamp"])
        for row_number, row in df.iterrows():
            events = []
            for column, value in row.items():
                if pd.isna(value) or str(value) == "nan":
                    continue
                if column in country_columns and _is_country_noise(value):
                    country_events_dropped += 1
                    continue
                events.append(f"{_clean(column)}{SEPARATOR}{_clean(value)}")
            if not events:
                continue
            trace_id = trace_ids[row_number]
            rows_of[trace_id] = [int(row_number)]
            for counter, activity in enumerate(events):
                timestamp = start + timedelta(minutes=counter)
                writer.writerow([trace_id, counter, activity, timestamp.isoformat()])
                last = max(last, timestamp)
            trace_count += 1
            event_count += len(events)

    Path(output_path).with_suffix(".rows.json").write_text(json.dumps(rows_of), encoding="utf-8")

    return {
        "trace_count": trace_count,
        "event_count": event_count,
        "country_events_dropped": country_events_dropped,
        "last_timestamp": last.isoformat(),
    }


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Convert a form spreadsheet into a trace CSV.")
    parser.add_argument("workbook")
    parser.add_argument("output", help="Trace CSV path")
    parser.add_argument("--like", help="Summary JSON of an earlier run whose encoding to reuse")
    args = parser.parse_args()

    if args.like:
        base = json.loads(Path(args.like).read_text(encoding="utf-8"))
        summary = apply_encoding(args.workbook, args.output, base)
    else:
        summary = run_form_analysis(args.workbook, args.output)
    Path(args.output).with_suffix(".summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(f"{summary['trace_count']} traces, {summary['event_count']} events -> {args.output}")
    if args.like:
        print(f"{len(summary['unseen_columns'])} columns are new and were dropped, "
              f"{len(summary['missing_columns'])} encoded columns are absent")
