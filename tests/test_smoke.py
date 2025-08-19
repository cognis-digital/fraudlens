"""Smoke tests for FRAUDLENS - import core, run on the demo, assert behavior."""

import json
import os
import subprocess
import sys

import pytest

from fraudlens import (
    TOOL_NAME,
    TOOL_VERSION,
    backtest,
    build_ruleset,
    list_rules,
    load_transactions,
    parse_transactions,
    report_to_dict,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(REPO_ROOT, "demos", "01-basic", "transactions.csv")


def test_tool_metadata():
    assert TOOL_NAME == "fraudlens"
    assert TOOL_VERSION.count(".") == 2


def test_load_demo():
    txns = load_transactions(DEMO)
    assert len(txns) == 12
    assert sum(t.is_fraud for t in txns) == 4
    # Country normalized to upper, channel to lower.
    gb = [t for t in txns if t.txn_id == "T006"][0]
    assert gb.country == "GB"
    assert gb.channel == "online"


def test_backtest_catches_all_fraud_on_demo():
    txns = load_transactions(DEMO)
    report = backtest(txns, build_ruleset())
    m = report.metrics
    # All 4 frauds are caught by the default ruleset.
    assert m.recall == 1.0
    assert m.true_positives == 4
    assert report.missed == []
    assert set(report.caught) == {"T004", "T006", "T008", "T010"}
    # The $1,500 legit rent payment is a false alarm -> precision < 1.
    assert "T012" in report.false_alarms
    assert m.precision < 1.0
    assert 0.0 <= m.f1 <= 1.0


def test_velocity_rule_fires_on_account_burst():
    txns = load_transactions(DEMO)
    report = backtest(txns, build_ruleset(enable=["velocity"]))
    # A200 has 3 txns within 5 min; the 3rd (T004) should be flagged.
    assert "T004" in report.caught
    assert report.per_rule["velocity"] >= 1


def test_threshold_override_removes_false_alarm():
    txns = load_transactions(DEMO)
    base = backtest(txns, build_ruleset())
    tuned = backtest(txns, build_ruleset(config={"high_amount_threshold": 2000}))
    # Raising the threshold drops the $1,500 rent false alarm.
    assert "T012" in base.false_alarms
    assert "T012" not in tuned.false_alarms
    assert tuned.metrics.precision >= base.metrics.precision


def test_parse_rejects_missing_columns():
    with pytest.raises(ValueError):
        parse_transactions("txn_id,amount\nT1,5\n")


def test_parse_rejects_bad_amount():
    bad = ("txn_id,account_id,amount,timestamp,merchant,country,channel,is_fraud\n"
           "T1,A1,notanumber,2026-06-01T00:00:00Z,M,US,online,0\n")
    with pytest.raises(ValueError):
        parse_transactions(bad)


def test_list_rules_nonempty():
    rules = list_rules()
    names = {r["name"] for r in rules}
    assert {"high_amount", "velocity", "odd_hour", "foreign_geo"} <= names


def test_report_to_dict_is_json_serializable():
    txns = load_transactions(DEMO)
    rd = report_to_dict(backtest(txns, build_ruleset()))
    s = json.dumps(rd)
    assert "metrics" in json.loads(s)


def _run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "fraudlens", *args],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )


def test_cli_version():
    res = _run_cli("--version")
    assert res.returncode == 0
    assert "fraudlens" in res.stdout.lower()


def test_cli_json_output_and_exit_zero():
    res = _run_cli("backtest", DEMO, "--format", "json")
    assert res.returncode == 0
    payload = json.loads(res.stdout)
    assert payload["metrics"]["recall"] == 1.0


def test_cli_gate_fails_with_exit_one():
    # Demo precision is < 0.95 due to the T012 false alarm -> gate fails.
    res = _run_cli("backtest", DEMO, "--format", "json", "--min-precision", "0.95")
    assert res.returncode == 1
    assert "GATE FAILED" in res.stderr


def test_cli_gate_passes_with_recall_gate():
    res = _run_cli("backtest", DEMO, "--min-recall", "0.9")
    assert res.returncode == 0
