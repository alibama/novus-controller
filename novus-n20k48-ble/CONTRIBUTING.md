# Contributing

Thanks for your interest. This started as one studio's tool and is most useful
if the community improves the protocol decode and broadens hardware coverage.

## Ground rules

- **Safety first.** This software drives real kilns and furnaces. Any change to
  control logic (`monitor.py` watchdog, `novus_client.py` write paths) must
  fail safe: never drive output on an implausible reading, never remove a guard
  without a clear argument, and prefer alerting a human over taking an action
  whose effect you can't verify.
- **Reading is safe; writing is not.** New features that *read* registers are
  low-risk. Features that *write* need a confirmation step in the UI and should
  be tested against the protocol tests first.
- Keep the pure protocol layer (`novus_protocol.py`) free of I/O so it stays
  unit-testable without hardware.

## Dev setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install pytest
pytest tests/          # runs without hardware (BLE is stubbed)
```

CI runs `pytest` on every push/PR (see `.github/workflows/ci.yml`).

## Especially wanted

- Confirmation or correction of the register map in `docs/PROTOCOL.md`
- The password/session mechanism (registers 51 / 53)
- The power-loss **resume-mode** register (suspected 253, unconfirmed)
- Program-table edge cases (linking, looping, "active segment" semantics)
- Support for sibling controllers (N1200, N480D, …)
- Packet captures from QuickTune Mobile doing operations we haven't decoded

## How to report hardware findings

When you confirm or contradict something about the protocol, please include:
your controller model and firmware, what you sent (hex frame if you have it),
what came back, and how you observed the effect (front panel, multimeter, etc.).
A correction with evidence is worth more than a dozen guesses.

## Pull requests

- One logical change per PR; describe what hardware (if any) you tested on.
- Run `pytest` and keep it green.
- Don't commit secrets or real device addresses — `notify.env`, `kiln_config.py`,
  `devices.json`, and `logs/` are gitignored for a reason.
