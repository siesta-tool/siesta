import argparse
import json
import logging
from pathlib import Path
from typing import Annotated, Any, Dict

from fastapi import Body, Form, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from siesta.core.config import get_config_value, get_system_config
from siesta.core.interfaces import SiestaModule
from siesta.core.storageFactory import get_storage_manager
from siesta.modules.analyse.main import Analysing
from siesta.modules.fundinfo.scraper import scrape

logger = logging.getLogger(__name__)

FULL_RULES_KEY = "fundinfo/rules.json"
PARTIAL_RULES_KEY = "fundinfo/rules.partial.json"


class FundInfoRulesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    storage_namespace: str = Field(default_factory=lambda: get_config_value("storage_namespace_default", "siesta"), description="Storage namespace holding the rules")
    rules_key: str | None = Field(None, description=f"Object key for the rules; null = '{FULL_RULES_KEY}' for a complete export, '{PARTIAL_RULES_KEY}' otherwise")
    from_storage: bool = Field(False, description="Return the stored rules without contacting the website")
    codes: list[str] | None = Field(None, description="Exact identifier codes to collect; null = every code in the dropdown")
    limit: int | None = Field(None, gt=0, description="Only collect the first N selected identifiers")
    workers: int = Field(4, ge=1, le=8, description="Concurrent detail downloads")
    delay: float = Field(0.25, ge=0, description="Global minimum seconds between request starts")
    timeout: float = Field(30, gt=0, description="Per-request timeout in seconds")
    retries: int = Field(4, ge=0, description="Retries for connection errors, HTTP 429 and 5xx")
    refresh: bool = Field(False, description="Redownload detail pages already in the local HTML cache")
    cache_dir: str = Field("output/fundinfo", description="Local directory for cached detail-page HTML")


