#!/usr/bin/env python3
"""Detector-frozen, incident-derived challenge for construct-validity analysis.

The semantic contracts are treated as frozen.  This script instantiates four
fault patterns specified by reporters in public issue trackers and evaluates
the existing validators without changing any contract or threshold.

This is an incident-derived reproduction study, not a claim that the original
production datasets from the issue reports are available.  The incidents are
an author-selected convenience set, not a random or statistically representative
sample of production failures.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.baselines.ensemble_baseline import run_ensemble_baseline
from src.baselines.ge_baseline import run_ge_baseline
from src.baselines.ks_drift_baseline import run_ks_baseline
from src.validators.semantic_validator import validate


INCIDENTS = [
    {
        "incident_id": "feature_order_mismatch",
        "dataset": "stock",
        "source": "https://github.com/scikit-learn/scikit-learn/issues/7242",
        "reported_fault": "Prediction data used a different feature-column order than training data.",
        "instantiation": "Exchange the positions, but not the labels or values, of Open and Close.",
    },
    {
        "incident_id": "null_key_duplicate_append",
        "dataset": "walmart",
        "source": "https://github.com/dbt-labs/dbt-core/issues/7873",
        "reported_fault": "Rows with NULL unique-key components were appended instead of replaced.",
        "instantiation": "Duplicate a deterministic 3% subset whose incident-only key is NULL.",
    },
    {
        "incident_id": "duplicate_aggregation_outputs",
        "dataset": "walmart",
        "source": "https://github.com/pandas-dev/pandas/issues/43067",
        "reported_fault": (
            "pd.concat created an invalid MultiIndex whose codes caused groupby "
            "to emit duplicate groups instead of one aggregated group."
        ),
        "instantiation": "Split 3% of Store-Date groups into two rows while preserving each group sum.",
    },
    {
        "incident_id": "timezone_conversion_error",
        "dataset": "power",
        "source": "https://issues.apache.org/jira/browse/SPARK-23715",
        "reported_fault": "UTC conversion produced an incorrect timestamp because of timezone handling.",
        "instantiation": "Shift every timestamp backward by seven hours, matching the documented example offset.",
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--walmart", type=Path, default=ROOT / "data/processed/walmart_clean.csv")
    parser.add_argument("--stock", type=Path, default=ROOT / "data/processed/stock_clean.csv")
    parser.add_argument("--power", type=Path, default=ROOT / "data/processed/power_clean.csv")
    parser.add_argument("--out", type=Path, default=ROOT / "experiments/external_incident_validation")
    return parser.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def feature_order_mismatch(base: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not {"Open", "Close"}.issubset(base.columns):
        raise ValueError("Stock data must contain Open and Close columns")
    columns = list(base.columns)
    open_index, close_index = columns.index("Open"), columns.index("Close")
    columns[open_index], columns[close_index] = columns[close_index], columns[open_index]
    return base.copy(), base.loc[:, columns].copy()


def null_key_duplicate_append(base: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(7873)
    clean = base.copy()
    clean["__incident_key"] = pd.Series(np.arange(len(clean)), dtype="Int64")
    count = max(1, int(round(0.03 * len(clean))))
    selected = np.sort(rng.choice(len(clean), size=count, replace=False))
    clean.loc[selected, "__incident_key"] = pd.NA
    duplicated = clean.iloc[selected].copy()
    test = pd.concat([clean, duplicated], ignore_index=True)
    return clean, test


def duplicate_aggregation_outputs(base: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    required = {"Store", "Date", "Weekly_Sales"}
    if not required.issubset(base.columns):
        raise ValueError(f"Walmart data must contain {sorted(required)}")
    rng = np.random.default_rng(43067)
    clean = base.copy()
    count = max(1, int(round(0.03 * len(clean))))
    selected = np.sort(rng.choice(len(clean), size=count, replace=False))
    untouched = clean.drop(index=selected)
    left = clean.iloc[selected].copy()
    right = clean.iloc[selected].copy()
    left["Weekly_Sales"] = pd.to_numeric(left["Weekly_Sales"], errors="coerce") * 0.5
    right["Weekly_Sales"] = pd.to_numeric(right["Weekly_Sales"], errors="coerce") * 0.5
    test = pd.concat([untouched, left, right], ignore_index=True)
    test = test.sort_values(["Date", "Store"], kind="stable").reset_index(drop=True)
    return clean, test


def timezone_conversion_error(base: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if "DateTime" not in base.columns:
        raise ValueError("Power data must contain DateTime")
    test = base.copy()
    parsed = pd.to_datetime(test["DateTime"], errors="coerce")
    test["DateTime"] = parsed - pd.Timedelta(hours=7)
    return base.copy(), test


BUILDERS = {
    "feature_order_mismatch": feature_order_mismatch,
    "null_key_duplicate_append": null_key_duplicate_append,
    "duplicate_aggregation_outputs": duplicate_aggregation_outputs,
    "timezone_conversion_error": timezone_conversion_error,
}


def max_numeric_mean_change(clean: pd.DataFrame, test: pd.DataFrame) -> float:
    common = [
        column for column in clean.columns.intersection(test.columns)
        if pd.api.types.is_numeric_dtype(clean[column]) and pd.api.types.is_numeric_dtype(test[column])
    ]
    changes = []
    for column in common:
        clean_mean = float(pd.to_numeric(clean[column], errors="coerce").mean())
        test_mean = float(pd.to_numeric(test[column], errors="coerce").mean())
        denominator = max(abs(clean_mean), 1e-12)
        changes.append(abs(test_mean - clean_mean) / denominator)
    return max(changes, default=0.0)


def timed(callable_):
    start = time.perf_counter()
    result = callable_()
    return result, time.perf_counter() - start


def evaluate(incident: dict, clean: pd.DataFrame, test: pd.DataFrame) -> dict:
    dataset = incident["dataset"]
    semantic, semantic_seconds = timed(lambda: validate(clean, test, dataset=dataset))
    ensemble, ensemble_seconds = timed(lambda: run_ensemble_baseline(clean, test))
    ks, ks_seconds = timed(lambda: run_ks_baseline(clean, test))
    ge, ge_seconds = timed(lambda: run_ge_baseline(clean, test, dataset=dataset))

    clean_semantic = validate(clean, clean, dataset=dataset)
    return {
        **incident,
        "clean_rows": len(clean),
        "test_rows": len(test),
        "row_count_change": (len(test) - len(clean)) / max(len(clean), 1),
        "same_column_names": set(clean.columns) == set(test.columns),
        "same_column_order": list(clean.columns) == list(test.columns),
        "max_numeric_mean_relative_change": max_numeric_mean_change(clean, test),
        "semantic_detected": semantic["detected"],
        "semantic_fired_detectors": "|".join(semantic["fired_detectors"]),
        "semantic_max_confidence": semantic["max_confidence"],
        "semantic_clean_control_detected": clean_semantic["detected"],
        "ensemble_detected": ensemble["detected"],
        "ensemble_n_fired": ensemble["n_fired"],
        "ks_detected": ks["detected"],
        "ks_score": ks["score"],
        "ge_detected": ge["detected"],
        "ge_n_failed": ge["n_failed"],
        "semantic_seconds": semantic_seconds,
        "ensemble_seconds": ensemble_seconds,
        "ks_seconds": ks_seconds,
        "ge_seconds": ge_seconds,
    }


def main() -> None:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    paths = {"walmart": args.walmart, "stock": args.stock, "power": args.power}
    datasets = {name: pd.read_csv(path) for name, path in paths.items()}

    protocol = {
        "protocol_version": "2.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "design": "Existing contracts and thresholds are frozen; no outcome-dependent tuning is permitted.",
        "scope": "Incident-derived deterministic reproductions, not original production datasets.",
        "selection": "Author-selected convenience set of public issue reports; not random or representative.",
        "interpretation": (
            "Report all case outcomes descriptively. Do not treat the four cases as independent "
            "population samples or use them to estimate production-wide detection performance."
        ),
        "challenge_script_sha256": sha256(Path(__file__)),
        "contract_file_sha256": sha256(ROOT / "src/validators/contracts.py"),
        "semantic_validator_sha256": sha256(ROOT / "src/validators/semantic_validator.py"),
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
        "numpy": np.__version__,
        "incidents": INCIDENTS,
    }
    (args.out / "protocol.json").write_text(json.dumps(protocol, indent=2) + "\n", encoding="utf-8")

    rows = []
    for incident in INCIDENTS:
        source = datasets[incident["dataset"]]
        clean, test = BUILDERS[incident["incident_id"]](source)
        row = evaluate(incident, clean, test)
        rows.append(row)
        print(
            f"{incident['incident_id']}: semantic={row['semantic_detected']} "
            f"ensemble={row['ensemble_detected']} ks={row['ks_detected']} ge={row['ge_detected']} "
            f"fired={row['semantic_fired_detectors'] or '-'}"
        )

    results = pd.DataFrame(rows)
    results.to_csv(args.out / "external_incident_results.csv", index=False)
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
