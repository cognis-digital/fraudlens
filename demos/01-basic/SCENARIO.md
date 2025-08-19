# Demo 01 - Basic fraud-rule backtest

This demo runs FRAUDLENS over a small labeled transaction set
(`transactions.csv`) containing 12 transactions, 4 of which are real fraud
(`is_fraud=1`).

The sample exercises every built-in rule:

- **high_amount** - a $4,200 electronics purchase (`T010`) and a $2,500
  online buy (`T006`).
- **velocity** - three rapid card-present hits on account `A200`
  (`T002`/`T003`/`T004`) within five minutes.
- **odd_hour** - a 03:00 spend (`T008`).
- **foreign_geo** - non-US transactions (`T006` in GB, `T011` in RO).

## Run it

```sh
python -m fraudlens backtest demos/01-basic/transactions.csv
```

or as JSON for CI / piping:

```sh
python -m fraudlens backtest demos/01-basic/transactions.csv --format json
```

## Expected result

All **4** fraudulent transactions (`T004`, `T006`, `T008`, `T010`) are caught,
so **recall = 1.0** and `missed` is empty. The legit $1,500 rent payment
`T012` trips the high_amount rule and shows up as a false alarm, so precision
is below 1.0 (a realistic precision/recall trade-off).

The `backtest` exit code is `0`. Adding a gate like `--min-recall 0.9` keeps
it `0`; a stricter `--min-precision 0.95` makes it exit `1` so a CI pipeline
would flag the false-alarm regression.

Try tuning a threshold to remove the false alarm:

```sh
python -m fraudlens backtest demos/01-basic/transactions.csv \
    --set high_amount_threshold=2000
```
