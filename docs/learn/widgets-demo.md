# Interactive widgets

With these small widgets, you can try the ideas of an AnaTele report yourself. Each widget uses the same
formulas and thresholds as the analysis code. Thus, the numbers here agree with the numbers that AnaTele calculates.
The widgets run fully in your browser. They do not send data to other locations.

## Tilt tracking error

A multicopter steers when it tilts its thrust. AnaTele compares the tilt that the controller *asked for* with the tilt that the
estimator *reported*. It changes each roll/pitch pair into a unit "thrust direction" and measures the angle between
the two directions. Thus, the roll error and the pitch error become one number. The `tracking-loss` rule triggers when this angle stays
above 30° for a minimum of 0.5 s.

<div class="anatele-widget" data-widget="tilt-error"></div>

## Motor mixer (QUAD/X)

The mixer changes throttle, roll, pitch and yaw demands into four motor commands. On a QUAD/X frame, motor 1 (front
right) and motor 2 (back left) turn counter-clockwise. Motor 3 (front left) and motor 4 (back right) turn clockwise. Each
output must stay between `MOT_SPIN_MIN` and `MOT_SPIN_MAX`. When this is not possible, the mixer keeps roll and pitch first, and
reduces yaw. AnaTele flags a motor that stays at its floor or ceiling as evidence.

<div class="anatele-widget" data-widget="mixer"></div>

## Persistence: threshold plus duration

One sample with a high peak does not mean that a problem exists. Thus, most AnaTele rules need a signal to stay above a threshold for
a minimum time. If the interval between samples is too long (more than the maximum gap), the run stops. If the telemetry rate is too
low for the necessary duration, the result is *unassessable*, not healthy. Change the rate, the gap and the
dropout to see this result.

<div class="anatele-widget" data-widget="persistence"></div>

## Hypothesis screening score

Each rule that triggered adds weight to the hypotheses that it supports. AnaTele puts the rules into evidence families. Each family has a
limit of 1.5. Thus, many related signals (for example three vibration axes) cannot increase the score too much. Contradicting findings
decrease the score. The score puts the hypotheses in a sequence for the investigation. It is **not** a probability.

<div class="anatele-widget" data-widget="hypothesis"></div>

## Waypoint distance and "reached"

AnaTele uses the haversine (great-circle) formula to measure the horizontal distance between a planned waypoint and the nearest recorded vehicle position.
Then it compares that distance with the acceptance radius plus the GPS accuracy at that time. The acceptance radius is `WP_RADIUS_M`, or 2 m
if the log does not contain it. Over some metres, a flat-earth calculation gives almost the same
answer. To see the two results become different, set the map to 200 km.

<div class="anatele-widget" data-widget="waypoint"></div>
