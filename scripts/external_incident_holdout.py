#!/usr/bin/env python3
"""Prospectively frozen extension of the external-incident challenge.

The four cases in this file were fixed before executing this script. They are
deterministic reproductions of fault mechanisms documented in public issue
trackers. Existing semantic contracts, baseline implementations, and thresholds
are imported unchanged.

This is an author-conducted, incident-derived robustness evaluation. It is not
an independent-party study and does not use the reporters' original datasets.
All outcomes, including misses, must be retained and reported.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.external_incident_validation import evaluate, sha256


INCIDENTS = [
    {
        "incident_id": "incremental_latest_record_replayed",
        "dataset": "walmart",
        "source": "https://github.com/airbytehq/airbyte/issues/14066",
        "reported_fault": (
            "Repeated incremental syncs with no new source data appended the "
            "most recent record again."
        ),
        "instantiation": (
            "Append one exact copy of the deterministically selected latest "
            "Store-Date record."
        ),
    },
    {
        "incident_id": "one_to_many_join_multiplication",
        "dataset": "walmart",
        "source": "https://github.com/pola-rs/polars/issues/13550",
        "reported_fault": (
            "A left join declared as one-to-one accepted a duplicate key on the "
            "right and unexpectedly multiplied matching output rows."
        ),
        "instantiation": (
            "Duplicate every output row matching the smallest Store key, as "
            "would occur when that lookup key appears twice on the right."
        ),
    },
    {
        "incident_id": "incremental_cursor_boundary_skip",
        "dataset": "power",
        "source": "https://github.com/airbytehq/airbyte/issues/14732",
        "reported_fault": (
            "A strict greater-than cursor boundary permanently skipped records "
            "inserted with a timestamp equal to the saved cursor."
        ),
        "instantiation": (
            "Remove all records at the median observed timestamp, representing "
            "the equality-boundary records omitted by the next sync."
        ),
    },
    {
        "incident_id": "missing_output_partition",
        "dataset": "power",
        "source": "https://issues.apache.org/jira/browse/SPARK-23895",
        "reported_fault": (
            "A failed executor left incomplete intermediate output, yet the job "
            "continued and loaded incomplete data into the destination."
        ),
        "instantiation": (
            "Sort by timestamp, divide the output into 100 deterministic "
            "partitions, and omit partition 50."
        ),
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--walmart", type=Path, default=ROOT / "data/processed/walmart_clean.csv")
    parser.add_argument("--stock", type=Path, default=ROOT / "data/processed/stock_clean.csv")
    parser.add_argument("--power", type=Path, default=ROOT / "data/processed/power_clean.csv")
    parser.add_argument("--out", type=Path, default=ROOT / "experiments/external_incident_holdout")
    return parser.parse_args()


def incremental_latest_record_replayed(base: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    required = {"Store", "Date"}
    if not required.issubset(base.columns):
        raise ValueError(f"Walmart data must contain {sorted(required)}")
    clean = base.copy()
    ordered = clean.assign(__dt=pd.to_datetime(clean["Date"], errors="coerce")).sort_values(
        ["__dt", "Store"], kind="stable"
    )
    latest_index = ordered.index[-1]
    replayed = clean.loc[[latest_index]].copy()
    test = pd.concat([clean, replayed], ignore_index=True)
    return clean, test


def one_to_many_join_multiplication(base: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if "Store" not in base.columns:
        raise ValueError("Walmart data must contain Store")
    clean = base.copy()
    key = clean["Store"].dropna().min()
    multiplied = clean.loc[clean["Store"] == key].copy()
    if multiplied.empty:
        raise ValueError("No rows matched the deterministic Store key")
    test = pd.concat([clean, multiplied], ignore_index=True)
    return clean, test


def incremental_cursor_boundary_skip(base: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if "DateTime" not in base.columns:
        raise ValueError("Power data must contain DateTime")
    clean = base.copy()
    timestamps = pd.to_datetime(clean["DateTime"], errors="coerce")
    unique = pd.DatetimeIndex(timestamps.dropna().unique()).sort_values()
    if len(unique) < 3:
        raise ValueError("Insufficient timestamps")
    boundary = unique[len(unique) // 2]
    test = clean.loc[timestamps != boundary].copy().reset_index(drop=True)
    return clean, test


def missing_output_partition(base: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if "DateTime" not in base.columns:
        raise ValueError("Power data must contain DateTime")
    clean = base.copy()
    ordered = clean.assign(__dt=pd.to_datetime(clean["DateTime"], errors="coerce")).sort_values(
        "__dt", kind="stable"
    )
    n = len(ordered)
    start = (49 * n) // 100
    stop = (50 * n) // 100
    keep = pd.Series(True, index=ordered.index)
    keep.loc[ordered.iloc[start:stop].index] = False
    test = clean.loc[keep].copy().reset_index(drop=True)
    return clean, test


BUILDERS = {
    "incremental_latest_record_replayed": incremental_latest_record_replayed,
    "one_to_many_join_multiplication": one_to_many_join_multiplication,
    "incremental_cursor_boundary_skip": incremental_cursor_boundary_skip,
    "missing_output_partition": missing_output_partition,
}


def main() -> None:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    paths = {"walmart": args.walmart, "stock": args.stock, "power": args.power}
    datasets = {name: pd.read_csv(path) for name, path in paths.items()}

    protocol = {
        "protocol_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "design": (
            "Case list and deterministic transformations fixed before execution; "
            "existing contracts, thresholds, and baselines unchanged."
        ),
        "scope": "Author-conducted deterministic reproductions of externally reported mechanisms.",
        "selection_rule": (
            "Public issue report from a maintained data-processing project; explicit "
            "incorrect tabular output; mechanism maps deterministically to an existing "
            "study dataset; no duplicate of the first four challenge reports."
        ),
        "reporting_rule": "Retain and report all four outcomes; no post-result tuning or exclusions.",
        "limitations": (
            "Not an independent-party evaluation, not the reporters' original production "
            "data, and not a representative population sample."
        ),
        "challenge_script_sha256": sha256(Path(__file__)),
        "base_evaluation_script_sha256": sha256(ROOT / "scripts/external_incident_validation.py"),
        "contract_file_sha256": sha256(ROOT / "src/validators/contracts.py"),
        "semantic_validator_sha256": sha256(ROOT / "src/validators/semantic_validator.py"),
        "baseline_file_sha256": {
            name: sha256(ROOT / "src/baselines" / filename)
            for name, filename in {
                "ensemble": "ensemble_baseline.py",
                "ks": "ks_drift_baseline.py",
                "ge": "ge_baseline.py",
            }.items()
        },
        "source_data": {
            name: {
                "path": str(path),
                "sha256": sha256(path),
                "rows": len(datasets[name]),
                "columns": list(datasets[name].columns),
            }
            for name, path in paths.items()
        },
        "python": platform.python_version(),
        "platform": platform.platform(),
        "pandas": pd.__version__,
        "incidents": INCIDENTS,
    }
    (args.out / "protocol.json").write_text(json.dumps(protocol, indent=2) + "\n", encoding="utf-8")

    rows = []
    for incident in INCIDENTS:
        clean, test = BUILDERS[incident["incident_id"]](datasets[incident["dataset"]])
        row = evaluate(incident, clean, test)
        rows.append(row)
        print(
            f"{incident['incident_id']}: semantic={row['semantic_detected']} "
            f"ensemble={row['ensemble_detected']} ks={row['ks_detected']} "
            f"ge={row['ge_detected']} fired={row['semantic_fired_detectors'] or '-'}"
        )

    results = pd.DataFrame(rows)
    results.to_csv(args.out / "external_incident_holdout_results.csv", index=False)
    summary = {
        "n_incidents": len(results),
        "semantic_detected": int(results["semantic_detected"].sum()),
        "ensemble_detected": int(results["ensemble_detected"].sum()),
        "ks_detected": int(results["ks_detected"].sum()),
        "ge_detected": int(results["ge_detected"].sum()),
        "semantic_clean_control_flags": int(results["semantic_clean_control_detected"].sum()),
    }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"Saved results to {args.out}")


if __name__ == "__main__":
    main()