class FundInfo(SiestaModule):

    name = "fundinfo"
    version = "0.1.0"

    def startup(self):
        logger.info("FundInfo startup complete.")

    def register_routes(self) -> SiestaModule.ApiRoutes | None:
        return {
            "rules":        ("POST", self.api_rules),
            "process_form": ("POST", self.api_process_form),
        }

    # ------------------------------------------------------------------
    # API entry points
    # ------------------------------------------------------------------

    def api_rules(self, rules_config: Annotated[FundInfoRulesConfig, Body(openapi_examples={
        "sample": {
            "summary": "Collect two identifiers",
            "value": {"codes": ["OFST452000", "OFPM000060(el)"]},
        },
        "full": {
            "summary": "Collect the full catalogue (5,000+ identifiers, 20+ minutes)",
            "value": {},
        },
        "stored": {
            "summary": "Return the stored catalogue",
            "value": {"from_storage": True},
        },
    })]) -> Any:
        """Fetch FE fundinfo validation rules, store them, and return them as JSON.

        Scrapes https://validate.fundinfo.com/Data/Identifiers for every identifier code,
        then each code's detail page for its metadata, allowed values (`data_options`),
        constraints (`Min`, `Max`, `Regex`, ...) and cross-field expressions, grouped by
        rule severity. The export is written to the storage namespace and returned.

        Detail pages are cached locally under `cache_dir`, so a rerun after a failure
        resumes instead of starting over. A full scrape takes 20+ minutes.

        **Config fields:**
        - `storage_namespace` *(str, default: system config `storage_namespace_default`)* - namespace holding the rules.
        - `rules_key` *(str | null, default: `null`)* - object key. `null` stores a complete export at
            `fundinfo/rules.json` and a partial one at `fundinfo/rules.partial.json`.
        - `from_storage` *(bool, default: `false`)* - return the stored rules at the key without scraping.
            With `rules_key` null, this reads `fundinfo/rules.json`.
        - `codes` *(list[str] | null, default: `null`)* - exact identifier codes to collect. `null` = all.
        - `limit` *(int | null, default: `null`)* - only collect the first N selected identifiers.
        - `workers` *(int 1-8, default: `4`)* - concurrent detail downloads.
        - `delay` *(float, default: `0.25`)* - global minimum seconds between request starts.
        - `timeout` *(float, default: `30`)* - per-request timeout in seconds.
        - `retries` *(int, default: `4`)* - retries for connection errors, HTTP 429 and 5xx.
        - `refresh` *(bool, default: `false`)* - redownload detail pages that are already cached.
        - `cache_dir` *(str, default: `"output/fundinfo"`)* - local directory for cached detail HTML.
        """
        logger.info(f"{self.name} running rules via API.")
        try:
            return {"code": 200, **self._fetch_rules(rules_config)}
        except LookupError as e:
            return {"code": 404, "message": str(e)}
        except Exception as e:
            logger.exception(f"Error fetching fundinfo rules: {e}")
            return {"code": 400, "message": f"Fetching fundinfo rules failed: {e}"}

    def api_process_form(
        self,
        form_file: UploadFile,
        form_config: Annotated[str, Form(
            description="JSON configuration object - same fields and defaults as analyser/form_analysis.",
            openapi_examples={
                "defaults": {"summary": "Preprocess only", "value": "{}"},
                "mine": {
                    "summary": "Preprocess, index and mine",
                    "value": '{"log_name": "fund_form", "mine": true, "clear_existing": true}',
                },
            },
        )] = "{}",
    ) -> Any:
        """Upload a fund form and run the form-analysis pipeline on it.

        Uses the same preprocessing, indexing and mining as `analyser/form_analysis`,
        with the same config fields and defaults. The response is a placeholder
        summary; no CSV or HTML report is produced.

        **Form fields:**
        - `form_file` *(file)* - the `.xlsx` fund form.
        - `form_config` *(JSON string, default: `{}`)* - see `analyser/form_analysis` for the fields.
            `return_csv`, `mined_format` and `api_base_url` are ignored.
        """
        logger.info(f"{self.name} running process_form via API.")
        try:
            config = json.loads(form_config)
        except Exception as e:
            return {"code": 400, "message": f"Invalid config: {e}"}

        try:
            return {"code": 200, **self._process_form(config, form_file.file, caller="api")}
        except Exception as e:
            logger.exception(f"Error processing fund form: {e}")
            return {"code": 400, "message": f"Processing fund form failed: {e}"}

    # ------------------------------------------------------------------
    # CLI entry point
    # ------------------------------------------------------------------

    def cli_run(self, args: Any, **kwargs: Any) -> Any:
        """Run `method` "rules" or "process_form" from a JSON config file.

        "process_form" reads the form from `input_path`.
        """
        logger.info(f"{self.name} is running with args: {args}")
        parser = argparse.ArgumentParser(description="Siesta FundInfo module")
        parser.add_argument("--fundinfo_config", type=str, required=True, help="Path to fundinfo configuration JSON file")
        parsed_args, _ = parser.parse_known_args(args)

        config_path = Path(parsed_args.fundinfo_config)
        if not config_path.exists():
            raise FileNotFoundError(f"Config file {config_path} not found.")
        config = json.loads(config_path.read_text())
        method = config.pop("method", "rules")

        match method:
            case "rules":
                result = self._fetch_rules(FundInfoRulesConfig(**config))
                return {key: value for key, value in result.items() if key != "fields"}
            case "process_form":
                input_path = config.get("input_path")
                if not input_path or not Path(input_path).exists():
                    raise FileNotFoundError(f"Input file {input_path!r} not found. Set 'input_path' in the config.")
                return self._process_form(config, input_path, caller="cli")
            case _:
                raise ValueError(f"Unknown fundinfo method: '{method}'")

    # ------------------------------------------------------------------
    # Method implementations
    # ------------------------------------------------------------------

    def _fetch_rules(self, config: FundInfoRulesConfig) -> Dict[str, Any]:
        """Scrape (or read back) the rules export and return it with its storage location."""
        storage = get_storage_manager()
        namespace = config.storage_namespace

        if config.from_storage:
            key = config.rules_key or FULL_RULES_KEY
            stored = storage.read_object(namespace, key)
            if stored is None:
                raise LookupError(f"No fundinfo rules stored at '{key}' in namespace '{namespace}'.")
            return {"stored_at": f"s3a://{namespace}/{key}", **json.loads(stored)}

        document = scrape(
            codes=config.codes,
            limit=config.limit,
            workers=config.workers,
            delay=config.delay,
            timeout=config.timeout,
            retries=config.retries,
            refresh=config.refresh,
            cache_dir=config.cache_dir,
        )
        key = config.rules_key or (FULL_RULES_KEY if document["complete"] else PARTIAL_RULES_KEY)
        payload = json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8")
        uri = storage.write_object(namespace, key, payload, content_type="application/json")
        logger.info(f"fundinfo: stored {document['downloaded_count']} identifier(s) at {uri}.")
        return {"stored_at": uri, **document}

    def _process_form(self, config: Dict[str, Any], source: Any, caller: str) -> Dict[str, Any]:
        """Preprocess a fund form and optionally index and mine it.

        Returns a placeholder summary that the fundinfo validation result will replace.
        """
        analyser = Analysing()
        analyser.siesta_config = get_system_config()
        analyser.storage = get_storage_manager()
        analyser._load_form_analysis_config(config)

        summary = analyser._preprocess_form(source)

        mined_count = None
        if analyser.analyser_config.get("mine", False):
            _, _, mined_rows = analyser._index_and_mine_form_log(summary["output_path"], caller=caller)
            mined_count = len(mined_rows)

        return {
            "status": "stub",
            "log_name": analyser.analyser_config.get("log_name"),
            "trace_count": summary["trace_count"],
            "event_count": summary["event_count"],
            "trace_log_path": summary["output_path"],
            "mined_constraints": mined_count,
        }
