/* AnaTele interactive teaching widgets. Vanilla JS, no dependencies.
 * Usage in Markdown: <div class="anatele-widget" data-widget="tilt-error"></div>
 * Widgets: tilt-error, mixer, persistence, hypothesis, waypoint.
 * The maths mirrors telemetry_platform/ (derived.py, simulator.py, rules.py, rules/core.yaml, missions.py).
 */
(function (root) {
  "use strict";

  // ------------------------------------------------------------------ maths (exported for tests)
  var DEG = Math.PI / 180;

  function clamp(value, low, high) { return Math.max(low, Math.min(high, value)); }
  function maxOf(list) { return Math.max.apply(null, list); }
  function minOf(list) { return Math.min.apply(null, list); }

  // derived.py tilt_vector / tilt_error_deg
  function tiltVector(rollDeg, pitchDeg) {
    var r = rollDeg * DEG, p = pitchDeg * DEG;
    return [Math.sin(p) * Math.cos(r), -Math.sin(r), Math.cos(p) * Math.cos(r)];
  }
  function tiltErrorDeg(desired, actual) {
    var a = tiltVector(desired[0], desired[1]), b = tiltVector(actual[0], actual[1]);
    var dot = clamp(a[0] * b[0] + a[1] * b[1] + a[2] * b[2], -1, 1);
    return Math.acos(dot) / DEG;
  }
  // rules/core.yaml tracking-loss: above 30 deg for >= 0.5 s, max_gap_s 1.0
  var TRACKING_LOSS = { above: 30, forS: 0.5, maxGapS: 1.0 };

  // derived.py FRAME_LAYOUTS[(1, 1)] "QUAD/X": x = right, y = forward, spin +1 = CCW prop
  var QUAD_X = [
    { motor: 1, x: 1, y: 1, spin: 1 },
    { motor: 2, x: -1, y: -1, spin: 1 },
    { motor: 3, x: -1, y: 1, spin: -1 },
    { motor: 4, x: 1, y: -1, spin: -1 }
  ];
  // simulator.py Frame defaults and parameters (MOT_SPIN_MIN/MAX, MOT_PWM_MIN/MAX)
  var MIXER_DEFAULTS = { spinMin: 0.15, spinMax: 0.95, pwmMin: 1000, pwmMax: 2000 };

  // simulator.py Simulator.control (mixer section) + derived.py motor_mapping/derive_motors flags.
  // roll/pitch/yaw are per-motor output fractions (the simulator's torque / (4 * lever * max_thrust) terms).
  function mixQuadX(throttle, roll, pitch, yaw, options) {
    var o = options || MIXER_DEFAULTS, low = o.spinMin, high = o.spinMax;
    var rp = QUAD_X.map(function (m) { return -m.x * roll + m.y * pitch; });
    var yw = QUAD_X.map(function (m) { return m.spin * yaw; });
    var demanded = rp.map(function (v, i) { return throttle + v + yw[i]; });
    var collective = clamp(throttle, low, high);
    var span = maxOf(rp) - minOf(rp);
    var rpScale = 1;
    if (span > high - low) {
      rpScale = (high - low) / span;
      rp = rp.map(function (v) { return v * rpScale; });
    }
    var shift = Math.max(0, maxOf(rp.map(function (v) { return collective + v; })) - high) -
                Math.max(0, low - minOf(rp.map(function (v) { return collective + v; })));
    var base = rp.map(function (v) { return collective - shift + v; });
    var headroom = Math.min(minOf(base.map(function (b) { return high - b; })), minOf(base.map(function (b) { return b - low; })));
    var biggest = maxOf(yw.map(Math.abs)) || 1.0;
    var yawScale = clamp(headroom / biggest, 0, 1);
    var outputs = base.map(function (b, i) { return clamp(b + yawScale * yw[i], low, high); });
    var pwmSpan = o.pwmMax - o.pwmMin;
    var pwm = outputs.map(function (u) { return Math.trunc(o.pwmMin + pwmSpan * u + 1e-9); });
    var floor = o.pwmMin + o.spinMin * pwmSpan, ceiling = o.pwmMin + o.spinMax * pwmSpan;
    return {
      motors: QUAD_X.map(function (m, i) {
        return {
          motor: m.motor, x: m.x, y: m.y, spin: m.spin, demanded: demanded[i], output: outputs[i], pwm: pwm[i],
          atFloor: pwm[i] <= floor + 2, atCeiling: pwm[i] >= ceiling - 2
        };
      }),
      collective: collective, shift: shift, rpScale: rpScale, yawScale: yawScale, floor: floor, ceiling: ceiling
    };
  }

  // rules.py runs_above for a single window; samples = [{t: microseconds, v: value, id}]
  function runsAbove(samples, limit, maxGapUs) {
    var runs = [], current = null, previousT = null;
    samples.forEach(function (s) {
      var gapOk = previousT === null || s.t - previousT <= maxGapUs;
      if (s.v > limit && gapOk && current !== null) {
        current.end = s.t; current.lastId = s.id; current.n += 1; current.max = Math.max(current.max, s.v);
      } else if (s.v > limit) {
        if (current) { runs.push(current); }
        current = { start: s.t, end: s.t, firstId: s.id, lastId: s.id, n: 1, max: s.v };
      } else {
        if (current) { runs.push(current); }
        current = null;
      }
      previousT = s.t;
    });
    if (current) { runs.push(current); }
    return runs;
  }

  // rules.py d_persistence for one series, one window and one criterion
  function persistence(samples, limit, forS, maxGapS) {
    var runs = runsAbove(samples, limit, Math.trunc(maxGapS * 1e6));
    var longest = null;
    runs.forEach(function (r) { if (!longest || r.end - r.start > longest.end - longest.start) { longest = r; } });
    var duration = longest ? (longest.end - longest.start) / 1e6 : 0;
    var triggered = !!longest && duration >= forS;
    var intervals = [];
    for (var i = 0; i + 1 < samples.length; i++) { intervals.push((samples[i + 1].t - samples[i].t) / 1e6); }
    intervals.sort(function (a, b) { return a - b; });
    var medianInterval = intervals.length ? intervals[Math.floor(intervals.length / 2)] : null;
    var state;
    if (samples.length < 3) { state = "unassessable"; }
    else if (!triggered && medianInterval !== null && medianInterval >= forS) { state = "unassessable"; }
    else { state = triggered ? "triggered" : "not_triggered"; }
    return { state: state, runs: runs, longest: longest, duration: duration, triggered: triggered, medianInterval: medianInterval };
  }

  // rules/core.yaml hypotheses (family_cap 1.5)
  var FAMILY_CAP = 1.5;
  var HYPOTHESES = [
    { id: "magnetic-estimation", title: "Estimator/magnetic inconsistency",
      support: [
        { rule: "compass-event-before-crash-check", family: "estimator", weight: 1 },
        { rule: "ekf-compass-innovation-pre-event", family: "magnetic", weight: 1 },
        // per: source rule; rules.py _in_episode() only keeps episode/event-scoped results, so it never counts.
        { rule: "compasses-inconsistent-reported", family: "magnetic", weight: 0.25, sourceScoped: true }],
      contradict: [{ rule: "compass-event-before-crash-check", flag: "contradictions", weight: 1 }] },
    { id: "thrust-or-configuration", title: "Mechanical/thrust-generation or frame configuration problem",
      support: [
        { rule: "tracking-loss", family: "control", weight: 1 },
        { rule: "motor-floor-saturation-pre-event", family: "propulsion", weight: 1 },
        { rule: "motor-output-imbalance-pre-event", family: "propulsion", weight: 0.75 },
        { rule: "motor-ceiling-saturation", family: "propulsion", weight: 0.5 }],
      contradict: [] },
    { id: "vibration-estimation", title: "Vibration-driven estimation degradation",
      support: [
        { rule: "vibration-persistent-pre-event", family: "inertial", weight: 1 },
        { rule: "clipping-pre-event", family: "inertial", weight: 1 }],
      contradict: [
        { rule: "vibration-persistent-pre-event", state: "not_triggered", weight: 0.5 },
        { rule: "clipping-pre-event", state: "not_triggered", weight: 0.5 }] },
    { id: "power-degradation", title: "Electrical/power degradation", blockedBy: ["power-monitoring-gap"],
      support: [], contradict: [] }
  ];

  // rules.py rank_hypotheses for one hypothesis; results = [{rule, state, contradictions: [...]}]
  function scoreHypothesis(hypothesis, results, cap) {
    cap = cap === undefined ? FAMILY_CAP : cap;
    var families = {}, support = [], against = [], assessed = false, penalty = 0;
    (hypothesis.support || []).forEach(function (item) {
      if (item.sourceScoped) { return; }
      results.filter(function (r) { return r.rule === item.rule; }).forEach(function (r) {
        if (r.state === "triggered" || r.state === "not_triggered") { assessed = true; }
        if (r.state === "triggered") {
          families[item.family] = (families[item.family] || 0) + item.weight;
          support.push(item);
        }
      });
    });
    (hypothesis.contradict || []).forEach(function (item) {
      results.filter(function (r) { return r.rule === item.rule; }).forEach(function (r) {
        var flagged = (item.flag && r[item.flag] && r[item.flag].length) || (item.state && r.state === item.state);
        if (flagged) { penalty += item.weight; against.push(item); }
      });
    });
    var blocked = results.filter(function (r) {
      return (hypothesis.blockedBy || []).indexOf(r.rule) >= 0 && r.state === "triggered";
    });
    var score = 0;
    Object.keys(families).forEach(function (f) { score += Math.min(cap, families[f]); });
    score -= penalty;
    var label;
    if (blocked.length || (!assessed && !against.length)) { label = "unassessable"; }
    else if (support.length && against.length) { label = "mixed support"; }
    else if (score >= 2) { label = "strong support"; }
    else if (score >= 1) { label = "some support"; }
    else if (score > 0) { label = "weak support"; }
    else { label = "limited or contradicting evidence"; }
    return { label: label, score: Math.round(score * 100) / 100, families: families, penalty: penalty,
             support: support, against: against, blocked: blocked.length > 0 };
  }

  // missions.py horizontal_m (haversine) and execution() status logic
  var EARTH_RADIUS_M = 6371008.8;
  var DEFAULT_RADIUS_M = 2.0;
  var HOME = [47.397742, 8.545594]; // simulator.py HOME (public SITL default field)
  function horizontalM(lat1, lon1, lat2, lon2) {
    var p1 = lat1 * DEG, p2 = lat2 * DEG, dp = p2 - p1, dl = (lon2 - lon1) * DEG;
    var a = Math.pow(Math.sin(dp / 2), 2) + Math.cos(p1) * Math.cos(p2) * Math.pow(Math.sin(dl / 2), 2);
    return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1.0, Math.sqrt(a)));
  }
  // Flat-earth (equirectangular) approximation with the same radius, for comparison only.
  function flatM(lat1, lon1, lat2, lon2) {
    var dn = (lat2 - lat1) * DEG * EARTH_RADIUS_M;
    var de = (lon2 - lon1) * DEG * EARTH_RADIUS_M * Math.cos((lat1 + lat2) / 2 * DEG);
    return Math.hypot(dn, de);
  }
  // simulator.py latlon(): 111320 m per degree, longitude scaled by cos(home latitude). Used to place map points.
  function toLatLon(n, e, ref) {
    ref = ref || HOME;
    return [ref[0] + n / 111320.0, ref[1] + e / (111320.0 * Math.cos(ref[0] * DEG))];
  }
  function waypointStatus(distanceM, radiusM, hAccM, reported, coverageOk) {
    if (reported) { return "reported_complete"; }
    var margin = radiusM + (hAccM ? hAccM : 0);
    if (distanceM <= margin) { return "geometry_consistent_with_completion"; }
    return coverageOk === false ? "completion_not_observed" : "not_visited_in_observed_trajectory";
  }

  // Seeded PRNG for reproducible demo signals
  function mulberry32(seed) {
    var a = seed >>> 0;
    return function () {
      a = (a + 0x6D2B79F5) >>> 0;
      var t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  function noiseAt(seed, tMs) {
    var rand = mulberry32((Math.imul(seed, 2654435761) + Math.imul(tMs, 40503)) >>> 0);
    var u = Math.max(rand(), 1e-12), v = rand();
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
  }
  // Demo tilt-error-like signal (deg) sampled at rateHz over durationS, with an optional telemetry dropout.
  function demoSignal(seed, rateHz, durationS, dropout) {
    var samples = [], step = Math.round(1000 / rateHz), id = 1;
    for (var tMs = 0; tMs <= durationS * 1000; tMs += step, id++) {
      var t = tMs / 1000;
      if (dropout && t > 12.0 && t < 13.4) { continue; }
      var v = 12 + 5 * Math.sin(t * 0.9) + 30 * Math.exp(-Math.pow((t - 12.6) / 1.6, 2)) +
              14 * Math.exp(-Math.pow((t - 5) / 0.25, 2)) + 3 * noiseAt(seed, tMs);
      samples.push({ t: tMs * 1000, v: Math.max(0, v), id: id });
    }
    return samples;
  }

  var api = {
    tiltVector: tiltVector, tiltErrorDeg: tiltErrorDeg, TRACKING_LOSS: TRACKING_LOSS,
    QUAD_X: QUAD_X, MIXER_DEFAULTS: MIXER_DEFAULTS, mixQuadX: mixQuadX,
    runsAbove: runsAbove, persistence: persistence, demoSignal: demoSignal, mulberry32: mulberry32,
    FAMILY_CAP: FAMILY_CAP, HYPOTHESES: HYPOTHESES, scoreHypothesis: scoreHypothesis,
    EARTH_RADIUS_M: EARTH_RADIUS_M, DEFAULT_RADIUS_M: DEFAULT_RADIUS_M, HOME: HOME,
    horizontalM: horizontalM, flatM: flatM, toLatLon: toLatLon, waypointStatus: waypointStatus
  };
  root.AnaTeleLearn = api;
  if (typeof module !== "undefined" && module.exports) { module.exports = api; }
  if (typeof document === "undefined") { return; }

  // ------------------------------------------------------------------ DOM helpers
  var SVG_NS = "http://www.w3.org/2000/svg";
  var uid = 0;
  function nextId(prefix) { uid += 1; return "anatele-" + prefix + "-" + uid; }

  function setAttrs(node, attrs) {
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") { node.textContent = attrs[k]; }
      else if (attrs[k] !== null && attrs[k] !== undefined && attrs[k] !== false) { node.setAttribute(k, attrs[k]); }
    });
  }
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    setAttrs(node, attrs);
    (children || []).forEach(function (c) { node.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return node;
  }
  function svg(tag, attrs, parent) {
    var node = document.createElementNS(SVG_NS, tag);
    setAttrs(node, attrs);
    if (parent) { parent.appendChild(node); }
    return node;
  }
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }
  function fmt(value, digits) { return Number(value).toFixed(digits); }

  function frame(node, title, caption) {
    var headingId = nextId("title");
    node.classList.add("anatele-widget--ready");
    node.setAttribute("role", "group");
    node.setAttribute("aria-labelledby", headingId);
    node.appendChild(el("p", { "class": "anatele-widget__title", id: headingId, text: title }));
    node.appendChild(el("p", { "class": "anatele-widget__caption", text: caption }));
    var body = el("div", { "class": "anatele-widget__body" });
    node.appendChild(body);
    return body;
  }

  function slider(parent, label, min, max, step, value, unit, onInput) {
    var id = nextId("in");
    var input = el("input", { type: "range", id: id, min: min, max: max, step: step, value: value });
    var out = el("output", { "for": id, "class": "anatele-widget__value" });
    var decimals = String(step).indexOf(".") >= 0 ? String(step).split(".")[1].length : 0;
    function show() {
      out.textContent = fmt(input.value, decimals) + (unit ? " " + unit : "");
      input.setAttribute("aria-valuetext", out.textContent);
    }
    input.addEventListener("input", function () { show(); onInput(); });
    show();
    parent.appendChild(el("div", { "class": "anatele-widget__control" },
      [el("label", { "for": id, text: label }), input, out]));
    return { input: input, get value() { return parseFloat(input.value); } };
  }

  function checkbox(parent, label, checked, onChange) {
    var id = nextId("cb");
    var input = el("input", { type: "checkbox", id: id, checked: checked ? "checked" : null });
    input.addEventListener("change", onChange);
    parent.appendChild(el("div", { "class": "anatele-widget__check" }, [input, el("label", { "for": id, text: label })]));
    return input;
  }

  function select(parent, label, options, value, onChange) {
    var id = nextId("sel");
    var input = el("select", { id: id }, options.map(function (o) {
      return el("option", { value: o[0], text: o[1], selected: o[0] === value ? "selected" : null });
    }));
    input.addEventListener("change", onChange);
    parent.appendChild(el("div", { "class": "anatele-widget__control anatele-widget__control--select" },
      [el("label", { "for": id, text: label }), input]));
    return input;
  }

  function badge(text, kind) { return el("span", { "class": "anatele-badge anatele-badge--" + kind, text: text }); }
  function liveRegion(parent) {
    var region = el("div", { "class": "anatele-widget__readout", "aria-live": "polite" });
    parent.appendChild(region);
    return region;
  }
  function split(body) {
    var layout = el("div", { "class": "anatele-widget__split" });
    var controls = el("div", { "class": "anatele-widget__controls" });
    var figure = el("div", { "class": "anatele-widget__figure" });
    layout.appendChild(controls); layout.appendChild(figure); body.appendChild(layout);
    return { controls: controls, figure: figure };
  }

  // ------------------------------------------------------------------ widget: tilt-error
  function renderTilt(node) {
    var body = frame(node, "Tilt (thrust-vector) tracking error",
      "Set where the controller wanted the drone to lean (desired) and where it actually leaned. " +
      "AnaTele measures the angle between the two thrust directions, so a roll error and a pitch error are combined.");
    var parts = split(body), controls = parts.controls, figure = parts.figure;
    var dr = slider(controls, "Desired roll", -90, 90, 1, 10, "°", update);
    var dp = slider(controls, "Desired pitch", -90, 90, 1, -5, "°", update);
    var ar = slider(controls, "Actual roll", -90, 90, 1, 35, "°", update);
    var ap = slider(controls, "Actual pitch", -90, 90, 1, 12, "°", update);
    var readout = liveRegion(body);

    var size = 220, c = size / 2, R = 90;
    var s = svg("svg", { viewBox: "0 0 " + size + " " + size, "class": "anatele-svg", role: "img",
      "aria-label": "Top-down projection of the desired and actual tilt vectors" }, figure);
    svg("circle", { cx: c, cy: c, r: R, "class": "anatele-svg__ring" }, s);
    svg("circle", { cx: c, cy: c, r: R * Math.sin(30 * DEG), "class": "anatele-svg__ring anatele-svg__ring--faint" }, s);
    svg("line", { x1: c - R, y1: c, x2: c + R, y2: c, "class": "anatele-svg__axis" }, s);
    svg("line", { x1: c, y1: c - R, x2: c, y2: c + R, "class": "anatele-svg__axis" }, s);
    svg("text", { x: c + 4, y: c - R + 12, "class": "anatele-svg__label", text: "+x" }, s);
    svg("text", { x: c + R - 16, y: c - 4, "class": "anatele-svg__label", text: "+y" }, s);
    var desiredLine = svg("line", { x1: c, y1: c, x2: c, y2: c, "class": "anatele-svg__vec anatele-svg__vec--desired" }, s);
    var actualLine = svg("line", { x1: c, y1: c, x2: c, y2: c, "class": "anatele-svg__vec anatele-svg__vec--actual" }, s);
    var desiredDot = svg("circle", { r: 5, "class": "anatele-svg__dot anatele-svg__dot--desired" }, s);
    var actualDot = svg("circle", { r: 5, "class": "anatele-svg__dot anatele-svg__dot--actual" }, s);
    figure.appendChild(el("p", { "class": "anatele-widget__legend" }, [
      el("span", { "class": "anatele-key anatele-key--desired" }), " desired ",
      el("span", { "class": "anatele-key anatele-key--actual" }), " actual · inner ring = 30° tilt · hollow dot = vector points below the horizon"]));

    function place(line, dot, v, kind) {
      // x/y components of the unit vector: x drawn up, y drawn right.
      var px = c + R * v[1], py = c - R * v[0];
      setAttrs(line, { x2: px, y2: py });
      setAttrs(dot, { cx: px, cy: py, "class": "anatele-svg__dot anatele-svg__dot--" + kind + (v[2] < 0 ? " is-below" : "") });
    }
    function update() {
      var vd = tiltVector(dr.value, dp.value), va = tiltVector(ar.value, ap.value);
      place(desiredLine, desiredDot, vd, "desired");
      place(actualLine, actualDot, va, "actual");
      var err = tiltErrorDeg([dr.value, dp.value], [ar.value, ap.value]);
      var above = err > TRACKING_LOSS.above;
      clear(readout);
      readout.appendChild(el("p", {}, [
        "Tilt error = ", el("strong", { text: fmt(err, 1) + "°" }), " ",
        above ? badge("above the 30° threshold", "warn") : badge("at or below the 30° threshold", "ok")]));
      readout.appendChild(el("p", { "class": "anatele-widget__small", text:
        "Desired vector (" + vd.map(function (x) { return fmt(x, 3); }).join(", ") + "), actual (" +
        va.map(function (x) { return fmt(x, 3); }).join(", ") + "). The tracking-loss rule only triggers if the error " +
        "stays above 30° for at least 0.5 s; one sample is not enough." }));
    }
    update();
  }

  // ------------------------------------------------------------------ widget: mixer
  function renderMixer(node) {
    var body = frame(node, "QUAD/X motor mixer",
      "Throttle sets the average motor output; roll, pitch and yaw commands push some motors up and others down. " +
      "Outputs are limited to MOT_SPIN_MIN–MOT_SPIN_MAX (0.15–0.95), i.e. 1150–1950 µs with a 1000–2000 µs PWM range.");
    var parts = split(body), controls = parts.controls, figure = parts.figure;
    var th = slider(controls, "Throttle", 0, 1, 0.01, 0.5, "", update);
    var ro = slider(controls, "Roll (+ = roll right)", -0.5, 0.5, 0.01, 0, "", update);
    var pi = slider(controls, "Pitch (+ = nose up)", -0.5, 0.5, 0.01, 0, "", update);
    var ya = slider(controls, "Yaw (+ = speed up CCW motors 1 and 2)", -0.5, 0.5, 0.01, 0, "", update);

    var size = 220, c = size / 2, arm = 62;
    var s = svg("svg", { viewBox: "0 0 " + size + " " + size, "class": "anatele-svg", role: "img",
      "aria-label": "QUAD/X frame seen from above, front at the top, with motor outputs" }, figure);
    svg("line", { x1: c - arm, y1: c - arm, x2: c + arm, y2: c + arm, "class": "anatele-svg__frame" }, s);
    svg("line", { x1: c - arm, y1: c + arm, x2: c + arm, y2: c - arm, "class": "anatele-svg__frame" }, s);
    svg("path", { d: "M" + (c - 8) + "," + (c - 2) + " L" + c + "," + (c - 16) + " L" + (c + 8) + "," + (c - 2) + " Z",
      "class": "anatele-svg__nose" }, s);
    svg("text", { x: c, y: 14, "text-anchor": "middle", "class": "anatele-svg__label", text: "front" }, s);
    var rotors = QUAD_X.map(function (m) {
      var x = c + m.x * arm, y = c - m.y * arm;
      var disc = svg("circle", { cx: x, cy: y, r: 26, "class": "anatele-svg__rotor" }, s);
      var fill = svg("circle", { cx: x, cy: y, r: 0, "class": "anatele-svg__rotor-fill" }, s);
      svg("text", { x: x, y: y - 1, "text-anchor": "middle", "class": "anatele-svg__motor", text: String(m.motor) }, s);
      svg("text", { x: x, y: y + 13, "text-anchor": "middle", "class": "anatele-svg__label", text: m.spin > 0 ? "CCW" : "CW" }, s);
      return { disc: disc, fill: fill };
    });
    var bars = el("div", { "class": "anatele-bars" });
    body.appendChild(bars);
    var readout = liveRegion(body);

    function update() {
      var mix = mixQuadX(th.value, ro.value, pi.value, ya.value);
      clear(bars);
      mix.motors.forEach(function (m, i) {
        var sat = m.atFloor ? "floor" : (m.atCeiling ? "ceiling" : "");
        rotors[i].fill.setAttribute("r", fmt(26 * m.output, 2));
        rotors[i].disc.setAttribute("class", "anatele-svg__rotor" + (sat ? " is-saturated" : ""));
        var demandPct = clamp(m.demanded, 0, 1) * 100;
        bars.appendChild(el("div", { "class": "anatele-bar" + (sat ? " is-saturated" : "") }, [
          el("span", { "class": "anatele-bar__name", text: "Motor " + m.motor }),
          el("span", { "class": "anatele-bar__track", role: "meter", "aria-valuemin": "0", "aria-valuemax": "1",
            "aria-valuenow": fmt(m.output, 3), "aria-label": "Motor " + m.motor + " output" }, [
            el("span", { "class": "anatele-bar__band", style: "left:15%;width:80%" }),
            el("span", { "class": "anatele-bar__fill", style: "width:" + fmt(m.output * 100, 1) + "%" }),
            el("span", { "class": "anatele-bar__demand", style: "left:" + fmt(demandPct, 1) + "%",
              title: "unlimited demand " + fmt(m.demanded, 2) })]),
          el("span", { "class": "anatele-bar__value", text: fmt(m.output, 2) + " · " + m.pwm + " µs" + (sat ? " · at " + sat : "") })]));
      });
      var notes = [];
      if (mix.rpScale < 1) { notes.push("roll/pitch scaled to " + fmt(mix.rpScale * 100, 0) + "% (difference exceeds the output range)"); }
      if (Math.abs(mix.shift) > 1e-9) { notes.push("throttle moved by " + fmt(-mix.shift, 2) + " to protect roll/pitch"); }
      if (ya.value !== 0 && mix.yawScale < 1) { notes.push("yaw reduced to " + fmt(mix.yawScale * 100, 0) + "% (not enough headroom)"); }
      clear(readout);
      readout.appendChild(el("p", { "class": "anatele-widget__small", text:
        (notes.length ? "Limiting: " + notes.join("; ") + ". " : "No limiting needed. ") +
        "The thin marker shows the unlimited demand; the shaded band is the allowed range. AnaTele flags a motor at floor " +
        "if PWM ≤ " + (mix.floor + 2) + " µs and at ceiling if PWM ≥ " + (mix.ceiling - 2) + " µs." }));
    }
    update();
  }

  // ------------------------------------------------------------------ widget: persistence
  function renderPersistence(node) {
    var body = frame(node, "Persistence: threshold + duration",
      "A rule only fires if the signal stays above the threshold for long enough. Samples further apart than the " +
      "maximum gap break a run. Defaults are the tracking-loss rule (above 30° for 0.5 s, max gap 1.0 s).");
    var controls = el("div", { "class": "anatele-widget__controls anatele-widget__controls--grid" });
    body.appendChild(controls);
    var seed = 7;
    var thr = slider(controls, "Threshold (above)", 5, 50, 1, 30, "°", update);
    var dur = slider(controls, "Required duration (for_s)", 0.1, 4, 0.1, 0.5, "s", update);
    var gap = slider(controls, "Maximum gap (max_gap_s)", 0.2, 3, 0.1, 1.0, "s", update);
    var rate = select(controls, "Telemetry rate", [["10", "10 Hz"], ["4", "4 Hz"], ["2", "2 Hz"], ["1", "1 Hz"]], "10", update);
    var drop = checkbox(controls, "Simulate a 1.4 s telemetry dropout", false, update);
    var reseed = el("button", { type: "button", "class": "md-button anatele-widget__button", text: "New noise" });
    reseed.addEventListener("click", function () { seed += 1; update(); });
    controls.appendChild(reseed);

    var W = 640, H = 200, padL = 34, padR = 8, padT = 8, padB = 22, vMax = 60, tMax = 20;
    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, "class": "anatele-svg anatele-svg--wide", role: "img",
      "aria-label": "Signal over 20 seconds with the threshold line and runs above it" }, body);
    var readout = liveRegion(body);
    function X(tUs) { return padL + (W - padL - padR) * (tUs / 1e6) / tMax; }
    function Y(v) { return padT + (H - padT - padB) * (1 - Math.min(v, vMax) / vMax); }

    function update() {
      var samples = demoSignal(seed, parseFloat(rate.value), tMax, drop.checked);
      var res = persistence(samples, thr.value, dur.value, gap.value);
      clear(s);
      [0, 20, 40, 60].forEach(function (v) {
        svg("line", { x1: padL, x2: W - padR, y1: Y(v), y2: Y(v), "class": "anatele-svg__grid" }, s);
        svg("text", { x: padL - 4, y: Y(v) + 4, "text-anchor": "end", "class": "anatele-svg__label", text: String(v) }, s);
      });
      for (var t = 0; t <= tMax; t += 5) {
        svg("text", { x: X(t * 1e6), y: H - 6, "text-anchor": "middle", "class": "anatele-svg__label", text: t + " s" }, s);
      }
      res.runs.forEach(function (r) {
        var qualifies = (r.end - r.start) / 1e6 >= dur.value;
        svg("rect", { x: X(r.start) - 2, y: padT, width: Math.max(4, X(r.end) - X(r.start) + 4), height: H - padT - padB,
          "class": "anatele-svg__run" + (qualifies ? " is-qualifying" : "") }, s);
      });
      svg("line", { x1: padL, x2: W - padR, y1: Y(thr.value), y2: Y(thr.value), "class": "anatele-svg__threshold" }, s);
      var path = "", prev = null;
      samples.forEach(function (p) {
        var broken = prev === null || p.t - prev > gap.value * 1e6;
        path += (broken ? "M" : "L") + fmt(X(p.t), 1) + "," + fmt(Y(p.v), 1);
        prev = p.t;
      });
      svg("path", { d: path, "class": "anatele-svg__signal" }, s);
      samples.forEach(function (p) {
        svg("circle", { cx: X(p.t), cy: Y(p.v), r: 1.8, "class": "anatele-svg__sample" + (p.v > thr.value ? " is-above" : "") }, s);
      });
      clear(readout);
      var kind = res.state === "triggered" ? "warn" : (res.state === "unassessable" ? "muted" : "ok");
      readout.appendChild(el("p", {}, [
        "Rule state: ", badge(res.state, kind), " · longest run above " + thr.value + ": ",
        el("strong", { text: fmt(res.duration, 1) + " s" }), " (needs ≥ " + fmt(dur.value, 1) + " s) · " +
        res.runs.length + " run(s); qualifying runs are shaded darker."]));
      readout.appendChild(el("p", { "class": "anatele-widget__small", text:
        "A run lasts from its first to its last sample above the threshold, so a single sample counts as 0 s. " +
        (res.state === "unassessable" ? "The median sample interval (" + fmt(res.medianInterval, 2) +
          " s) is not shorter than the required duration, so a missing run is not evidence of health." : "") }));
    }
    update();
  }

  // ------------------------------------------------------------------ widget: hypothesis
  var RULE_TITLES = {
    "compass-event-before-crash-check": "Compass-selection event preceded disarm",
    "ekf-compass-innovation-pre-event": "EKF compass innovations exceeded gate",
    "compasses-inconsistent-reported": "Compasses reported inconsistent",
    "tracking-loss": "Sustained tilt tracking error",
    "motor-floor-saturation-pre-event": "Motor held at floor",
    "motor-output-imbalance-pre-event": "Motor output imbalance",
    "motor-ceiling-saturation": "Motor at ceiling",
    "vibration-persistent-pre-event": "Sustained vibration",
    "clipping-pre-event": "Accelerometer clipping",
    "power-monitoring-gap": "No usable battery voltage (coverage gap)"
  };
  function renderHypothesis(node) {
    var body = frame(node, "Hypothesis screening score",
      "Choose a hypothesis and set what each rule found. Support adds its weight to an evidence family, each family " +
      "is capped at 1.5, and contradictions subtract. The result is a screening order, not a probability.");
    var hypSelect = select(body, "Hypothesis", HYPOTHESES.map(function (h) { return [h.id, h.title]; }), "thrust-or-configuration", build);
    var rows = el("div", { "class": "anatele-hyp" });
    body.appendChild(rows);
    var readout = liveRegion(body);
    var states = {};

    function current() { return HYPOTHESES.filter(function (x) { return x.id === hypSelect.value; })[0]; }
    function addRow(h, rule) {
      var sup = h.support.filter(function (i) { return i.rule === rule; })[0];
      var con = h.contradict.filter(function (i) { return i.rule === rule; });
      var role = sup ? "supports family '" + sup.family + "' with weight " + sup.weight : "blocks this hypothesis when triggered";
      if (sup && sup.sourceScoped) {
        role += " (listed in the rule pack, but this rule is per-source rather than per-flight, so the current ranking does not count it)";
      }
      con.forEach(function (c) {
        role += c.state ? "; contradicts (−" + c.weight + ") when assessed as not triggered"
                        : "; contradicts (−" + c.weight + ") when it carries a contradiction";
      });
      var row = el("fieldset", { "class": "anatele-hyp__row" });
      row.appendChild(el("legend", { text: RULE_TITLES[rule] || rule }));
      row.appendChild(el("p", { "class": "anatele-widget__small" }, [el("code", { text: rule }), " " + role]));
      var state = { rule: rule, state: "unassessable", contradictions: [] };
      states[rule] = state;
      var flag = null;
      var sel = select(row, "Rule result", [["unassessable", "unassessable"], ["triggered", "triggered"],
        ["not_triggered", "not triggered"]], "unassessable", function () {
        state.state = sel.value;
        if (flag) {
          flag.disabled = sel.value !== "triggered";
          if (flag.disabled) { flag.checked = false; }
          state.contradictions = flag.checked ? ["tilt error came first"] : [];
        }
        update();
      });
      if (con.some(function (c) { return c.flag; })) {
        flag = checkbox(row, "Tilt error > 30° for ≥ 0.5 s came before the compass event (contradiction; needs 'triggered')", false, function () {
          state.contradictions = flag.checked ? ["tilt error came first"] : [];
          update();
        });
        flag.disabled = true;
      }
      rows.appendChild(row);
    }
    function build() {
      var h = current(), rules = [];
      clear(rows);
      states = {};
      h.support.concat(h.contradict).forEach(function (i) { if (rules.indexOf(i.rule) < 0) { rules.push(i.rule); } });
      (h.blockedBy || []).forEach(function (r) { if (rules.indexOf(r) < 0) { rules.push(r); } });
      rules.forEach(function (rule) { addRow(h, rule); });
      if (!h.support.length) {
        rows.appendChild(el("p", { "class": "anatele-widget__small",
          text: "This hypothesis has no supporting rules yet: it stays unassessable until battery data can be analysed." }));
      }
      update();
    }
    function update() {
      var h = current();
      var results = Object.keys(states).map(function (k) { return states[k]; });
      var r = scoreHypothesis(h, results);
      var terms = Object.keys(r.families).map(function (f) {
        return f + ": min(1.5, " + fmt(r.families[f], 2) + ") = " + fmt(Math.min(FAMILY_CAP, r.families[f]), 2);
      });
      var kind = { "strong support": "warn", "some support": "warn", "mixed support": "info", "weak support": "info" }[r.label] || "muted";
      clear(readout);
      readout.appendChild(el("p", {}, ["Score ", el("strong", { text: fmt(r.score, 2) }), " → ", badge(r.label, kind)]));
      readout.appendChild(el("p", { "class": "anatele-widget__small", text:
        (terms.length ? terms.join(" + ") : "No triggered support") + (r.penalty ? " − contradictions " + fmt(r.penalty, 2) : "") +
        ". Labels: unassessable if blocked or nothing assessed; mixed if both support and contradictions; " +
        "strong ≥ 2; some ≥ 1; weak > 0; otherwise limited or contradicting evidence." }));
    }
    build();
  }

  // ------------------------------------------------------------------ widget: waypoint
  function renderWaypoint(node) {
    var body = frame(node, "Waypoint distance and the 'reached' check",
      "Drag the waypoint (W) and the vehicle's nearest recorded position (V), or focus one with Tab and use the arrow keys " +
      "(Shift for bigger steps). AnaTele uses great-circle (haversine) distance and compares it with the acceptance radius plus GPS accuracy.");
    var controls = el("div", { "class": "anatele-widget__controls anatele-widget__controls--grid" });
    body.appendChild(controls);
    var span = 60, S = 300;
    var points = [{ key: "W", label: "Waypoint", n: 8, e: 10 }, { key: "V", label: "Vehicle nearest position", n: 10.5, e: 12 }];
    var spanSel = select(controls, "Map width", [["60", "60 m"], ["2000", "2 km"], ["200000", "200 km"]], "60", function () {
      var k = parseFloat(spanSel.value) / span;
      span = parseFloat(spanSel.value);
      points.forEach(function (p) { p.n *= k; p.e *= k; });
      update();
    });
    var radius = slider(controls, "WP_RADIUS_M (acceptance radius)", 0.5, 10, 0.5, DEFAULT_RADIUS_M, "m", update);
    var hacc = slider(controls, "GPS horizontal accuracy (h_acc)", 0, 5, 0.1, 1.0, "m", update);
    var haccUnknown = checkbox(controls, "GPS accuracy not recorded", false, update);
    var reported = checkbox(controls, "MISSION_ITEM_REACHED received", false, update);

    var s = svg("svg", { viewBox: "0 0 " + S + " " + S, "class": "anatele-svg anatele-svg--map", role: "group",
      "aria-label": "Local map centred on the ArduPilot SITL home location, north up" }, body);
    var readout = liveRegion(body);
    function toPx(p) { return [S / 2 + p.e / span * S, S / 2 - p.n / span * S]; }
    function latlon(p) { return toLatLon(p.n, p.e); }

    for (var i = 1; i < 6; i++) {
      svg("line", { x1: i * S / 6, x2: i * S / 6, y1: 0, y2: S, "class": "anatele-svg__grid" }, s);
      svg("line", { y1: i * S / 6, y2: i * S / 6, x1: 0, x2: S, "class": "anatele-svg__grid" }, s);
    }
    svg("text", { x: 6, y: 14, "class": "anatele-svg__label", text: "N ↑" }, s);
    var scaleText = svg("text", { x: 6, y: S - 6, "class": "anatele-svg__label" }, s);
    var marginCircle = svg("circle", { "class": "anatele-svg__margin" }, s);
    var radiusCircle = svg("circle", { "class": "anatele-svg__radius" }, s);
    var link = svg("line", { "class": "anatele-svg__link" }, s);
    function limit(p) { p.n = clamp(p.n, -span / 2, span / 2); p.e = clamp(p.e, -span / 2, span / 2); }
    var handles = points.map(function (p) {
      var g = svg("g", { tabindex: "0", role: "button", "class": "anatele-svg__handle anatele-svg__handle--" + p.key,
        "aria-label": p.label + ": drag, or use the arrow keys to move" }, s);
      p.circle = svg("circle", { r: 10 }, g);
      p.text = svg("text", { "text-anchor": "middle", dy: "4", text: p.key }, g);
      g.addEventListener("keydown", function (ev) {
        var step = span * (ev.shiftKey ? 0.05 : 0.01), moved = true;
        if (ev.key === "ArrowUp") { p.n += step; }
        else if (ev.key === "ArrowDown") { p.n -= step; }
        else if (ev.key === "ArrowRight") { p.e += step; }
        else if (ev.key === "ArrowLeft") { p.e -= step; }
        else { moved = false; }
        if (moved) { ev.preventDefault(); limit(p); update(); }
      });
      g.addEventListener("pointerdown", function (ev) {
        ev.preventDefault();
        if (g.focus) { g.focus(); }
        if (g.setPointerCapture) { g.setPointerCapture(ev.pointerId); }
        function move(e2) {
          var box = s.getBoundingClientRect();
          var x = (e2.clientX - box.left) / box.width * S, y = (e2.clientY - box.top) / box.height * S;
          p.e = (x - S / 2) / S * span; p.n = (S / 2 - y) / S * span;
          limit(p); update();
        }
        function up() {
          g.removeEventListener("pointermove", move);
          g.removeEventListener("pointerup", up);
          g.removeEventListener("pointercancel", up);
        }
        g.addEventListener("pointermove", move);
        g.addEventListener("pointerup", up);
        g.addEventListener("pointercancel", up);
      });
      return g;
    });

    function fmtDistance(m) { return m >= 1000 ? fmt(m / 1000, 3) + " km" : fmt(m, 2) + " m"; }
    function update() {
      var w = points[0], v = points[1], wl = latlon(w), vl = latlon(v);
      var hav = horizontalM(vl[0], vl[1], wl[0], wl[1]);
      var flat = flatM(vl[0], vl[1], wl[0], wl[1]);
      var acc = haccUnknown.checked ? null : hacc.value;
      hacc.input.disabled = haccUnknown.checked;
      var status = waypointStatus(hav, radius.value, acc, reported.checked, true);
      var wp = toPx(w), vp = toPx(v), pxPerM = S / span;
      setAttrs(radiusCircle, { cx: wp[0], cy: wp[1], r: radius.value * pxPerM });
      setAttrs(marginCircle, { cx: wp[0], cy: wp[1], r: (radius.value + (acc || 0)) * pxPerM });
      setAttrs(link, { x1: wp[0], y1: wp[1], x2: vp[0], y2: vp[1] });
      points.forEach(function (p, idx) {
        var px = toPx(p), ll = latlon(p);
        setAttrs(p.circle, { cx: px[0], cy: px[1] });
        setAttrs(p.text, { x: px[0], y: px[1] });
        handles[idx].setAttribute("aria-description", "latitude " + fmt(ll[0], 7) + ", longitude " + fmt(ll[1], 7));
      });
      scaleText.textContent = "grid " + (span / 6 >= 1000 ? fmt(span / 6000, 1) + " km" : fmt(span / 6, 0) + " m");
      var diff = flat - hav;
      var kind = status === "geometry_consistent_with_completion" || status === "reported_complete" ? "ok" : "warn";
      clear(readout);
      readout.appendChild(el("p", {}, ["Haversine distance ", el("strong", { text: fmtDistance(hav) }),
        " · flat-earth " + fmtDistance(flat) + " (difference " + (Math.abs(diff) >= 1 ? fmt(diff, 2) + " m" : fmt(diff * 1000, 2) + " mm") + ")"]));
      readout.appendChild(el("p", {}, ["Status: ", badge(status, kind), " · margin = " + fmt(radius.value, 1) + " m radius + " +
        (acc === null ? "0 (accuracy unknown)" : fmt(acc, 1) + " m h_acc") + " = " + fmt(radius.value + (acc || 0), 1) + " m"]));
      readout.appendChild(el("p", { "class": "anatele-widget__small", text:
        "W " + fmt(wl[0], 7) + ", " + fmt(wl[1], 7) + " · V " + fmt(vl[0], 7) + ", " + fmt(vl[1], 7) +
        ". Being close is not proof the waypoint was completed; 'not visited' also requires position coverage without gaps over 5 s " +
        "(assumed here)." }));
    }
    update();
  }

  // ------------------------------------------------------------------ bootstrap
  var WIDGETS = {
    "tilt-error": renderTilt, "mixer": renderMixer, "persistence": renderPersistence,
    "hypothesis": renderHypothesis, "waypoint": renderWaypoint
  };

  function renderAll() {
    var nodes = document.querySelectorAll(".anatele-widget[data-widget]");
    Array.prototype.forEach.call(nodes, function (node) {
      if (node.getAttribute("data-rendered") === "1") { return; }
      node.setAttribute("data-rendered", "1");
      var render = WIDGETS[node.getAttribute("data-widget")];
      clear(node);
      if (!render) {
        node.appendChild(el("p", { text: "Unknown widget: " + node.getAttribute("data-widget") }));
        return;
      }
      try { render(node); }
      catch (err) {
        clear(node);
        node.appendChild(el("p", { text: "This interactive widget could not be displayed." }));
        if (root.console) { root.console.error(err); }
      }
    });
  }
  api.renderAll = renderAll;

  if (root.document$ && typeof root.document$.subscribe === "function") {
    root.document$.subscribe(function () { renderAll(); });
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", renderAll);
  } else {
    renderAll();
  }
})(typeof window !== "undefined" ? window : globalThis);
