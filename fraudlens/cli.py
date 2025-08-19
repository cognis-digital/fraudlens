"""Command-line interface for FRAUDLENS.

Examples
--------
  # Backtest the default ruleset against a labeled CSV (human-readable table)
  python -m fraudlens backtest transactions.csv

  # JSON output for CI / piping into jq
  python -m fraudlens backtest transactions.csv --format json | jq .metrics

  # Only run a subset of rules
  python -m fraudlens backtest transactions.csv --rules high_amount,velocity

  # Override a threshold and fail CI if recall drops below 0.8
  python -m fraudlens backtest transactions.csv \\
      --set high_amount_threshold=500 --min-recall 0.8

  # List available rules
  python -m fraudlens rules

Exit codes
----------
  0  success and all gates (if any) passed
  1  a quality gate (--min-recall / --min-precision / --max-alert-rate) failed
  2  bad usage / unparseable input
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from . import TOOL_NAME, TOOL_VERSION
from .core import (
    backtest,
    build_ruleset,
    list_rules,
    load_transactions,
    report_to_dict,
    DEFAULTS,
)


def _coerce(value: str):
    for caster in (int, float):
        try:
            return caster(value)
        except ValueError:
            continue
    return value


def _parse_overrides(pairs: List[str]) -> dict:
    cfg = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--set expects key=value, got {pair!r}")
        key, _, raw = pair.partition("=")
        key = key.strip()
        if key not in DEFAULTS:
            raise ValueError(
                f"unknown config key {key!r}; valid keys: "
                + ", ".join(sorted(DEFAULTS))
            )
        cfg[key] = _coerce(raw.strip())
    return cfg


def _fmt_table(report_dict: dict) -> str:
    m = report_dict["metrics"]
    lines = []
    lines.append("=" * 52)
    lines.append("  FRAUDLENS backtest")
    lines.append("=" * 52)
    order = [
        ("transactions", "total"),
        ("actual fraud", "actual_fraud"),
        ("actual legit", "actual_legit"),
        ("alerts raised", "alerts"),
        ("true positives", "true_positives"),
        ("false positives", "false_positives"),
        ("false negatives", "false_negatives"),
        ("true negatives", "true_negatives"),
    ]
    for label, key in order:
        lines.append(f"  {label:<18} {m[key]:>10}")
    lines.append("-" * 52)
    for label, key in [
        ("precision", "precision"),
        ("recall", "recall"),
        ("f1", "f1"),
        ("alert rate", "alert_rate"),
        ("false-positive rate", "false_positive_rate"),
    ]:
        lines.append(f"  {label:<18} {m[key]:>10.4f}")
    lines.append("-" * 52)
    lines.append("  alerts per rule:")
    for name, count in report_dict["per_rule"].items():
        lines.append(f"    {name:<16} {count:>8}")
    lines.append("-" * 52)
    lines.append(f"  caught fraud  ({len(report_dict['caught'])}): "
                 + (", ".join(report_dict["caught"]) or "-"))
    lines.append(f"  MISSED fraud  ({len(report_dict['missed'])}): "
                 + (", ".join(report_dict["missed"]) or "-"))
    lines.append(f"  false alarms  ({len(report_dict['false_alarms'])}): "
                 + (", ".join(report_dict["false_alarms"]) or "-"))
    lines.append("=" * 52)
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="Replay transactions against pluggable fraud rules and "
                    "report precision/recall, alert volume, and a "
                    "caught-vs-missed diff.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--version", action="version",
        version=f"{TOOL_NAME} {TOOL_VERSION}",
    )
    sub = parser.add_subparsers(dest="command")

    bt = sub.add_parser(
        "backtest",
        help="replay a labeled transaction CSV against the ruleset",
        description="Run fraud rules over a labeled CSV and score the results.",
    )
    bt.add_argument("input", help="path to labeled transactions CSV")
    bt.add_argument(
        "--format", choices=["table", "json"], default="table",
        help="output format (default: table)",
    )
    bt.add_argument(
        "--rules", default=None,
        help="comma-separated subset of rule names to enable",
    )
    bt.add_argument(
        "--set", dest="overrides", action="append", default=[],
        metavar="KEY=VALUE",
        help="override a threshold (repeatable), e.g. --set high_amount_threshold=500",
    )
    bt.add_argument("--min-recall", type=float, default=None,
                    help="fail (exit 1) if recall is below this value")
    bt.add_argument("--min-precision", type=float, default=None,
                    help="fail (exit 1) if precision is below this value")
    bt.add_argument("--max-alert-rate", type=float, default=None,
                    help="fail (exit 1) if alert rate exceeds this value")

    rules_p = sub.add_parser(
        "rules", help="list available fraud rules",
        description="Print the built-in fraud rules and their descriptions.",
    )
    rules_p.add_argument("--format", choices=["table", "json"], default="table")

    return parser


def _run_backtest(args) -> int:
    try:
        txns = load_transactions(args.input)
    except FileNotFoundError:
        print(f"error: file not found: {args.input}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        cfg = _parse_overrides(args.overrides)
        enable = ([s.strip() for s in args.rules.split(",") if s.strip()]
                  if args.rules else None)
        rules = build_ruleset(config=cfg, enable=enable)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = backtest(txns, rules)
    rd = report_to_dict(report)

    if args.format == "json":
        print(json.dumps(rd, indent=2))
    else:
        print(_fmt_table(rd))

    # Quality gates for CI.
    m = rd["metrics"]
    failures = []
    if args.min_recall is not None and m["recall"] < args.min_recall:
        failures.append(f"recall {m['recall']} < min {args.min_recall}")
    if args.min_precision is not None and m["precision"] < args.min_precision:
        failures.append(f"precision {m['precision']} < min {args.min_precision}")
    if args.max_alert_rate is not None and m["alert_rate"] > args.max_alert_rate:
        failures.append(f"alert_rate {m['alert_rate']} > max {args.max_alert_rate}")

    if failures:
        for f in failures:
            print(f"GATE FAILED: {f}", file=sys.stderr)
        return 1
    return 0


def _run_rules(args) -> int:
    rules = list_rules()
    if args.format == "json":
        print(json.dumps(rules, indent=2))
    else:
        print("Available fraud rules:")
        for r in rules:
            print(f"  {r['name']:<16} {r['description']}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "backtest":
        return _run_backtest(args)
    if args.command == "rules":
        return _run_rules(args)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
