#!/usr/bin/env python3
"""Download the public FE fundinfo identifier catalogue and validation rules."""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re
from pathlib import Path
import sys
import threading
import time
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

INDEX_URL = "https://validate.fundinfo.com/Data/Identifiers"
DETAIL_URL = "https://validate.fundinfo.com/Data/Identifier/Detail/"
SCHEMA_VERSION = 1


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    """Replace output atomically so an interrupted run cannot truncate it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def parse_index(html):
    soup = BeautifulSoup(html, "html.parser")
    identifiers = {}
    for option in soup.select("select#identifiers option[value]"):
        code = option["value"].strip()
        if not code:
            continue
        label = option.get_text(" ", strip=True)
        name = label.removeprefix(code + " - ")
        if code in identifiers and identifiers[code]["name"] != name:
            raise ValueError(f"Conflicting dropdown entries for {code}")
        identifiers[code] = {"code": code, "name": name}
    if not identifiers:
        raise ValueError("No identifiers found; the index HTML may have changed.")
    return list(identifiers.values())


def labelled_value(strong):
    """Keep interior whitespace intact (especially in regexes/expressions)."""
    cell = BeautifulSoup(str(strong.parent), "html.parser")
    for tag in cell.select("strong, i, script, style"):
        tag.decompose()
    for br in cell.find_all("br"):
        br.replace_with("\n")
    return cell.get_text().strip()


def parse_options(raw):
    if not raw.strip():
        return []
    # The site renders comma-separated JSON strings, without enclosing brackets.
    # Some fields can contain nested structures; preserve their shape verbatim.
    try:
        result = json.loads("[" + raw + "]")
        return result
    except ValueError:
        return None  # Never invent enum values if the source format is unfamiliar.


def parse_detail(html, expected_code):
    soup = BeautifulSoup(html, "html.parser")
    metadata = {}
    rules = []
    references = []
    required = []
    warnings = []
    current = None
    section = None
    openfunds_url = None

    for row in soup.select("div.row"):
        # The endpoint currently returns sibling rows. Avoid processing nested
        # rows twice if the server adds an outer wrapper.
        if row.select_one("div.row"):
            continue
        heading = row.find("h4")
        if heading:
            title = heading.get_text(" ", strip=True)
            if title == "Meta Information":
                section = "metadata"
            elif title.endswith(" Rule"):
                section = "rule"
                current = {
                    "severity": title.removesuffix(" Rule"),
                    "constraints": {},
                    "data_options": [],
                    "data_options_raw": "",
                    "nested_data_option_keys": None,
                    "expressions": [],
                }
                rules.append(current)
            elif title == "DataOptions":
                if current is None:
                    raise ValueError("DataOptions appeared before a rule")
                section = "rule"
            elif title == "Field References in":
                section = "references"
            elif title == "Required Fields":
                section = "required"
            else:
                warnings.append(f"Unrecognised section: {title}")
                section = "unknown"

        if section == "references":
            references.extend(a.get_text(strip=True) for a in row.find_all("a"))
            continue

        # Fields that must be filled whenever this field is filled. Pages separate them by commas or newlines.
        if section == "required":
            if not heading:
                for code in re.findall(r"OF[A-Za-z0-9]+(?:\([^)]*\))?", row.get_text()):
                    if code not in required:
                        required.append(code)
            continue

        for strong in row.find_all("strong"):
            label = strong.get_text(" ", strip=True).rstrip(":").strip()
            value = labelled_value(strong)
            if section == "metadata":
                metadata[label] = {"True": True, "False": False}.get(value, value)
                if label == "Code":
                    link = strong.parent.find("a", href=True)
                    if link:
                        openfunds_url = urljoin(INDEX_URL, link["href"])
            elif section == "rule" and current is not None:
                if label == "NestedDataOptionKeys":
                    current["nested_data_option_keys"] = value
                elif label == "Expression":
                    current["expressions"].append({"expression": value, "error_message": None})
                elif label == "Error Message":
                    if not current["expressions"] or current["expressions"][-1]["error_message"] is not None:
                        raise ValueError("Error message without a matching expression")
                    current["expressions"][-1]["error_message"] = value
                else:
                    if label in current["constraints"]:
                        raise ValueError(f"Duplicate constraint {label!r}")
                    current["constraints"][label] = value
            else:
                warnings.append(f"Unrecognised field: {label}")

        for textarea in row.find_all("textarea"):
            if current is None or section != "rule":
                raise ValueError("Options outside a rule")
            raw = textarea.get_text().strip()
            current["data_options_raw"] = raw
            current["data_options"] = parse_options(raw)
            if current["data_options"] is None:
                warnings.append("Could not parse DataOptions; see data_options_raw")

    if metadata.get("Code") != expected_code:
        raise ValueError(f"Expected {expected_code!r}, received {metadata.get('Code')!r}")
    if not all(key in metadata for key in ("Name", "Level", "Type")) or not rules:
        raise ValueError(f"Missing metadata or validation rules for {expected_code}")
    return {
        "code": expected_code,
        "source_url": DETAIL_URL + quote(expected_code, safe=""),
        "openfunds_url": openfunds_url,
        "metadata": metadata,
        "rules": rules,
        "field_references_in": references,
        "required_fields": required,
        "parse_warnings": warnings,
    }


class Downloader:
    def __init__(self, delay=0.25, timeout=30, retries=4):
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.local = threading.local()

    def get(self, url):
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
            self.local.session.headers["User-Agent"] = "fundinfo-rule-export/1.0 (public identifier catalogue)"
        for attempt in range(self.retries + 1):
            with self.lock:
                time.sleep(max(0, self.next_request - time.monotonic()))
                self.next_request = time.monotonic() + self.delay
            try:
                response = self.local.session.get(url, timeout=self.timeout)
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                response.encoding = "utf-8"
                return response.text
            except requests.RequestException as exc:
                response = getattr(exc, "response", None)
                if response is not None and response.status_code != 429 and response.status_code < 500:
                    raise
                if attempt == self.retries:
                    raise
                pause = min(60, 2 ** attempt)
                retry_after = response.headers.get("Retry-After") if response is not None else None
                if retry_after:
                    try:
                        pause = max(pause, float(retry_after))
                    except ValueError:
                        try:
                            pause = max(pause, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                        except (ValueError, TypeError):
                            pass
                # Apply server backoff globally, including other workers.
                with self.lock:
                    self.next_request = max(self.next_request, time.monotonic() + pause)


def cached_record(raw_path, code):
    record = parse_detail(raw_path.read_text(encoding="utf-8"), code)
    record["fetched_at"] = datetime.fromtimestamp(raw_path.stat().st_mtime, timezone.utc).isoformat()
    return record


def run(args):
    output = args.output
    raw_dir = output / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    downloader = Downloader(args.delay, args.timeout, args.retries)
    index_path = output / "identifiers.html"
    if args.offline:
        index_html = index_path.read_text(encoding="utf-8")
    else:
        index_html = downloader.get(INDEX_URL)
        parse_index(index_html)  # Do not replace a valid cache with an error page.
        index_path.write_text(index_html, encoding="utf-8")
    catalogue = parse_index(index_html)
    write_json(output / "identifiers.json", {"source_url": INDEX_URL, "identifiers": catalogue})
    selected = catalogue
    if args.code:
        requested = set(args.code)
        unknown = requested - {entry["code"] for entry in catalogue}
        if unknown:
            raise ValueError(f"Codes not in the dropdown: {', '.join(sorted(unknown))}")
        selected = [entry for entry in catalogue if entry["code"] in requested]
    if args.limit:
        selected = selected[:args.limit]
    print(f"Discovered {len(catalogue)} identifiers; collecting {len(selected)}.", flush=True)
    records = {}
    failures = {}

    def collect(entry):
        code = entry["code"]
        # Encoding the complete code also makes it safe as a filename.
        raw_path = raw_dir / (quote(code, safe="") + ".html")
        if raw_path.exists() and (args.offline or not args.refresh):
            try:
                return cached_record(raw_path, code)
            except ValueError:
                if args.offline:
                    raise
        if args.offline:
            raise ValueError(f"No cached detail for {code}")
        html = downloader.get(DETAIL_URL + quote(code, safe=""))
        record = parse_detail(html, code)
        temporary = raw_path.with_suffix(".html.tmp")
        temporary.write_text(html, encoding="utf-8")
        temporary.replace(raw_path)
        record["fetched_at"] = utc_now()
        return record

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            entries = iter(selected)
            jobs = {}

            def submit_next():
                entry = next(entries, None)
                if entry is not None:
                    jobs[executor.submit(collect, entry)] = entry["code"]

            for _ in range(args.workers):
                submit_next()
            index = 0
            while jobs:
                completed, _ = wait(jobs, return_when=FIRST_COMPLETED)
                for future in completed:
                    code = jobs.pop(future)
                    try:
                        records[code] = future.result()
                    except Exception as exc:
                        failures[code] = str(exc)
                        print(f"FAILED {code}: {exc}", file=sys.stderr, flush=True)
                    index += 1
                    if index % 100 == 0 or index == len(selected):
                        print(f"Processed {index}/{len(selected)}; {len(failures)} failed.", flush=True)
                    submit_next()
    finally:
        ordered = {entry["code"]: records[entry["code"]] for entry in selected if entry["code"] in records}
        missing = [entry["code"] for entry in selected if entry["code"] not in records]
        document = {
            "schema_version": SCHEMA_VERSION,
            "source_url": INDEX_URL,
            "exported_at": utc_now(),
            "discovered_count": len(catalogue),
            "selected_count": len(selected),
            "downloaded_count": len(ordered),
            "complete": len(ordered) == len(catalogue),
            "selection_complete": not missing,
            "missing_codes": missing,
            "failures": failures,
            "fields": ordered,
        }
        write_json(output / "fundinfo.json", document)
        write_json(output / "failures.json", failures)
    warning_count = sum(bool(record["parse_warnings"]) for record in records.values())
    print(f"Saved {len(records)} fields to {output / 'fundinfo.json'}; {warning_count} fields have parse warnings.")
    return 1 if failures else (2 if warning_count else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data"))
    parser.add_argument("--workers", type=int, default=4, help="Concurrent downloads (1–8; default: 4)")
    parser.add_argument("--delay", type=float, default=0.25, help="Global minimum seconds between request starts")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--retries", type=int, default=4, help="Retries for connection errors, HTTP 429 and 5xx")
    parser.add_argument("--limit", type=int, help="Only collect the first N identifiers")
    parser.add_argument("--code", action="append", help="Collect an exact dropdown code; repeat to select several")
    parser.add_argument("--refresh", action="store_true", help="Redownload cached detail pages")
    parser.add_argument("--offline", action="store_true", help="Reparse cached HTML without any network requests")
    args = parser.parse_args()
    if not 1 <= args.workers <= 8 or args.delay < 0 or args.timeout <= 0 or args.retries < 0:
        parser.error("Use workers 1–8, delay >= 0, timeout > 0, retries >= 0")
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if args.offline and args.refresh:
        parser.error("--offline and --refresh cannot be combined")
    try:
        return run(args)
    except (OSError, ValueError, requests.RequestException) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; cached details are saved. Rerun to resume.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
