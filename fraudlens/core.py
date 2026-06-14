"""Core engine for FRAUDLENS.

Parses labeled transaction records, evaluates pluggable fraud rules against
them, and computes detection quality metrics (precision/recall/F1), alert
volume, and a caught-vs-missed diff versus the ground-truth labels.

No third-party dependencies.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Sequence


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Transaction:
    """A single transaction record.

    `is_fraud` is the ground-truth label (1 fraud / 0 legit). It is used only
    for scoring, never for detection.
    """

    txn_id: str
    account_id: str
    amount: float
    timestamp: datetime
    merchant: str
    country: str
    channel: str  # e.g. card_present, online, atm
    is_fraud: int

    @property
    def hour(self) -> int:
        return self.timestamp.hour


@dataclass
class RuleHit:
    txn_id: str
    rule_name: str
    reason: str


@dataclass
class Rule:
    """A pluggable fraud rule.

    `predicate` receives (txn, context) and returns either False / None for no
    alert, or a truthy reason string when the transaction should be flagged.
    `context` carries cross-transaction state (e.g. velocity counters) built
    once per backtest run.
    """

    name: str
    description: str
    predicate: Callable[["Transaction", dict], object]

    def evaluate(self, txn: "Transaction", context: dict) -> Optional[RuleHit]:
        result = self.predicate(txn, context)
        if result:
            reason = result if isinstance(result, str) else self.description
            return RuleHit(txn_id=txn.txn_id, rule_name=self.name, reason=reason)
        return None


@dataclass
class Metrics:
    total: int
    actual_fraud: int
    actual_legit: int
    alerts: int
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int
    precision: float
    recall: float
    f1: float
    alert_rate: float
    false_positive_rate: float


@dataclass
class Report:
    metrics: Metrics
    per_rule: Dict[str, int]
    caught: List[str] = field(default_factory=list)
    missed: List[str] = field(default_factory=list)
    false_alarms: List[str] = field(default_factory=list)
    hits: List[RuleHit] = field(default_factory=list)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

_REQUIRED = ["txn_id", "account_id", "amount", "timestamp", "merchant",
             "country", "channel", "is_fraud"]


def _parse_ts(raw: str) -> datetime:
    raw = raw.strip()
    # Support trailing Z and naive ISO timestamps.
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"unparseable timestamp: {raw!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_transactions(text: str) -> List[Transaction]:
    """Parse CSV text into Transaction objects.

    Expects a header row containing the required columns. Raises ValueError on
    malformed input so CI surfaces bad data instead of silently scoring zero.
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("empty input: no header row found")
    missing = [c for c in _REQUIRED if c not in reader.fieldnames]
    if missing:
        raise ValueError(f"missing required columns: {', '.join(missing)}")

    txns: List[Transaction] = []
    seen_ids: set = set()
    for lineno, row in enumerate(reader, start=2):
        try:
            is_fraud_raw = int(float(row["is_fraud"]))
            if is_fraud_raw not in (0, 1):
                raise ValueError(
                    f"is_fraud must be 0 or 1, got {is_fraud_raw}"
                )
            txn = Transaction(
                txn_id=row["txn_id"].strip(),
                account_id=row["account_id"].strip(),
                amount=float(row["amount"]),
                timestamp=_parse_ts(row["timestamp"]),
                merchant=row["merchant"].strip(),
                country=row["country"].strip().upper(),
                channel=row["channel"].strip().lower(),
                is_fraud=is_fraud_raw,
            )
        except (ValueError, KeyError, AttributeError) as exc:
            raise ValueError(f"row {lineno}: {exc}") from exc
        if not txn.txn_id:
            raise ValueError(f"row {lineno}: empty txn_id")
        if txn.txn_id in seen_ids:
            raise ValueError(f"row {lineno}: duplicate txn_id {txn.txn_id!r}")
        seen_ids.add(txn.txn_id)
        txns.append(txn)
    return txns


def load_transactions(path: str) -> List[Transaction]:
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return parse_transactions(fh.read())
    except IsADirectoryError:
        raise ValueError(f"path is a directory, not a file: {path!r}")
    except OSError as exc:
        raise ValueError(f"cannot read {path!r}: {exc.strerror}") from exc


# --------------------------------------------------------------------------
# Built-in rules
# --------------------------------------------------------------------------

# Reasonable defaults; a library user can override via build_ruleset(...).
DEFAULTS = {
    "high_amount_threshold": 1000.0,
    "velocity_window_seconds": 300,  # 5 minutes
    "velocity_count": 3,            # >=N txns in window flags later ones
    "odd_hour_start": 1,            # inclusive
    "odd_hour_end": 5,              # inclusive
    "odd_hour_min_amount": 200.0,
    "home_country": "US",
    "foreign_min_amount": 100.0,
}


def _build_context(txns: Sequence[Transaction], cfg: dict) -> dict:
    """Precompute cross-transaction state used by stateful rules.

    Velocity: for each transaction, how many transactions occurred on the same
    account within the preceding window (inclusive of the current one).
    """
    by_account: Dict[str, List[Transaction]] = {}
    for t in txns:
        by_account.setdefault(t.account_id, []).append(t)

    window = cfg["velocity_window_seconds"]
    velocity: Dict[str, int] = {}
    for acct, items in by_account.items():
        items.sort(key=lambda t: t.timestamp)
        start = 0
        for i, t in enumerate(items):
            while (t.timestamp - items[start].timestamp).total_seconds() > window:
                start += 1
            velocity[t.txn_id] = i - start + 1
    return {"velocity": velocity, "cfg": cfg}


