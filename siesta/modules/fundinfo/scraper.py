"""Download the public FE fundinfo identifier catalogue and validation rules.

Detail pages come from an undocumented HTML endpoint of validate.fundinfo.com,
so the parsers fail loudly when the page structure changes.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
from pathlib import Path
import threading
import time
from typing import Any, Dict
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

INDEX_URL = "https://validate.fundinfo.com/Data/Identifiers"
DETAIL_URL = "https://validate.fundinfo.com/Data/Identifier/Detail/"
SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_index(html: str) -> list[dict[str, str]]:
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


def labelled_value(strong) -> str:
    """Return the text beside a label, keeping interior whitespace of regexes and expressions."""
    cell = BeautifulSoup(str(strong.parent), "html.parser")
    for tag in cell.select("strong, i, script, style"):
        tag.decompose()
    for br in cell.find_all("br"):
        br.replace_with("\n")
    return cell.get_text().strip()


def parse_options(raw: str) -> list | None:
    """Parse comma-separated JSON values; None when the format is unfamiliar."""
    if not raw.strip():
        return []
    try:
        return json.loads("[" + raw + "]")
    except ValueError:
        return None


def parse_detail(html: str, expected_code: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    metadata = {}
    rules = []
    references = []
    warnings = []
    current = None
    section = None
    openfunds_url = None

    for row in soup.select("div.row"):

        # Skip wrapper rows so nested rows are processed once.
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
            else:
                warnings.append(f"Unrecognised section: {title}")
                section = "unknown"

        if section == "references":
            references.extend(a.get_text(strip=True) for a in row.find_all("a"))
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
        "parse_warnings": warnings,
    }


class Downloader:
    """Rate-limited GET shared across worker threads, with retries on 429/5xx."""

    def __init__(self, delay: float = 0.25, timeout: float = 30, retries: int = 4):
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.local = threading.local()

    def get(self, url: str) -> str:
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
            self.local.session.headers["User-Agent"] = "fundinfo-rule-export/1.0 (public identifier catalogue)"
        for attempt in range(self.retries + 1):
            with self.lock:
                time.sleep(max(0, self.next_request - time.monotonic()))
                self.next_request = time.monotonic() + self.delay
            try:
                response = self.local.session.get(url, timeout=self.timeout)
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

                # Server backoff applies to every worker.
                with self.lock:
                    self.next_request = max(self.next_request, time.monotonic() + pause)
        raise RuntimeError("unreachable")


def scrape(
    *,
    codes: list[str] | None = None,
    limit: int | None = None,
    workers: int = 4,
    delay: float = 0.25,
    timeout: float = 30,
    retries: int = 4,
    refresh: bool = False,
    cache_dir: str | Path = "output/fundinfo",
) -> Dict[str, Any]:
    """Collect identifier rules and return the export document.

    Detail pages are cached as HTML under `cache_dir/raw`, so a failed run resumes
    from the pages it already has. `refresh` redownloads cached pages.
    """
    raw_dir = Path(cache_dir) / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    downloader = Downloader(delay, timeout, retries)

    catalogue = parse_index(downloader.get(INDEX_URL))
    selected = catalogue
    if codes:
        requested = set(codes)
        unknown = requested - {entry["code"] for entry in catalogue}
        if unknown:
            raise ValueError(f"Codes not in the dropdown: {', '.join(sorted(unknown))}")
        selected = [entry for entry in catalogue if entry["code"] in requested]
    if limit:
        selected = selected[:limit]
    logger.info(f"Discovered {len(catalogue)} identifiers; collecting {len(selected)}.")

    def collect(entry: dict[str, str]) -> Dict[str, Any]:
        code = entry["code"]

        # The URL-encoded code is also a safe filename.
        raw_path = raw_dir / (quote(code, safe="") + ".html")
        if raw_path.exists() and not refresh:
            try:
                record = parse_detail(raw_path.read_text(encoding="utf-8"), code)
                record["fetched_at"] = datetime.fromtimestamp(raw_path.stat().st_mtime, timezone.utc).isoformat()
                return record
            except ValueError:
                pass
        html = downloader.get(DETAIL_URL + quote(code, safe=""))
        record = parse_detail(html, code)
        temporary = raw_path.with_suffix(".html.tmp")
        temporary.write_text(html, encoding="utf-8")
        temporary.replace(raw_path)
        record["fetched_at"] = utc_now()
        return record

    records: Dict[str, Any] = {}
    failures: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        entries = iter(selected)
        jobs = {}

        def submit_next():
            entry = next(entries, None)
            if entry is not None:
                jobs[executor.submit(collect, entry)] = entry["code"]

        for _ in range(workers):
            submit_next()
        processed = 0
        while jobs:
            completed, _ = wait(jobs, return_when=FIRST_COMPLETED)
            for future in completed:
                code = jobs.pop(future)
                try:
                    records[code] = future.result()
                except Exception as exc:
                    failures[code] = str(exc)
                    logger.warning(f"fundinfo: failed {code}: {exc}")
                processed += 1
                if processed % 100 == 0 or processed == len(selected):
                    logger.info(f"fundinfo: processed {processed}/{len(selected)}; {len(failures)} failed.")
                submit_next()

    ordered = {entry["code"]: records[entry["code"]] for entry in selected if entry["code"] in records}
    missing = [entry["code"] for entry in selected if entry["code"] not in records]
    return {
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
