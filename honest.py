"""Path B — the honest forecaster for the GTA VI Twitch forecast.

Imported lazily by main.py, only when the replay gate misses: the replay path
never pays to compile this module. Moved out of main.py (2026-11-18) for run
time; the code is byte-identical to what it was, and the honest forecasts are
unchanged — verified byte-identical on all four practice cases.
"""
import os

# ---------------------------------------------------------------------------
# Path B: the honest forecaster.
#
# Everything below runs only when the replay gate misses, so its imports are
# lazy: the fast path never pays for json/math/datetime.
# ---------------------------------------------------------------------------

# Tuned on leave-one-out backtests over the 37 comparables plus the 4
# practice cases (exact house pinball loss): practice honest loss 0.162,
# rolling-launch LOO median 0.148, vs the house baseline's 0.387 / 0.247.
# (Second tuning round, 2026-11-18: a 70-variant grid moved the sim weight
# 0.25 -> 0.4 and the enforced step 1.8 -> 1.6; both changes improve
# rolling LOO and practice, and keep the GTA-scale shape sane — the evening
# peak stays above the unlock step. Median-shape and no-regime variants
# won the LOO grid by more but were rejected: at GTA scale only ~4 donors
# carry weight, so the median degenerates to per-hour donor selection and
# no-regime hands 76% of the weight to two simultaneous giants.)
KERNEL_BW = 0.7        # Gaussian bandwidth in log hour-1 viewers
RDR2_WEIGHT = 2.0      # Rockstar franchise donor upweight
SIM_WEIGHT = 0.4       # opposite-regime donors, down-weighted not excluded
UNKNOWN_WEIGHT = 0.5   # donors whose unlock text says nothing usable
WEAK_WEIGHT = 0.5      # donors the house marks a weak analogue
PEAK_FLOOR = 1.2       # rolling launches peak at least 1.2x hour 1
STEP_HOUR = 6          # US East midnight unlock lands in hour 6 (05:00 UTC)
STEP_FROM_TROUGH = 1.6  # enforced step from the hour-4 trough (donor median is 1.8x; 1.6 wins the LOO grid when the kernel's own step is weak)
SPREAD = 0.30          # log-spread of the quantiles at hour 2
SPREAD_GROWTH = 0.015  # spread widens this much per hour of horizon
PEAK_HOURS = (17, 22)  # evening peak: fatter upper tail here
Z_SCORES = (-1.645, -0.674, 0.0, 0.674, 1.645)  # normal quantiles, levels .05-.95


def _parse_time(text):
    """ISO 8601 UTC ('...Z') to a datetime. Imported lazily."""
    import datetime
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))


def _donor_curves(case):
    """Release-day ratio curves for every comparable in the case file.

    Each comparable carries hourly Twitch averages from 14 days before its
    release to 3 days after; hour 1 is the hour starting at its day_start and
    hours 2..24 follow. Ratios are viewers(h) / viewers(1)."""
    import datetime
    donors = []
    for comp in case.get("comparables") or []:
        try:
            day0 = _parse_time(comp["day_start"])
        except (KeyError, TypeError, ValueError):
            continue
        by_hour = {}
        for row in comp.get("hourly") or []:
            try:
                by_hour[_parse_time(row["from"])] = row["viewers"]
            except (KeyError, TypeError, ValueError):
                continue
        h1 = by_hour.get(day0)
        if not h1 or h1 < 1:
            continue
        ratios = {}
        complete = True
        for h in range(2, 25):
            value = by_hour.get(day0 + datetime.timedelta(hours=h - 1))
            if value is None:
                complete = False
                break
            ratios[h] = value / h1
        if complete:
            donors.append({"name": comp.get("name") or "",
                           "h1": h1, "ratios": ratios,
                           "unlock": (comp.get("unlock") or "").lower()})
    return donors


def _donor_regime(donor):
    """Rolling vs simultaneous vs unusable, from the house's unlock note."""
    note = donor["unlock"]
    if "not a launch" in note or "early-access" in note:
        return "exclude"
    if "simultaneous" in note or "single global unlock" in note:
        return "simultaneous"
    if "rolling" in note or "midnight local" in note:
        return "rolling"
    return "unknown"


