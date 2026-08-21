# Limits

The honest list. Each limit names the test or reference that pins it.

- **PVR INOX chain only.** Other chains, single screens, and BookMyShow
  listings are out of scope in v1 (see Roadmap in the README). Design
  scope, not a bug.
- **Seat counts are a floor, not a ceiling.** PVR withholds some seat rows
  and releases them later; a withheld seat is indistinguishable from a
  sold one in a single snapshot. A show that reads full may just not be
  fully on sale yet. Documented in
  `skills/pvr-inox-radar/references/status-colors.md`.
- **Availability labels lag.** PVR's own status text can be stale in both
  directions, so the tool treats labels as a first pass and counted seats
  as truth (`tests/test_render.py` keeps the two visually distinct).
- **Only the green "Available" status color is confirmed.** Busy-day
  colors (fast filling, housefull) have not been captured yet, so those
  pin states are reserved and cannot appear
  (`skills/pvr-inox-radar/references/status-colors.md`, asserted in
  `tests/test_render.py`).
- **Drive times are estimates.** One OSRM table call when reachable, a
  labeled heuristic when not; both always carry an "est." tag
  (`tests/test_radar.py`).
- **A date not on sale answers HTTP 500.** The client maps this to
  "closed", distinct from errors and blocks (`tests/test_client.py`).
  PVR sells a rolling window of about 5 days.
- **The upstream blocks heavy users.** Calls are paced and budgeted; a
  403/429 trips a cooldown instead of retries (`tests/test_client.py`).
  Bulk polling is deliberately not implemented.
- **Four cities carry no coordinates upstream** (Gangtok, Jabalpur, Leh,
  Muzaffarpur); queries there need an explicit lat and lng
  (`tests/test_radar.py`).
- **Unofficial API.** The whole data source is undocumented and can change
  or be closed without notice. The offline test suite keeps the tool
  honest about what it does, not about what the API will do tomorrow.
