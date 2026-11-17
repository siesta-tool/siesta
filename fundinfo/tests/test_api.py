"""The pipeline API, with a stub in place of run_pipeline.py."""

import sys
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

import conftest  # noqa: F401  (puts the repo on sys.path)
sys.path.insert(0, str(conftest.ROOT))
from api import app as api  # noqa: E402

# The stub prints step markers, writes a report into the output folder and
# fails or hangs when the workbook name asks it to.

STUB = r"""
import os, sys, time
from pathlib import Path
args = sys.argv[1:]
add = "--add" in args
workbook = Path(args[args.index("--add") + 1] if add else args[0])
out = Path(os.environ["STUB_OUT"]) / ("added/" + workbook.stem if add else "")
out.mkdir(parents=True, exist_ok=True)
print("args", " ".join(args))
print("\n== Step 2: Validate against fundinfo")
if "slow" in workbook.name:
    time.sleep(30)
if "broken" in workbook.name:
    sys.exit("Step 2 failed with exit code 3")
print("\n== Step 9: Produce the report")
(out / "report.html").write_text("<html>report</html>")
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    stub = tmp_path / "stub.py"
    stub.write_text(STUB)
    out = tmp_path / "output"
    out.mkdir()
    (out / "validation").mkdir()
    (out / "validation" / "issues.csv").write_text("old")
    monkeypatch.setenv("STUB_OUT", str(out))
    monkeypatch.setattr(api, "OUT", out)
    monkeypatch.setattr(api, "RUNS", out / "runs")
    monkeypatch.setattr(api, "PIPELINE", [sys.executable, str(stub)])
    monkeypatch.setattr(api, "SETTINGS", {"--llm-model": "test-model"})
    monkeypatch.setattr(api, "runner", api.Runner())
    with TestClient(api.app) as test_client:
        yield test_client


def submit(client, name="book.xlsx", **form):
    return client.post("/runs", files={"workbook": (name, b"xlsx bytes")}, data={k: str(v) for k, v in form.items()})


def wait(client, run_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/runs/{run_id}").json()
        if run["status"] in api.FINISHED:
            return run
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish")


def test_successful_run(client):
    response = submit(client, "My Book.xlsx", first=2, llm_limit=0)
    assert response.status_code == 202
    run = wait(client, response.json()["id"])
    assert (run["status"], run["exit_code"], run["step"], run["step_title"]) == ("succeeded", 0, 9, "Produce the report")
    assert run["workbook"] == "My_Book.xlsx"
    assert run["artifacts"] == ["report.html"]
    assert client.get(f"/runs/{run['id']}/report").text == "<html>report</html>"


def test_command_line_carries_the_options(client):
    run = wait(client, submit(client, first=7, last=9, llm_limit=0, rescrape="true").json()["id"])
    first_line = client.get(f"/runs/{run['id']}/log").text.splitlines()[0]
    assert first_line.endswith("book.xlsx --from 7 --to 9 --llm-limit 0 --rescrape --llm-model test-model")


def test_stale_results_are_not_copied(client):
    run = wait(client, submit(client).json()["id"])
    assert "validation/issues.csv" not in run["artifacts"]
    assert client.get(f"/runs/{run['id']}/files/validation/issues.csv").status_code == 404


def test_failed_run_reports_the_last_line(client):
    run = wait(client, submit(client, "broken.xlsx").json()["id"])
    assert (run["status"], run["exit_code"]) == ("failed", 1)
    assert run["error"] == "Step 2 failed with exit code 3"
    assert run["artifacts"] == []


def test_log_tail(client):
    run = wait(client, submit(client).json()["id"])
    assert client.get(f"/runs/{run['id']}/log", params={"tail": 1}).text == "== Step 9: Produce the report\n"


def test_add_mode_collects_results_from_the_added_folder(client):
    (api.OUT / "trace.summary.json").write_text("{}")
    run = wait(client, submit(client, "new rows.xlsx", mode="add").json()["id"])
    assert run["artifacts"] == ["report.html"]
    assert (api.OUT / "added" / "new_rows" / "report.html").exists()


def test_add_mode_needs_a_base_run(client):
    assert submit(client, mode="add").status_code == 409


@pytest.mark.parametrize("name, form", [
    ("book.csv", {}),
    ("book.xlsx", {"first": 0}),
    ("book.xlsx", {"last": 10}),
    ("book.xlsx", {"first": 5, "last": 4}),
    ("book.xlsx", {"mode": "replace"}),
    ("book.xlsx", {"llm_limit": -1}),
])
def test_rejects_bad_requests(client, name, form):
    assert submit(client, name, **form).status_code == 422


def test_runs_execute_one_at_a_time_and_cancel(client):
    slow = submit(client, "slow.xlsx").json()["id"]
    queued = submit(client).json()["id"]
    deadline = time.time() + 5
    while client.get(f"/runs/{slow}").json()["status"] != "running" and time.time() < deadline:
        time.sleep(0.05)
    assert client.get(f"/runs/{queued}").json()["status"] == "queued"
    assert client.get("/health").json() == {"status": "ok", "running": slow, "queued": 1}

    assert client.delete(f"/runs/{queued}").json()["status"] == "cancelled"
    assert client.delete(f"/runs/{slow}").status_code == 200
    assert wait(client, slow)["status"] == "cancelled"
    assert client.get(f"/runs/{queued}").json()["started_at"] is None
    assert client.delete(f"/runs/{slow}").status_code == 409


def test_list_is_newest_first(client):
    ids = [wait(client, submit(client).json()["id"])["id"] for _ in range(2)]
    assert [run["id"] for run in client.get("/runs").json()] == sorted(ids, reverse=True)


def test_unknown_runs(client):
    assert client.get("/runs/20260101-000000-abcdef").status_code == 404
    assert client.get("/runs/../../etc").status_code == 404
    run = wait(client, submit(client).json()["id"])
    assert client.get(f"/runs/{run['id']}/files/../run.json").status_code == 404


def test_interrupted_runs_fail_on_restart(client):
    run = submit(client, "slow.xlsx").json()
    deadline = time.time() + 5
    while client.get(f"/runs/{run['id']}").json()["status"] != "running" and time.time() < deadline:
        time.sleep(0.05)
    client.delete(f"/runs/{run['id']}")
    wait(client, run["id"])
    api.update(run["id"], status="running")
    api.Runner().start()
    restarted = client.get(f"/runs/{run['id']}").json()
    assert (restarted["status"], restarted["error"]) == ("failed", "The server stopped during the run")