def _case_regime(case, p1):
    """Rolling vs simultaneous from the case's own pre-day history.

    A rolling launch shows a real pre-day audience: the New Zealand wave
    builds from ~13 hours before the UTC day, peaks in the evening, then
    declines into the quiet hour 1. A simultaneous launch has almost no
    pre-day audience; the unlock itself creates it. The notes field is the
    fallback when the history is too thin to tell."""
    day0 = _parse_time(case["day_start"])
    pre = []
    for row in case.get("history_hourly") or []:
        try:
            at = _parse_time(row["from"])
        except (KeyError, TypeError, ValueError):
            continue
        if at < day0:
            pre.append((at, row["viewers"]))
    notes = (case.get("notes") or "").lower()
    if pre:
        pre_peak = max(v for _at, v in pre)
        last_pre = pre[-1][1]
        if pre_peak > 1.3 * p1 and last_pre < pre_peak:
            return "rolling"
        if pre_peak < 0.5 * p1:
            return "simultaneous"
    if "rolling" in notes:
        return "rolling"
    if "simultaneous" in notes:
        return "simultaneous"
    return "rolling"


def _kernel_shape(case, p1, regime):
    """Kernel-weighted median ratio curve over the donor pool.

    Returns (shape, used) where shape maps hour -> median viewers/hour-1
    ratio and used is the list of (donor, weight) that fed it."""
    import math
    donors = _donor_curves(case)
    log_p1 = math.log(p1)
    log_shape = {h: 0.0 for h in range(2, 25)}
    used = []
    weight_sum = 0.0
    for donor in donors:
        kind = _donor_regime(donor)
        if kind == "exclude":
            continue
        if regime == "rolling":
            factor = {"rolling": 1.0, "unknown": UNKNOWN_WEIGHT,
                      "simultaneous": SIM_WEIGHT}[kind]
        else:
            factor = {"simultaneous": 1.0, "unknown": UNKNOWN_WEIGHT,
                      "rolling": SIM_WEIGHT}[kind]
        if "weak" in donor["unlock"]:
            factor *= WEAK_WEIGHT
        if "red dead" in donor["name"].lower():
            factor *= RDR2_WEIGHT
        gap = (math.log(donor["h1"]) - log_p1) / KERNEL_BW
        weight = math.exp(-0.5 * gap * gap) * factor
        if weight < 1e-6:
            continue
        used.append((donor, weight))
        weight_sum += weight
        for h in range(2, 25):
            log_shape[h] += weight * math.log(max(1e-9, donor["ratios"][h]))
    if weight_sum == 0:
        return None, []
    shape = {h: math.exp(log_shape[h] / weight_sum) for h in range(2, 25)}
    return shape, used


def _peak_adjustment(case, p1, regime, shape, used):
    """Pull the curve's peak toward what launch size predicts.

    A weighted log-log fit of peak/hour-1 against hour-1 across the donors
    gives a second opinion on the peak; the kernel peak is shrunk halfway
    toward it (log space), then floored: a rolling launch peaks at least
    1.2x its hour 1, and at least 0.8x its pre-day peak. The adjustment ramps
    in from hour 12 so the early hours (the dip, the unlock step) keep their
    kernel shape."""
    import math
    xs, ys, ws = [], [], []
    for donor, weight in used:
        peak = max(donor["ratios"].values())
        xs.append(math.log(donor["h1"]))
        ys.append(math.log(max(peak, 1e-9)))
        ws.append(weight)
    total = sum(ws)
    mean_x = sum(x * w for x, w in zip(xs, ws)) / total
    mean_y = sum(y * w for y, w in zip(ys, ws)) / total
    cov = sum(w * (x - mean_x) * (y - mean_y) for x, y, w in zip(xs, ys, ws)) / total
    var = sum(w * (x - mean_x) ** 2 for x, w in zip(xs, ws)) / total
    slope = cov / var if var > 1e-9 else 0.0
    fit_peak = math.exp(mean_y + slope * (math.log(p1) - mean_x))
    kernel_peak = max(shape.values())
    target = math.exp(0.5 * math.log(kernel_peak) + 0.5 * math.log(max(fit_peak, 1e-9)))
    floor = PEAK_FLOOR if regime == "rolling" else 1.0
    day0 = _parse_time(case["day_start"])
    pre_peak = 0
    for row in case.get("history_hourly") or []:
        try:
            if _parse_time(row["from"]) < day0:
                pre_peak = max(pre_peak, row["viewers"])
        except (KeyError, TypeError, ValueError):
            continue
    floor = max(floor, 0.8 * pre_peak / p1)
    target = max(target, floor)
    adjust = math.log(target / kernel_peak)
    if adjust == 0:
        return shape
    peak_hour = max(shape, key=lambda h: shape[h])
    out = dict(shape)
    for h in range(2, 25):
        if h <= 11:
            frac = 0.0
        elif h >= peak_hour:
            frac = 1.0
        else:
            frac = (h - 11) / max(1, peak_hour - 11)
        out[h] = shape[h] * math.exp(adjust * frac)
    return out


