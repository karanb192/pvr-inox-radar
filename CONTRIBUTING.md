# Contributing

Small, focused PRs are welcome. The bar for merging is the same bar the
repo holds itself to: offline tests for every behavior, politeness toward
the upstream API, and no em or en dashes anywhere (a test enforces this).

## Setup

There is nothing to install. Python 3.9+ standard library only.

```
git clone https://github.com/karanb192/pvr-inox-radar.git
cd pvr-inox-radar
python3 -m unittest discover -s tests
```

All tests run offline against the captured fixtures in `tests/fixtures/`.
The optional live smoke test is gated behind `PVR_LIVE=1` and is never run
in CI.

## Rules that will not bend

- **Politeness.** Never lower the pacing floor, remove the 403/429
  cooldown, or add parallel API calls. The upstream blocks IPs that get
  hammered, and every user of this skill shares that fate individually.
- **Read-only.** No booking, holding, carting, or payment automation of
  any kind. PRs that add them will be closed.
- **Honesty in output.** Verified counts and unverified labels stay
  visually and textually distinct. Do not blur that line.
- **Tests or it did not happen.** New behavior ships with offline tests;
  fixture updates ship with the capture script change that produced them.

## Good first contributions

- New city quirks (rollups, coordless cities) with a fixture.
- Better title parsing for regional-language parentheticals.
- Map and table rendering improvements that keep the no-JS fallback whole.