def _rule_high_amount(txn: Transaction, ctx: dict):
    thr = ctx["cfg"]["high_amount_threshold"]
    if txn.amount >= thr:
        return f"amount {txn.amount:.2f} >= {thr:.2f}"
    return None


def _rule_velocity(txn: Transaction, ctx: dict):
    n = ctx["velocity"].get(txn.txn_id, 1)
    need = ctx["cfg"]["velocity_count"]
    if n >= need:
        win = ctx["cfg"]["velocity_window_seconds"]
        return f"{n} txns on account within {win}s (>= {need})"
    return None


def _rule_odd_hour(txn: Transaction, ctx: dict):
    cfg = ctx["cfg"]
    lo, hi = cfg["odd_hour_start"], cfg["odd_hour_end"]
    if lo <= txn.hour <= hi and txn.amount >= cfg["odd_hour_min_amount"]:
        return f"odd-hour {txn.hour:02d}:00 spend {txn.amount:.2f}"
    return None


def _rule_foreign(txn: Transaction, ctx: dict):
    cfg = ctx["cfg"]
    if txn.country != cfg["home_country"] and txn.amount >= cfg["foreign_min_amount"]:
        return f"foreign country {txn.country} spend {txn.amount:.2f}"
    return None


def build_ruleset(config: Optional[dict] = None,
                  enable: Optional[Iterable[str]] = None) -> List[Rule]:
    """Construct the default ruleset.

    `config` overrides any DEFAULTS thresholds. `enable` optionally restricts
    to a subset of rule names.
    """
    cfg = dict(DEFAULTS)
    if config:
        cfg.update(config)

    rules = [
        Rule("high_amount", "transaction amount over threshold",
             _rule_high_amount),
        Rule("velocity", "rapid-fire transactions on one account",
             _rule_velocity),
        Rule("odd_hour", "sizeable spend during overnight hours",
             _rule_odd_hour),
        Rule("foreign_geo", "spend outside home country",
             _rule_foreign),
    ]
    # Stash config so context builder can read it.
    for r in rules:
        r.description = r.description  # keep dataclass mutable-friendly  # noqa: B005
    if enable is not None:
        wanted = set(enable)
        unknown = wanted - {r.name for r in rules}
        if unknown:
            raise ValueError(f"unknown rule(s): {', '.join(sorted(unknown))}")
        rules = [r for r in rules if r.name in wanted]
    # Attach cfg to the ruleset via a sentinel rule attribute used by backtest.
    for r in rules:
        setattr(r, "_cfg", cfg)
    return rules


def list_rules() -> List[Dict[str, str]]:
    return [{"name": r.name, "description": r.description}
            for r in build_ruleset()]


# --------------------------------------------------------------------------
# Backtest
# --------------------------------------------------------------------------

def _div(a: float, b: float) -> float:
    return a / b if b else 0.0


def backtest(txns: Sequence[Transaction], rules: Sequence[Rule]) -> Report:
    """Replay transactions against rules and score against ground truth.

    A transaction is *alerted* if any rule fires. Scoring compares the alert
    set against the `is_fraud` labels.
    """
    cfg = dict(DEFAULTS)
    for r in rules:
        cfg.update(getattr(r, "_cfg", {}))
    context = _build_context(txns, cfg)

    alerted: Dict[str, Transaction] = {}
    hits: List[RuleHit] = []
    per_rule: Dict[str, int] = {r.name: 0 for r in rules}

    for txn in txns:
        for rule in rules:
            hit = rule.evaluate(txn, context)
            if hit is not None:
                hits.append(hit)
                per_rule[rule.name] += 1
                alerted[txn.txn_id] = txn

    actual_fraud = sum(1 for t in txns if t.is_fraud)
    actual_legit = len(txns) - actual_fraud

    tp = sum(1 for t in alerted.values() if t.is_fraud)
    fp = sum(1 for t in alerted.values() if not t.is_fraud)
    fn = actual_fraud - tp
    tn = actual_legit - fp

    precision = _div(tp, tp + fp)
    recall = _div(tp, tp + fn)
    f1 = _div(2 * precision * recall, precision + recall)

    metrics = Metrics(
        total=len(txns),
        actual_fraud=actual_fraud,
        actual_legit=actual_legit,
        alerts=len(alerted),
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        true_negatives=tn,
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(f1, 4),
        alert_rate=round(_div(len(alerted), len(txns)), 4),
        false_positive_rate=round(_div(fp, actual_legit), 4),
    )

    caught = sorted(t.txn_id for t in alerted.values() if t.is_fraud)
    missed = sorted(t.txn_id for t in txns if t.is_fraud and t.txn_id not in alerted)
    false_alarms = sorted(t.txn_id for t in alerted.values() if not t.is_fraud)

    return Report(
        metrics=metrics,
        per_rule=per_rule,
        caught=caught,
        missed=missed,
        false_alarms=false_alarms,
        hits=hits,
    )


def report_to_dict(report: Report) -> dict:
    m = report.metrics
    return {
        "metrics": {
            "total": m.total,
            "actual_fraud": m.actual_fraud,
            "actual_legit": m.actual_legit,
            "alerts": m.alerts,
            "true_positives": m.true_positives,
            "false_positives": m.false_positives,
            "false_negatives": m.false_negatives,
            "true_negatives": m.true_negatives,
            "precision": m.precision,
            "recall": m.recall,
            "f1": m.f1,
            "alert_rate": m.alert_rate,
            "false_positive_rate": m.false_positive_rate,
        },
        "per_rule": report.per_rule,
        "caught": report.caught,
        "missed": report.missed,
        "false_alarms": report.false_alarms,
    }


def to_json(report: Report, indent: int = 2) -> str:
    return json.dumps(report_to_dict(report), indent=indent)