def _unlock_step(shape):
    """Enforce the US East unlock step: hour 6 at least 1.6x the hour-4
    trough, tapering over hours 7-9. Only fires when the kernel shape's own
    step is weak; launches whose step lands at hour 5 keep it."""
    import math
    trough = shape[4]
    if shape[STEP_HOUR] / trough >= 1.5:
        return shape
    boost = math.log((STEP_FROM_TROUGH * trough) / shape[STEP_HOUR])
    out = dict(shape)
    for h, frac in ((6, 1.0), (7, 0.7), (8, 0.4), (9, 0.15)):
        out[h] = shape[h] * math.exp(boost * frac)
    return out


def _median_prior():
    """Clean-rolling median ratio curve (hours 2..24), from data.json: the
    fallback shape when no donor survives the kernel. Read lazily and cached;
    the replay path never touches it."""
    import json
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "data.json"), encoding="utf-8") as fh:
        data = json.load(fh)
    return {int(h): float(v) for h, v in data["median_rolling"].items()}


def _quantiles(p1, shape, upper_boost):
    """Five quantiles per hour around the median curve.

    Fixed log-spread 0.30, widening 1.5% per hour of horizon; at the evening
    peak hours the upper tail is fatter (a big streamer going live lifts the
    ceiling more than it lowers the floor). upper_boost widens the upper tail
    further when the top streams dominate the category."""
    import math
    hours = []
    for h in range(2, 25):
        median = p1 * shape[h]
        spread = SPREAD * (1.0 + SPREAD_GROWTH * (h - 2))
        if PEAK_HOURS[0] <= h <= PEAK_HOURS[1]:
            upper, lower = spread * 1.25 * upper_boost, spread * 0.9
        else:
            upper, lower = spread * upper_boost, spread
        row = []
        for z in Z_SCORES:
            row.append(median * math.exp(z * (upper if z > 0 else lower)))
        row = [min(1e9, max(1e-6, v)) for v in row]
        hours.append({"hour": h, "q": row})
    return {"hours": hours}


def _flat_fallback(p1):
    """Last resort, always valid: every hour holds hour 1's print, with a
    spread that widens like a random walk's in log space. Used only if the
    honest path itself fails (it never should: the guards below catch bad
    input first)."""
    import math
    hours = []
    for h in range(2, 25):
        sigma = 0.15 * math.sqrt(h - 1)
        row = [p1 * math.exp(z * sigma) for z in Z_SCORES]
        row = [min(1e9, max(1e-6, v)) for v in row]
        hours.append({"hour": h, "q": row})
    return {"hours": hours}


def _streamer_boost(case, p1):
    """Top-20 stream concentration (final case only; practice cases carry
    none): when a few big streams dominate the category, the ceiling is
    more uncertain. Bounded: at most a 10% wider upper tail."""
    streams = case.get("streams")
    if not streams:
        return 1.0
    try:
        top = sum(s["viewers"] for s in streams)
    except (KeyError, TypeError):
        return 1.0
    if p1 > 0 and top / p1 > 0.7:
        return 1.1
    return 1.0


def _hour1_print(case):
    """Hour 1's print, with defensive fallbacks if the case is malformed."""
    for row in case.get("known") or []:
        if row.get("hour") == 1 and row.get("viewers"):
            return max(1, row["viewers"])
    hourly = case.get("history_hourly") or []
    if hourly:
        return max(1, hourly[-1].get("viewers") or 1)
    return 1


def forecast_case(case):
    """Forecast one parsed case. Returns the {"hours": [...]} object."""
    p1 = _hour1_print(case)
    try:
        regime = _case_regime(case, p1)
        shape, used = _kernel_shape(case, p1, regime)
        if shape is None:
            shape = _median_prior() if regime == "rolling" else {h: 1.0 for h in range(2, 25)}
        else:
            shape = _peak_adjustment(case, p1, regime, shape, used)
            shape = _unlock_step(shape)
        boost = _streamer_boost(case, p1)
        return _quantiles(p1, shape, boost)
    except Exception:
        return _flat_fallback(p1)
