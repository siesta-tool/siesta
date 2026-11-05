"""
tests/vldb_eval/hdfs_data.py — the HDFS log (LogHub 2.0) as an event log.

Inputs (downloaded from https://zenodo.org/records/8275861, HDFS.zip):
  datasets/loghub2_hdfs/HDFS/HDFS_full.log                 raw lines
  datasets/loghub2_hdfs/HDFS/HDFS_full.log_structured.csv  LineId, Content, EventId

One event per log line: case = the block id the line mentions (``blk_...``;
every line of this log mentions exactly one), activity = EventId, attributes
Level and Component (raw line: ``Date Time Pid Level Component: Content``).
Timestamps have second precision; to give every system the same, unambiguous
order, each line gets a microsecond offset equal to its rank among the lines
of the same second (log order), so events are ordered by log line.

Writes
  results/data/hdfs/canonical.parquet   trace_id, position, activity, ts, Level, Component
  results/data/hdfs/full.csv            trace_id, activity, timestamp (ISO, microseconds), attributes

Usage:
    python -m tests.vldb_eval.hdfs_data
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from tests.vldb_eval import pattern_common as pc

SRC = Path(__file__).resolve().parents[2] / "datasets" / "loghub2_hdfs" / "HDFS"
DS = "hdfs"
ATTRS = ["Level", "Component"]
BLK = re.compile(r"blk_-?\d+")


def main() -> None:
    date, tim, level, comp = [], [], [], []
    with (SRC / "HDFS_full.log").open(encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.split(maxsplit=5)
            p += [""] * (5 - len(p))
            date.append(p[0])
            tim.append(p[1])
            level.append(p[3])
            comp.append(p[4].rstrip(":"))
    st = pd.read_csv(SRC / "HDFS_full.log_structured.csv", usecols=["Content", "EventId"], dtype=str)
    assert len(st) == len(date), (len(st), len(date))
    blk = st["Content"].str.extract(r"(blk_-?\d+)", expand=False)
    raw = pd.DataFrame({"date": date, "time": tim, "Level": level, "Component": comp,
                        "EventId": st["EventId"].to_numpy(), "blk": blk.to_numpy()})
    del date, tim, level, comp, st, blk
    raw["line"] = np.arange(len(raw))
    sec = pd.to_datetime("20" + raw["date"] + raw["time"], format="%Y%m%d%H%M%S", errors="coerce")
    bad = int(sec.isna().sum() + raw["blk"].isna().sum())
    raw = raw[sec.notna() & raw["blk"].notna()].copy()
    raw["sec"] = sec[raw.index]
    # microsecond offset = rank of the line within its second (log order)
    raw["us"] = raw.groupby("sec", sort=False).cumcount()
    assert raw["us"].max() < 1_000_000
    raw["t"] = raw["sec"] + pd.to_timedelta(raw["us"], unit="us")
    raw = raw.sort_values(["blk", "t", "line"], kind="stable")
    raw["position"] = raw.groupby("blk", sort=False).cumcount()
    can = pd.DataFrame({
        "trace_id": raw["blk"].to_numpy(), "position": raw["position"].to_numpy(np.int64),
        "activity": raw["EventId"].to_numpy(), "ts": ((raw["sec"] - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)).to_numpy(np.int64),
        **{a: raw[a].replace("", None).to_numpy(dtype=object) for a in ATTRS},
    }).reset_index(drop=True)
    out = pc.canonical_path(DS)
    out.parent.mkdir(parents=True, exist_ok=True)
    can.to_parquet(out, index=False)
    pd.DataFrame({"trace_id": can["trace_id"], "activity": can["activity"],
                  "timestamp": raw["t"].dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ").to_numpy(),
                  **{a: can[a] for a in ATTRS}}).to_csv(out.parent / "full.csv", index=False)
    g = can.groupby("trace_id").size()
    meta = {"dataset": DS, "events": len(can), "dropped": bad, "traces": int(len(g)),
            "activities": int(can.activity.nunique()), "attributes": ATTRS,
            "trace_length": {"median": float(g.median()), "p99": float(g.quantile(.99)), "max": int(g.max())},
            "traces_ge": {str(k): int((g >= k).sum()) for k in (50, 100, 200)}}
    out.with_suffix(".json").write_text(json.dumps(meta, indent=1))
    print(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
