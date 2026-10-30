# Interactive widgets

These small widgets let you try the ideas behind an AnaTele report yourself. Each one uses the same
formulas and thresholds as the analysis code, so the numbers you see here match what AnaTele would compute.
They run entirely in your browser and do not send data anywhere.

## Tilt tracking error

A multicopter steers by leaning its thrust. AnaTele compares the lean the controller *asked for* with the lean the
estimator *reported*. It turns each roll/pitch pair into a unit "thrust direction" and measures the angle between
the two. That way roll and pitch errors combine into one number. The `tracking-loss` rule fires when this angle stays
above 30° for at least 0.5 s.

<div class="anatele-widget" data-widget="tilt-error"></div>

## Motor mixer (QUAD/X)

The mixer turns throttle, roll, pitch and yaw demands into four motor commands. On a QUAD/X frame, motors 1 (front
right) and 2 (back left) spin counter-clockwise, and motors 3 (front left) and 4 (back right) spin clockwise. Each
output must stay between `MOT_SPIN_MIN` and `MOT_SPIN_MAX`. When it can't, the mixer protects roll and pitch first and
gives up yaw. A motor stuck at its floor or ceiling is something AnaTele flags as evidence.

<div class="anatele-widget" data-widget="mixer"></div>

## Persistence: threshold plus duration

One spiky sample does not mean anything is wrong, so most AnaTele rules need a signal to stay above a threshold for
some minimum time. Samples that are too far apart (more than the maximum gap) break a run. If telemetry is too
coarse to resolve the required duration, the result is *unassessable* rather than healthy. Change the rate, gap and
dropout to see this happen.

<div class="anatele-widget" data-widget="persistence"></div>

## Hypothesis screening score

Rules that triggered add weight to the hypotheses they support, grouped into evidence families. Each family is
capped at 1.5, so many related signals (like three vibration axes) can't pile up. Contradicting findings
subtract. The score orders hypotheses for investigation. It is **not** a probability.

<div class="anatele-widget" data-widget="hypothesis"></div>

## Waypoint distance and "reached"

AnaTele measures the horizontal distance between a planned waypoint and the closest recorded vehicle position with
the haversine (great-circle) formula. It then compares that distance with the acceptance radius (`WP_RADIUS_M`, 2 m
if not logged) plus the GPS accuracy at that moment. Over a few metres a flat-earth calculation gives almost the same
answer. Switch the map to 200 km to see the two drift apart.

<div class="anatele-widget" data-widget="waypoint"></div>
