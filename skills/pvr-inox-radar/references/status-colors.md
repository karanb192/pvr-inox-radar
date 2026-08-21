# statusCode color legend

Every csessions show carries `statusCode`, a hex chip color the PVR INOX web
app paints its showtime buttons with, plus a `statusTxt` label. This skill
maps confirmed hexes to categories (`pvr_client.status_category`) and always
passes the raw hex through so the renderer can paint the API's own color
even when the category is unknown.

## Confirmed

| statusCode | statusTxt seen | category | evidence |
|---|---|---|---|
| 76BE43 | Available | available | all 82 shows in the 2026-08-21 capture (both Gurugram venues, 2026-08-22) |

## Not yet confirmed

The capture day was a quiet one: every sampled show was green. The other
states the web app is known to show ("Filling Up Fast", "Almost Full",
"Housefull", "Lapsed" in the reference project's statusTxt handling) were
not observed, so their hex values are unmapped. Every unmapped hex reads as
category `unknown` (gray) by design; the skill never guesses a legend.

Consequence for the map: the legend's red "housefull or sold out" PIN state
is reserved and cannot appear yet, because no confirmed hex maps to it and
the venue rollup never produces it. Red CHIPS can appear: they come from
verified seat counts (a fetched seat map that cannot seat the party), not
from status colors. A busier capture can extend the table above; add a hex
only with fixture evidence.

## Labels lie; counted seats do not

The reference project (notprashanth/pvr-inox-mcp, MIT, by Prashanth
Krishnan) measured statusTxt lagging in both directions across 160 sampled
shows: "Lapsed" up to 5.4 hours in the future, "Available" on shows already
started, "Housefull" with 1 seat free, "Filling Up Fast" with 9 of 508
free. That finding is theirs. It is why chip colors are only a cheap first
pass here and shortlisted shows are re-colored from verified seat counts.
