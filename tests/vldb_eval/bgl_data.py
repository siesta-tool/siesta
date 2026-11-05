"""
tests/vldb_eval/bgl_data.py — the BGL log (LogHub 2.0) as an event log.

Inputs (downloaded from https://zenodo.org/records/8275861, BGL.zip):
  datasets/loghub2_bgl/BGL/BGL_full.log                 raw lines
  datasets/loghub2_bgl/BGL/BGL_full.log_structured.csv  LineId, EventId (Drain templates)

One event per log line: case = Node, activity = EventId, attributes
Component, Level and Type (from the raw line: ``Label Timestamp Date Node
Time NodeRepeat Type Component Level Content``).  Events of a node are
ordered by the microsecond ``Time`` field, then by line number.

Writes
  results/data/bgl/canonical.parquet   trace_id, position, activity, ts, Component, Level, Type
  results/data/bgl/full.csv            trace_id, activity, timestamp (ISO, microseconds), attributes

Usage:
    python -m tests.vldb_eval.bgl_data
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from tests.vldb_eval import pattern_common as pc

SRC = Path(__file__).resolve().parents[2] / "datasets" / "loghub2_bgl" / "BGL"
DS = "bgl"
ATTRS = ["Component", "Level", "Type"]


def main() -> None:
    rows = []
    with (SRC / "BGL_full.log").open(encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.rstrip("\n").split(maxsplit=9)
            p += [""] * (9 - len(p))
            rows.append((p[1], p[3], p[4], p[6], p[7], p[8]))
    raw = pd.DataFrame(rows, columns=["Timestamp", "Node", "Time", "Type", "Component", "Level"])
    st = pd.read_csv(SRC / "BGL_full.log_structured.csv", usecols=["LineId", "EventId"], dtype=str)
    assert len(st) == len(raw), (len(st), len(raw))
    raw["EventId"] = st["EventId"].to_numpy()
    raw["line"] = np.arange(len(raw))
    t = pd.to_datetime(raw["Time"], format="%Y-%m-%d-%H.%M.%S.%f", errors="coerce")
    bad = int(t.isna().sum())
    raw = raw[t.notna() & raw["Node"].ne("") & raw["EventId"].notna()].copy()
    raw["t"] = t[raw.index]
    raw = raw.sort_values(["Node", "t", "line"], kind="stable")
    raw["position"] = raw.groupby("Node", sort=False).cumcount()
    can = pd.DataFrame({
        "trace_id": raw["Node"].to_numpy(), "position": raw["position"].to_numpy(np.int64),
        "activity": raw["EventId"].to_numpy(), "ts": ((raw["t"] - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)).to_numpy(np.int64),
        **{a: raw[a].replace("", None).to_numpy(dtype=object) for a in ATTRS},
    }).reset_index(drop=True)
    out = pc.canonical_path(DS)
    out.parent.mkdir(parents=True, exist_ok=True)
    can.to_parquet(out, index=False)
    csv = pd.DataFrame({"trace_id": can["trace_id"], "activity": can["activity"],
                        "timestamp": raw["t"].dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ").to_numpy(),
                        **{a: can[a] for a in ATTRS}})
    csv.to_csv(out.parent / "full.csv", index=False)
    g = can.groupby("trace_id").size()
    meta = {"dataset": DS, "events": len(can), "dropped_unparsable": bad, "traces": int(len(g)),
            "activities": int(can.activity.nunique()), "attributes": ATTRS,
            "trace_length": {"median": float(g.median()), "p90": float(g.quantile(.9)),
                             "p99": float(g.quantile(.99)), "max": int(g.max())},
            "traces_ge": {str(k): int((g >= k).sum()) for k in (1000, 5000, 10000, 50000)}}
    out.with_suffix(".json").write_text(json.dumps(meta, indent=1))
    print(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
