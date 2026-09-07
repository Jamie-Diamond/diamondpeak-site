/*
 * wetsuit-engine.js — Cervia IRONMAN water-temperature / wetsuit prediction engine.
 *
 * SINGLE SOURCE OF TRUTH for the wetsuit-predictor maths. Loaded two ways:
 *   • the browser (cycling/cervia-wetsuit.html) as window.WetsuitEngine
 *   • Node.js (ClaudeCoach bot, via js/wetsuit-cli.js) as require(...)
 * so the web predictor and the coach bot always give identical answers.
 *
 * Pure functions only — no DOM, no I/O, no clock reads (callers pass dates and
 * any fetched series in).
 *
 * ══ TWO-STAGE STRUCTURE (v2.0) ══════════════════════════════════════════
 * The model predicts the OFFICIAL IRONMAN READING in two explicit stages, so
 * a sea-surface forecast is never confused with what the referee's thermometer
 * will say at 6am:
 *
 *   STAGE 1 — SEA-SURFACE SST on race day, in Open-Meteo Marine
 *             `sea_surface_temperature_max` space (daily MAXIMUM). Seven
 *             methods are blended, then a cold-event (NE-wind) risk allowance
 *             is applied.
 *   STAGE 2 — OFFSET CHAIN to the official reading:
 *               a) diurnal:  6am is cooler than the daily max.
 *                            MEASURED −0.26 ± 0.10 °C (n = 90 September days,
 *                            hourly Open-Meteo archive 2023–25).
 *               b) reading:  the official 60cm coastal reading vs the offshore
 *                            model grid cell at 6am. MEASURED from race days
 *                            with archive coverage — see readingOffsetStats().
 *                            Small n and WIDE: this term dominates the total
 *                            uncertainty, and saying so is the point.
 *
 * Stage 1 methods (all in SST space):
 *   1. Historical race-day average (empirical, year-blind)
 *   2. Climatological normal + Mediterranean warming trend (empirical, year-blind)
 *   3. Seasonal exponential cooling curve from the August peak (model)
 *   4. August anomaly propagation into September (model)
 *   5. Live SST + climatological cooling to race day (observation, ≤30 days out)
 *   6. Physical ocean-model FORECAST SST + cooling bridge to race day
 *      (forecast, active whenever the series lands within M6_MAX_GAP_DAYS)
 *   7. Current-summer anomaly with seasonal persistence (>30 days out)
 *
 * Calibration layers:
 *   • LEAD-TIME DEFLATION of methods 1 and 2. Both are year-blind — they know
 *     nothing about how hot the sea actually is this year — so their weight
 *     decays as the race approaches and real observations/forecasts arrive.
 *     Methods 3 and 4 are NOT deflated: they already track the current year
 *     through its August SST.
 *   • COLD-EVENT (Bora) TAIL, active at EVERY lead time. The shallow north
 *     Adriatic can lose 4-6°C in days under NE wind — that is how 2024 went
 *     mandatory-wetsuit (27.8°C on 9 Sep → 21.9°C on race day). Beyond the
 *     wind forecast's skill horizon the risk is climatological; inside it the
 *     actual forecast raises or lowers it. Absence of a 12-day-out Bora signal
 *     is NOT evidence of absence.
 *   • LEAVE-ONE-OUT BACKTEST over the real historical years. The quoted
 *     reading uncertainty is floored at the measured LOO error, so the
 *     probability can never be more confident than the model's track record.
 *
 * ══ WHY THE MEAN NO LONGER DECAYS TOWARDS NORMAL ═══════════════════════
 * v1.x shrank a warm live anomaly by 5%/day (45% gone at 12 days out). The
 * archive contradicts that: the warm anomaly GREW through September in 2023
 * and 2025, and collapsed only in 2024 — when a Bora hit. The right shape is
 * therefore "persist the anomaly in the mean, carry the collapse risk in a
 * fat LEFT tail", not "shrink the mean and keep the spread symmetric".
 * Consequently methods 5/6 propagate at BASE_COOLING_RATE, the cooling rate
 * measured in NON-collapse years (−0.10°C/day in 2023 and 2025). The 30-year
 * normal's implied −0.155°C/day averages collapse years in, and using it
 * alongside an explicit cold-event term would double-count the same risk.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api; // Node
  if (root) root.WetsuitEngine = api;                                        // browser
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  /* ── Historical race data ─────────────────────────────────────────────
   * waterTemp  = official IRONMAN race-morning reading (swim start ~6am, 60cm depth)
   * onlineSST  = third-party satellite/coastal SST for the same date
   *              (seatemperature.info / .net — a DIFFERENT instrument to the
   *              live feed; kept for provenance, NOT used to correct forecasts)
   * omSSTmax   = Open-Meteo Marine `sea_surface_temperature_max` on race day
   * omSST6am   = Open-Meteo Marine hourly SST at 06:00 local on race day
   *              — THE reference for the reading offset, because the live and
   *              forecast feeds are the same instrument. Archive starts 2023.
   * augSST     = that year's August mean SST (Open-Meteo marine archive)
   * estimated  = no official reading found; waterTemp inferred — excluded from
   *              offset statistics AND from the backtest.
   * censoredUpper = the official reading is known only as an UPPER BOUND
   *              (the swim was wetsuit-legal, so ≤ 24.5). Included in the
   *              reading-offset estimate at its bound, which is deliberately
   *              conservative-warm: the true value can only be lower.
   *
   * NOTE on 2023: the three sources for that race day span 3.2°C — official
   * 22.0, seatemperature.info 23.5, Open-Meteo 25.2 (25.1 at 6am). That single
   * disagreement is load-bearing in the reading-offset estimate and is the main
   * reason its sd is so wide. Provenance was swept in v1.3 and is unresolved.  */
  var RACE_HISTORY = [
    { year: 2017, date: 'Sep 23', dayOfSept: 23, waterTemp: 22.0, onlineSST: 23.0, omSSTmax: null, omSST6am: null, augSST: null, wetsuit: 'Yes (AG)', source: 'Estimated from satellite SST', onlineSource: 'seatemperature.net (2022–25 archive)', estimated: true },
    { year: 2018, date: 'Sep 22', dayOfSept: 22, waterTemp: 24.8, onlineSST: 25.5, omSSTmax: null, omSST6am: null, augSST: null, wetsuit: 'No (too warm)', source: 'Race reports — European heatwave year', onlineSource: 'seatemperature.info satellite' },
    { year: 2019, date: 'Sep 21', dayOfSept: 21, waterTemp: 24.6, onlineSST: 25.2, omSSTmax: null, omSST6am: null, augSST: null, wetsuit: 'Yes (jellyfish exception)', source: 'Race reports — above limit, exception granted', onlineSource: 'seatemperature.info satellite' },
    { year: 2021, date: 'Sep 18', dayOfSept: 18, waterTemp: 24.0, onlineSST: 24.5, omSSTmax: null, omSST6am: null, augSST: null, wetsuit: 'Yes (AG)', source: 'Satellite SST data', onlineSource: 'seatemperature.info satellite' },
    { year: 2022, date: 'Sep 18', dayOfSept: 18, waterTemp: 21.0, onlineSST: null, omSSTmax: null, omSST6am: null, augSST: null, wetsuit: 'Yes (AG — wetsuit swim)', source: 'Athlete-reported ~21°C (research 6 Jul 2026). Race storm-delayed 17→18 Sep; the previous 25.0 was satellite for the PRE-storm window — a wind cold-drop like 2024', onlineSource: 'no same-day satellite (archive starts 2023)', reported: true },
    { year: 2023, date: 'Sep 16', dayOfSept: 16, waterTemp: 22.0, onlineSST: 23.5, omSSTmax: 25.2, omSST6am: 25.1, augSST: 27.77, wetsuit: 'Yes (AG)', source: 'Race reports confirmed', onlineSource: 'seatemperature.info 23.5; Open-Meteo archive 25.2 max / 25.1 at 06:00' },
    { year: 2024, date: 'Sep 21', dayOfSept: 21, waterTemp: 21.5, onlineSST: 21.5, omSSTmax: 21.9, omSST6am: 21.5, augSST: 29.75, wetsuit: 'Yes (mandatory)', source: 'Race reports — mandatory wetsuits (Bora-type cold drop)', onlineSource: 'seatemperature.info 21.5; Open-Meteo archive 21.9 max / 21.5 at 06:00' },
    { year: 2025, date: 'Sep 20', dayOfSept: 20, waterTemp: 24.5, onlineSST: 25.2, omSSTmax: 25.2, omSST6am: 24.9, augSST: 27.44, wetsuit: 'Yes (AG)', source: 'Athlete raced it — wetsuit-legal, so official reading ≤24.5; exact value unknown', onlineSource: 'Open-Meteo archive 25.2 max / 24.9 at 06:00', estimated: true, censoredUpper: true }
  ];

  var CONSTANTS = {
    // DATA_UPDATED: bump only when the race history, climatology or model
    // constants actually change. DATA_REVIEWED: bump on a re-verification pass
    // even when nothing moved, so "updated" never falsely implies new data.
    DATA_UPDATED: '2026-09-07',
    DATA_UPDATED_LABEL: '7 September 2026',
    DATA_REVIEWED: '2026-09-07',
    DATA_REVIEWED_LABEL: '7 September 2026',
    SEPT_CLIM_START: 26.0,       // Sept 1 climatological normal (1991-2020)
    SEPT_CLIM_END: 21.5,         // Sept 30 normal
    AUG_NORMAL: 25.7,            // 30-year August average
    TREND_RATE: 0.04,            // Mediterranean warming °C/year
    TREND_BASE_YEAR: 2010,
    T_WINTER: 14.0,              // cooling-curve winter asymptote
    LAMBDA: 0.0085,              // cooling-curve decay constant
    PERSISTENCE: 0.65,           // Aug→Sept anomaly persistence factor

    /* ── Stage 1: propagating an observation/forecast to race day ──────
     * BASE_COOLING_RATE is the September cooling rate measured in NON-collapse
     * years — OLS on the Open-Meteo daily-max archive gives −0.096°C/day for
     * 2023 and −0.095°C/day for 2025. (2024 gives −0.33°C/day, but that whole
     * month is one Bora collapse, which the cold-event term below models
     * separately.) The 30-year normal's implied −0.155°C/day blends collapse
     * years in; using it here as well would double-count the collapse risk. */
    BASE_COOLING_RATE: 0.12,     // °C/day — conservative middle of 0.095–0.20
    M6_MAX_GAP_DAYS: 7,          // how far a forecast may be bridged to race day
    M5_MAX_LEAD_DAYS: 30,        // live-observation method active window

    /* ── Stage 1: lead-time deflation of the year-blind methods ────────
     * Methods 1 and 2 contain no information about the current year's sea
     * state. Weeks out that is all there is, so they keep full weight; as
     * observations and forecasts arrive their share must fall away. */
    CLIM_DEFLATE_MIN: 0.15,      // m1/m2 residual weight fraction on race day
    AUG_DEFLATE_MIN: 0.35,       // m3/m4 floor — they at least know this August
    CLIM_DEFLATE_FULL_DAYS: 28,  // lead at which full weight is restored

    /* ── Stage 1: cold-event (Bora / NE-wind) risk ─────────────────────
     * Active at EVERY lead time, not just inside the wind forecast window.
     * COLD_EVENT_DROP: the size of a wind-driven collapse — 2024 fell 27.8°C
     *   (9 Sep) to 21.9°C (21 Sep); 2022's race was storm-delayed to an
     *   athlete-reported 21°C from a pre-storm 25.0. ~4-6°C; 4.0 taken.
     * COLD_EVENT_RATE: onset probability per day of exposure. Two collapses
     *   in roughly five recent Septembers with ~20 exposed days each
     *   → 2/(5×20) = 0.02/day. Judgement from a tiny n, flagged as such.
     * Beyond WIND_SKILL_DAYS the rate is climatological. Inside it the actual
     * wind forecast substitutes WIND_BORA_P (NE days present) or WIND_CLEAR_P
     * (a skilful forecast showing none). */
    COLD_EVENT_DROP: 4.0,        // °C
    COLD_EVENT_RATE: 0.02,       // per day of exposure
    COLD_EVENT_MAX_WINDOW: 21,   // days of exposure counted
    WIND_SKILL_DAYS: 7,          // wind forecast is informative within this lead

    /* ── Cold-wind detector, RECALIBRATED against the archive ──────────
     * v1.x looked for the NE (Bora) sector 10-100° at ≥30 km/h within 5 days
     * of the race. Checked against the 2023-25 archive wind that detector
     * NEVER FIRES — not even in 2024, the mandatory-wetsuit collapse it was
     * written for. Two reasons:
     *   • the 2024 collapse was driven by NW wind (300-311°) on 13-15 Sep,
     *     outside the NE sector entirely;
     *   • no September day in 2023-25 reached 30 km/h (the monthly max is
     *     30.9 km/h), so the speed floor was above the whole distribution.
     * The physics is upwelling/mixing in a shallow shelf sea, which any
     * strong wind from the northerly half drives — Bora (NE) and Tramontana
     * /Maestrale (NW) alike. Sector widened to wrap 270°→90° and the floor
     * dropped to 25 km/h, which separates the archive cleanly:
     *     14 days before race, northerly ≥25 km/h → 2024: 4 days (collapsed)
     *                                               2023: 1 day  (no collapse)
     *                                               2025: 0 days (no collapse)
     * n=3, so this is JUDGEMENT, not a fit — but a detector that fires on the
     * one collapse in the record beats one that never fires at all. */
    WIND_DIR_FROM: 270,          // sector start (degrees, wraps through 0)
    WIND_DIR_TO: 90,             // sector end
    WIND_SPEED_KMH: 25,          // daily max ≥ this counts as a cold-wind day
    WIND_SCAN_DAYS: 14,          // days before race scanned
    WIND_P_PER_DAY: 0.20,        // P(collapse) added per cold-wind day
    WIND_BORA_P: 0.65,           // cap — P(collapse) with a strong wind signal
    WIND_CLEAR_P: 0.05,          // floor — P(collapse) with a skilful clear forecast

    /* ── Stage 2: offset chain from model SST to the official reading ──
     * DIURNAL_6AM is MEASURED, not assumed: mean(06:00 hourly SST − daily max)
     * over all 90 September days in the 2023-25 Open-Meteo hourly archive.
     * It is small and tight — the reading offset below is where the real
     * uncertainty lives. */
    DIURNAL_6AM: -0.26,
    DIURNAL_6AM_SD: 0.10,
    READING_OFFSET_FALLBACK_SD: 1.8,  // if fewer than 2 usable race days
    /* Small-sample floor on the reading offset's sd. Three race days cannot
       pin a coastal-vs-offshore offset to better than about a degree, and the
       sample sd is itself unstable at n=3 — leave-one-out on 2023 drops it to
       0.28°C, which would make the model MOST confident on the very year it
       cannot predict. Applied while n < READING_OFFSET_MIN_N. */
    READING_OFFSET_MIN_SD: 1.2,
    READING_OFFSET_MIN_N: 5,

    /* Seasonal (marine-heatwave scale) anomaly persistence for Method 7.
     * E-folding ~65 days is typical for Mediterranean summer SST anomalies
     * (standard oceanography, not fitted — the archive is too short). */
    SEASONAL_EFOLD_DAYS: 65,

    PRO_THRESHOLD: 21.9,         // wetsuit limits (°C, official reading)
    AG_THRESHOLD: 24.5,
    DEFAULT_AUG_SST: 28.3,       // API-failure fallback: 2023-25 August archive mean
    // Known race days-of-September by year; fallback 20 for unknown years
    RACE_DATES: { 2017: 23, 2018: 22, 2019: 21, 2021: 18, 2022: 18, 2023: 16, 2024: 21, 2025: 20, 2026: 19 },
    LOCATION: { lat: 44.26, lon: 12.35, name: 'Cervia, Adriatic' }
  };
  CONSTANTS.SEPT_COOLING_RATE = (CONSTANTS.SEPT_CLIM_START - CONSTANTS.SEPT_CLIM_END) / 29;

  /* Abramowitz-Stegun normal CDF approximation */
  function normalCDF(z) {
    var a1 = 0.254829592, a2 = -0.284496736, a3 = 1.421413741,
        a4 = -1.453152027, a5 = 1.061405429, p = 0.3275911;
    var sign = z < 0 ? -1 : 1;
    var t = 1 / (1 + p * Math.abs(z));
    var y = 1 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * Math.exp(-z * z / 2);
    return 0.5 * (1 + sign * y);
  }

  /* Split-normal CDF: sdLo applies below the mean, sdHi above. Continuous at
     the mean; integrates to 1. Used for the cold-event left tail. */
  function splitNormalCDF(t, mean, sdLo, sdHi) {
    var A = 2 * sdLo / (sdLo + sdHi), B = 2 * sdHi / (sdLo + sdHi);
    if (t < mean) return A * normalCDF((t - mean) / sdLo);
    return A / 2 + B * (normalCDF((t - mean) / sdHi) - 0.5);
  }

  /* Next upcoming race edition for a given date ('YYYY-MM-DD' or Date). */
  function defaultRace(today) {
    // Duck-typed, not `instanceof Date`: instanceof fails across realms (an
    // iframe, a vm context, a structured clone), which silently produced a NaN
    // year and a fallback race date instead of an error.
    var isDate = today && typeof today.getUTCFullYear === 'function' &&
                 !isNaN(today.getTime());
    var d = isDate ? today : new Date(String(today).slice(0, 10) + 'T00:00:00Z');
    if (isNaN(d.getTime())) throw new Error('defaultRace: unparseable date ' + today);
    var year = d.getUTCMonth() > 8 ? d.getUTCFullYear() + 1 : d.getUTCFullYear();
    return { year: year, dayOfSept: CONSTANTS.RACE_DATES[year] || 20 };
  }

  function _mean(a) { var s = 0, i; for (i = 0; i < a.length; i++) s += a[i]; return s / a.length; }
  function _sd(a) {
    var m = _mean(a), s = 0, i;
    for (i = 0; i < a.length; i++) s += (a[i] - m) * (a[i] - m);
    return Math.sqrt(s / (a.length - 1));
  }
  function _utcFromISO(iso) { return new Date(String(iso).slice(0, 10) + 'T00:00:00Z'); }

  /*
   * readingOffsetStats() — STAGE 2b. The offset from Open-Meteo 06:00 SST to
   * the official IRONMAN reading, measured on race days where both exist.
   *
   * Crucially this is measured against the SAME instrument the live and
   * forecast feeds use. v1.x measured it against seatemperature.info instead
   * and then applied it to Open-Meteo inputs — the two sources differ by
   * 1.7°C on 2023 race day, so that correction was simply the wrong offset
   * for the numbers it was being applied to.
   *
   * excludeYear lets the backtest hold a year out.
   */
  function readingOffsetStats(excludeYear) {
    var C = CONSTANTS;
    var rows = RACE_HISTORY.filter(function (r) {
      return r.year !== excludeYear && r.omSST6am != null && r.waterTemp != null &&
             (!r.estimated || r.censoredUpper);
    });
    var offs = rows.map(function (r) { return r.waterTemp - r.omSST6am; });
    if (!offs.length) return { avg: -0.4, mean: -0.4, median: -0.4, sd: C.READING_OFFSET_FALLBACK_SD, n: 0, rows: [] };
    // CENTRAL ESTIMATE = MEDIAN, not mean. With n=3 and one point (2023) whose
    // three independent sources disagree by 3.2 C, the mean is hostage to that
    // single disputed value (it pulls the offset to -1.17 vs a median of
    // -0.40). The median is the robust choice and, reassuringly, it puts the
    // TOTAL Stage-2 offset at -0.66 C -- the same figure independently derived
    // from the seatemperature.info series over 2018-24. Two different
    // instruments agreeing is a real cross-check.
    // The full spread stays in the sd: the disagreement is genuine uncertainty
    // about the offset, not a reason to shift the centre.
    var sorted = offs.slice().sort(function (a, b) { return a - b; });
    var mid = Math.floor(sorted.length / 2);
    var median = sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
    return {
      avg: median,
      mean: _mean(offs),
      median: median,
      sd: Math.max(
        offs.length > 1 ? _sd(offs) : C.READING_OFFSET_FALLBACK_SD,
        offs.length < C.READING_OFFSET_MIN_N ? C.READING_OFFSET_MIN_SD : 0),
      sdRaw: offs.length > 1 ? _sd(offs) : null,
      n: offs.length,
      rows: rows.map(function (r, i) {
        return { year: r.year, om6am: r.omSST6am, reading: r.waterTemp,
                 offset: Math.round(offs[i] * 100) / 100, censored: !!r.censoredUpper };
      })
    };
  }

  /* Total STAGE 2 offset (diurnal + reading) and its combined sd. */
  function _offsetChain(excludeYear) {
    var C = CONSTANTS;
    var ro = readingOffsetStats(excludeYear);
    var total = C.DIURNAL_6AM + ro.avg;
    var sd = Math.sqrt(C.DIURNAL_6AM_SD * C.DIURNAL_6AM_SD + ro.sd * ro.sd);
    return {
      diurnal: C.DIURNAL_6AM, diurnalSd: C.DIURNAL_6AM_SD,
      reading: ro.avg, readingMean: ro.mean, readingSd: ro.sd,
      readingN: ro.n, readingRows: ro.rows,
      total: total, sd: sd
    };
  }

  /*
   * Lead-time deflation factor for the year-blind methods 1 and 2.
   * 1.0 at CLIM_DEFLATE_FULL_DAYS or more; CLIM_DEFLATE_MIN on race day.
   */
  function climDeflation(leadDays, floor) {
    var C = CONSTANTS;
    var min = floor == null ? C.CLIM_DEFLATE_MIN : floor;
    if (leadDays == null || leadDays < 0) return 1.0;
    var f = Math.min(1, leadDays / C.CLIM_DEFLATE_FULL_DAYS);
    return min + (1 - min) * f;
  }

  /* Core methods 1-4 for a given race (year, dayOfSept), in SST space.
     m1 is the mean of official READINGS, so it is transformed into SST space
     by removing the offset chain — otherwise the chain gets applied to it
     twice (a v1.x bug: m1 was already a reading and still got bias-corrected).
     augustTemp: that year's August mean SST (or the normal). */
  function _coreMethods(raceYear, raceDayOfSept, augustTemp, histRows, offsetTotal) {
    var C = CONSTANTS;
    var histTemps = histRows.map(function (r) { return r.waterTemp; });
    var m1 = { meanReading: _mean(histTemps), sd: histTemps.length > 1 ? _sd(histTemps) : 1.5 };
    // Inverse Stage-2 transform: reading space → SST space
    m1.mean = m1.meanReading - offsetTotal;
    m1.lo = m1.mean - m1.sd; m1.hi = m1.mean + m1.sd;

    var m2 = {};
    m2.baseline = C.SEPT_CLIM_START - C.SEPT_COOLING_RATE * (raceDayOfSept - 1);
    m2.trend = C.TREND_RATE * (raceYear - C.TREND_BASE_YEAR);
    m2.temp = m2.baseline + m2.trend;
    m2.sd = 1.5;
    m2.lo = m2.temp - m2.sd; m2.hi = m2.temp + m2.sd;

    var m3 = { daysSincePeak: raceDayOfSept + 16 };
    m3.temp = C.T_WINTER + (augustTemp - C.T_WINTER) * Math.exp(-C.LAMBDA * m3.daysSincePeak);
    m3.sd = 1.2;
    m3.lo = m3.temp - m3.sd; m3.hi = m3.temp + m3.sd;

    var m4 = { anomaly: augustTemp - C.AUG_NORMAL };
    m4.propagated = C.PERSISTENCE * m4.anomaly;
    m4.base = m2.baseline;
    m4.temp = m4.base + m2.trend + m4.propagated;
    m4.sd = 1.1;
    m4.lo = m4.temp - m4.sd; m4.hi = m4.temp + m4.sd;

    return { m1: m1, m2: m2, m3: m3, m4: m4, histTemps: histTemps };
  }

  /* Inverse-variance weighted blend, in SST space. No offset applied here —
     Stage 2 is a separate, explicit step. */
  function _blendSST(methods) {
    var totalWeight = 0, weightedTemp = 0, invVar = 0, i;
    for (i = 0; i < methods.length; i++) {
      totalWeight += methods[i].weight;
      weightedTemp += methods[i].temp * methods[i].weight;
      invVar += methods[i].weight / (methods[i].sd * methods[i].sd);
    }
    return { temp: weightedTemp / totalWeight, sd: Math.sqrt(1 / invVar) };
  }

  var _HAND_WEIGHTS = { m1: 1.0, m2: 1.2, m3: 1.3, m4: 1.4 };

  /*
   * coldEventRisk() — probability that a wind-driven cold collapse hits
   * between now and race day, and the resulting mean shift / left-tail width.
   *
   * Split into two windows:
   *   • BLIND window (lead beyond WIND_SKILL_DAYS): no usable wind forecast,
   *     so the climatological onset rate applies. This is what v1.x missed —
   *     it only ever looked 5 days out, so 12 days from the race it reported
   *     "no significant NE wind" and applied ZERO cold risk, treating absence
   *     of information as evidence of absence.
   *   • SKILFUL window (lead ≤ WIND_SKILL_DAYS): the actual forecast decides.
   */
  function coldEventRisk(leadDays, boraDayCount, windForecastAvailable) {
    var C = CONSTANTS;
    var exposure = Math.max(0, Math.min(leadDays == null ? C.COLD_EVENT_MAX_WINDOW : leadDays,
                                        C.COLD_EVENT_MAX_WINDOW));
    var blindDays = Math.max(0, exposure - C.WIND_SKILL_DAYS);
    var skilfulDays = exposure - blindDays;

    var pBlind = 1 - Math.exp(-C.COLD_EVENT_RATE * blindDays);
    var pSkilful;
    if (!windForecastAvailable) {
      // No wind data at all — fall back to climatology across the whole window
      pSkilful = 1 - Math.exp(-C.COLD_EVENT_RATE * skilfulDays);
    } else if (skilfulDays <= 0) {
      pSkilful = 0;
    } else {
      // Graded, not binary: each forecast cold-wind day adds risk, floored at
      // WIND_CLEAR_P (a skilful clear forecast is good news but never a
      // guarantee) and capped at WIND_BORA_P.
      pSkilful = Math.min(C.WIND_BORA_P, C.WIND_CLEAR_P + C.WIND_P_PER_DAY * boraDayCount);
    }
    var p = 1 - (1 - pBlind) * (1 - pSkilful);

    return {
      p: p, pBlind: pBlind, pSkilful: pSkilful,
      blindDays: blindDays, skilfulDays: skilfulDays,
      // Expected loss shifts the mean; the residual variance fattens the LEFT
      // tail only — a collapse can only make the sea colder.
      shift: -C.COLD_EVENT_DROP * p,
      extraColdSd: C.COLD_EVENT_DROP * Math.sqrt(p * (1 - p))
    };
  }

  /*
   * backtest() — leave-one-out over the REAL (non-estimated) historical years,
   * using ONLY methods 1-4: no live or forecast series exists for past races,
   * so this measures the model's skill in its weakest configuration, weeks out.
   * For each year: rebuild the reading-offset chain and the m1 mean from the
   * OTHER years, run methods 1-4 with that year's August SST (or the normal),
   * blend in SST space, apply the offset chain, and score the predicted
   * READING against the official one.
   *
   * Because it is scored in reading space, its RMSE already contains the
   * Stage-2 offset error — so the final uncertainty is floored at this RMSE
   * rather than having the offset variance added on top of it.
   */
  function backtest() {
    var C = CONSTANTS;
    var real = RACE_HISTORY.filter(function (r) { return !r.estimated; });
    var rows = [], errs = [], calls = 0;
    for (var i = 0; i < real.length; i++) {
      var y = real[i];
      var chain = _offsetChain(y.year);
      var histRows = RACE_HISTORY.filter(function (r) { return r.year !== y.year; });
      var core = _coreMethods(y.year, y.dayOfSept,
                              y.augSST != null ? y.augSST : C.AUG_NORMAL,
                              histRows, chain.total);
      // Weeks-out configuration: no deflation, no live/forecast methods.
      var methods = [
        { key: 'm1', temp: core.m1.mean, sd: core.m1.sd, weight: _HAND_WEIGHTS.m1 },
        { key: 'm2', temp: core.m2.temp, sd: core.m2.sd, weight: _HAND_WEIGHTS.m2 },
        { key: 'm3', temp: core.m3.temp, sd: core.m3.sd, weight: _HAND_WEIGHTS.m3 },
        { key: 'm4', temp: core.m4.temp, sd: core.m4.sd, weight: _HAND_WEIGHTS.m4 }
      ];
      var sst = _blendSST(methods);
      var predReading = sst.temp + chain.total;
      var err = predReading - y.waterTemp;
      var callPred = predReading <= C.AG_THRESHOLD, callActual = y.waterTemp <= C.AG_THRESHOLD;
      if (callPred === callActual) calls++;
      errs.push(err);
      rows.push({ year: y.year, predicted: Math.round(predReading * 10) / 10,
                  predictedSST: Math.round(sst.temp * 10) / 10,
                  actual: y.waterTemp, error: Math.round(err * 100) / 100,
                  augInput: y.augSST != null ? 'archive' : 'normal',
                  offsetUsed: Math.round(chain.total * 100) / 100,
                  callCorrect: callPred === callActual });
    }
    var mae = _mean(errs.map(Math.abs));
    var rmse = Math.sqrt(_mean(errs.map(function (e) { return e * e; })));
    return { rows: rows, n: rows.length, mae: mae, rmse: rmse,
             bias: _mean(errs), callAccuracy: calls / rows.length };
  }

  /*
   * predictWater(params) — the whole model.
   *   raceYear, raceDayOfSept   race edition (required)
   *   augustAvgSST              this year's August average SST (optional)
   *   liveTemp, liveDateISO     most recent SST observation (optional; Method 5
   *                             activates 0-30 days before the race)
   *   forecastSeries            [{date:'YYYY-MM-DD', temp}] physical-model SST
   *                             forecast (optional; Method 6 activates when the
   *                             series reaches within M6_MAX_GAP_DAYS of race
   *                             day and bridges the remainder at the measured
   *                             non-collapse cooling rate)
   *   windDays                  [{date:'YYYY-MM-DD', speedKmh, dirDeg}] daily
   *                             max wind forecast (optional)
   *   todayISO                  reference date for lead-time logic; defaults to
   *                             the live observation date, else the race date
   */
  function predictWater(params) {
    var C = CONSTANTS;
    var raceYear = params.raceYear;
    var raceDayOfSept = params.raceDayOfSept;
    if (!raceYear || !raceDayOfSept) throw new Error('raceYear and raceDayOfSept are required');
    var augustTemp = params.augustAvgSST != null ? params.augustAvgSST : C.DEFAULT_AUG_SST;
    var raceDate = new Date(Date.UTC(raceYear, 8, raceDayOfSept));
    var raceISO = raceDate.toISOString().slice(0, 10);

    /* Reference "today" — drives lead time, deflation and cold-event exposure */
    var todayISO = params.todayISO ||
      (params.liveDateISO ? String(params.liveDateISO).slice(0, 10) : null);
    var leadDays = todayISO
      ? Math.round((raceDate - _utcFromISO(todayISO)) / 86400000)
      : null;

    /* ═══ STAGE 2 offset chain (needed up front: m1 is transformed by it) ═══ */
    var chain = _offsetChain(null);

    /* Legacy provenance figure — the seatemperature.info offset. Reported for
       transparency only; it is NOT applied to the Open-Meteo-sourced methods. */
    var biasRows = RACE_HISTORY.filter(function (r) { return !r.estimated && r.onlineSST != null; });
    var biases = biasRows.map(function (r) { return r.waterTemp - r.onlineSST; });
    var legacyBias = { avg: _mean(biases), sd: _sd(biases), n: biases.length };

    /* ═══ STAGE 1 — methods, all in Open-Meteo daily-max SST space ═══ */
    var core = _coreMethods(raceYear, raceDayOfSept, augustTemp, RACE_HISTORY, chain.total);
    var m1 = core.m1, m2 = core.m2, m3 = core.m3, m4 = core.m4;

    /* Method 5 — live observation + non-collapse cooling to race day.
       No anomaly decay: the archive says a warm anomaly persists in the mean
       (it grew in 2023 and 2025) and collapses only under wind, which the
       cold-event term now carries separately. */
    var m5 = null;
    if (params.liveTemp != null && params.liveDateISO) {
      var obsDate = _utcFromISO(params.liveDateISO);
      var daysUntilRace = Math.round((raceDate - obsDate) / 86400000);
      if (daysUntilRace >= 0 && daysUntilRace <= C.M5_MAX_LEAD_DAYS) {
        var obsDayOfSept = obsDate.getUTCMonth() === 8 ? obsDate.getUTCDate() : 1;
        m5 = { daysUntilRace: daysUntilRace, obsDayOfSept: obsDayOfSept, obsTemp: params.liveTemp };
        m5.cooling = -C.BASE_COOLING_RATE * daysUntilRace;
        m5.temp = params.liveTemp + m5.cooling;
        // Reference anomaly, reported for context only — not used in the mean
        m5.obsClim = C.SEPT_CLIM_START - C.SEPT_COOLING_RATE * (obsDayOfSept - 1) + m2.trend;
        m5.liveAnomaly = params.liveTemp - m5.obsClim;
        m5.sd = 0.4 + 0.09 * daysUntilRace;
        m5.lo = m5.temp - m5.sd; m5.hi = m5.temp + m5.sd;
        m5.confidence = daysUntilRace <= 3 ? 'HIGH' : daysUntilRace <= 7 ? 'MEDIUM' :
                        daysUntilRace <= 14 ? 'LOW' : 'VERY LOW';
        m5.weight = daysUntilRace <= 3 ? 3.0 : daysUntilRace <= 7 ? 2.5 :
                    daysUntilRace <= 14 ? 2.0 : 1.2;
      }
    }

    /* Method 6 — physical ocean-model forecast SST, bridged to race day.
       The strongest signal available: an assimilated forecast beats any
       statistical rule. v1.x threw the ENTIRE series away unless it landed
       within 2 days of the race, so at 12 days out a perfectly good 10-day
       forecast went unused. It is now bridged from the last forecast day to
       race day at BASE_COOLING_RATE, with uncertainty and weight decaying
       across the gap. */
    var m6 = null;
    if (params.forecastSeries && params.forecastSeries.length) {
      var usable = params.forecastSeries.filter(function (f) {
        return f.temp != null && f.date <= raceISO;
      });
      if (usable.length) {
        var exact = usable.filter(function (f) { return f.date === raceISO; })[0];
        var pick = exact || usable[usable.length - 1];
        var gapDays = Math.round((raceDate - _utcFromISO(pick.date)) / 86400000);
        if (gapDays <= C.M6_MAX_GAP_DAYS) {
          m6 = { forecastDate: pick.date, forecastTemp: pick.temp, gapDays: gapDays,
                 horizonDays: usable.length };
          m6.bridge = -C.BASE_COOLING_RATE * gapDays;
          m6.temp = pick.temp + m6.bridge;
          m6.sd = 0.4 + 0.12 * gapDays;
          m6.lo = m6.temp - m6.sd; m6.hi = m6.temp + m6.sd;
          // Decays smoothly with the bridge length instead of falling off a cliff
          m6.weight = 4.0 * Math.exp(-gapDays / 4);
        }
      }
    }

    /* Methods 5 and 6 come from the SAME ocean model, so when the forecast is
       available the live observation is largely redundant — down-weight it
       rather than counting the same signal twice. */
    if (m5 && m6) { m5.weight *= 0.3; m5.redundantWithM6 = true; }

    /* Method 7 — CURRENT-summer anomaly with seasonal persistence. The sea's
       temperature TODAY vs the same dates in recent years carries real signal
       months ahead (marine heatwaves persist). Active only OUTSIDE Method 5's
       window — inside it the live observation supersedes this. */
    var m7 = null;
    if (params.summerAnomalyC != null && params.summerLeadDays != null &&
        params.summerLeadDays > C.M5_MAX_LEAD_DAYS) {
      var retained = Math.exp(-params.summerLeadDays / C.SEASONAL_EFOLD_DAYS);
      m7 = {
        anomalyNow: params.summerAnomalyC,
        leadDays: params.summerLeadDays,
        retained: retained,
        anomalyAtRace: params.summerAnomalyC * retained,
        baseline: params.summerBaseline || 'same dates, 2023-25 marine archive'
      };
      m7.temp = m2.temp + m7.anomalyAtRace;
      m7.sd = 1.4;   // wide by construction — seasonal persistence is a weak signal
      m7.lo = m7.temp - m7.sd; m7.hi = m7.temp + m7.sd;
      m7.weight = 1.0;
    }

    /* ── NE-wind (Bora) day detection inside the forecast's skilful window ── */
    var boraDays = [];
    var windAvailable = !!(params.windDays && params.windDays.length);
    var windCoversWindow = false;
    if (windAvailable) {
      boraDays = params.windDays.filter(function (w) {
        if (w.date > raceISO || w.speedKmh == null || w.dirDeg == null) return false;
        // STRICTLY FUTURE ONLY. A cold-wind day that has already happened is
        // already reflected in the live SST observation — charging the mean for
        // it a second time double-counts. (Replaying 2024 three days out, the
        // sea had already collapsed to 22.6°C and the model still subtracted a
        // further 2.6°C for the same event, landing 2.2°C too cold.)
        if (todayISO && w.date <= todayISO) return false;
        var lead = Math.round((raceDate - _utcFromISO(w.date)) / 86400000);
        // Northerly sector wraps through 0°, so the test is an OR not an AND
        var northerly = w.dirDeg >= C.WIND_DIR_FROM || w.dirDeg <= C.WIND_DIR_TO;
        return lead >= 0 && lead <= C.WIND_SCAN_DAYS &&
               northerly && w.speedKmh >= C.WIND_SPEED_KMH;
      }).map(function (w) { return w.date; });
      // The forecast only counts as skilful evidence once the race is inside
      // WIND_SKILL_DAYS — a 16-day wind forecast has no useful cold-wind signal.
      windCoversWindow = leadDays != null && leadDays <= C.WIND_SKILL_DAYS;
    }

    var cold = coldEventRisk(leadDays, boraDays.length, windCoversWindow);
    var wind = {
      boraDays: boraDays,        // alias kept: the UI reads wind.boraDays
      coldWindDays: boraDays,
      available: windAvailable,
      skilful: windCoversWindow,
      leadDays: leadDays,
      shift: cold.shift,
      extraColdSd: cold.extraColdSd,
      p: cold.p,
      note: !windCoversWindow
        ? 'Beyond the wind forecast\'s skill horizon (' + C.WIND_SKILL_DAYS + ' days) — climatological cold-drop risk applied, NOT zero'
        : boraDays.length
          ? boraDays.length + ' northerly cold-wind day(s) forecast within ' + C.WIND_SCAN_DAYS +
            ' days of the race — shallow-Adriatic upwelling risk elevated (cf. 2024, which had 4)'
          : 'Race-week forecast shows no northerly wind above ' + C.WIND_SPEED_KMH +
            ' km/h — cold-drop risk reduced to its floor, but not to zero'
    };

    /* ── STAGE 1 ensemble ─────────────────────────────────────────────── */
    var deflate = climDeflation(leadDays);                       // m1/m2 — year-blind
    var deflateAug = climDeflation(leadDays, C.AUG_DEFLATE_MIN); // m3/m4 — August-informed
    var methods = [
      { key: 'm1', name: 'Historical Average', temp: m1.mean, sd: m1.sd,
        weight: _HAND_WEIGHTS.m1 * deflate, deflated: true, yearBlind: true },
      { key: 'm2', name: 'Climatological Normal', temp: m2.temp, sd: m2.sd,
        weight: _HAND_WEIGHTS.m2 * deflate, deflated: true, yearBlind: true },
      { key: 'm3', name: 'Cooling Curve', temp: m3.temp, sd: m3.sd,
        weight: _HAND_WEIGHTS.m3 * deflateAug, deflated: true },
      { key: 'm4', name: 'August Anomaly', temp: m4.temp, sd: m4.sd,
        weight: _HAND_WEIGHTS.m4 * deflateAug, deflated: true }
    ];
    if (m5) methods.push({ key: 'm5', name: 'Live Observation', temp: m5.temp, sd: m5.sd, weight: m5.weight });
    if (m6) methods.push({ key: 'm6', name: 'Ocean-Model Forecast', temp: m6.temp, sd: m6.sd, weight: m6.weight });
    if (m7) methods.push({ key: 'm7', name: 'Summer Anomaly (seasonal)', temp: m7.temp, sd: m7.sd, weight: m7.weight });

    var blendSST = _blendSST(methods);
    var totalW = methods.reduce(function (a, m) { return a + m.weight; }, 0);

    /* Sea surface on race day: ensemble, then the cold-event mean allowance */
    var sst = {
      blend: blendSST.temp,
      blendSd: blendSST.sd,
      coldShift: cold.shift,
      temp: blendSST.temp + cold.shift,
      sd: blendSST.sd,
      // Quadrature, not linear addition: the cold-event risk and the model's
      // own uncertainty are independent sources of variance. (v1.x added them,
      // over-widening the tail.)
      sdLo: Math.sqrt(blendSST.sd * blendSST.sd + cold.extraColdSd * cold.extraColdSd),
      deflation: deflate,
      deflationAug: deflateAug,
      // Share of the blend held by methods that know this year's sea state
      currentYearWeightPct: 100 * methods.reduce(function (a, m) {
        return a + (m.yearBlind ? 0 : m.weight);
      }, 0) / totalW
    };
    sst.at6am = sst.temp + chain.diurnal;

    /* ── STAGE 2 — predicted official reading ─────────────────────────── */
    var bt = backtest();
    var reading = {
      temp: sst.temp + chain.total,
      analyticSd: Math.sqrt(sst.sd * sst.sd + chain.sd * chain.sd)
    };
    // Calibration: methods 2-4 share a climatology/August signal, so the
    // analytic inverse-variance sd overstates confidence. Floor at the measured
    // LOO error — which is already scored in reading space, so it subsumes the
    // Stage-2 offset error rather than adding to it. A live race-week forecast
    // or observation genuinely adds information the backtest years never had,
    // so the floor relaxes once Method 6 or a ≤7-day observation is active —
    // but only to 0.6×, not the 0.5× of v1.x: a sharper SST forecast reduces
    // the SST-side error and nothing else. The Stage-2 reading offset is the
    // dominant term and no amount of forecast skill shrinks it.
    var sdFloor = (m6 || (m5 && m5.daysUntilRace <= 7)) ? bt.rmse * 0.6 : bt.rmse;
    reading.sd = Math.max(reading.analyticSd, sdFloor);
    reading.sdLo = Math.sqrt(reading.sd * reading.sd + cold.extraColdSd * cold.extraColdSd);
    reading.sdFloorApplied = reading.sd > reading.analyticSd;
    reading.lo = reading.temp - 1.5 * reading.sdLo;
    reading.hi = reading.temp + 1.5 * reading.sd;

    /* Probabilities — split-normal: the cold-event risk fattens the LEFT tail */
    var prob = {
      ag: splitNormalCDF(C.AG_THRESHOLD, reading.temp, reading.sdLo, reading.sd) * 100,
      pro: splitNormalCDF(C.PRO_THRESHOLD, reading.temp, reading.sdLo, reading.sd) * 100
    };
    var verdict = prob.ag >= 80 ? 'Likely wetsuit-legal' :
                  prob.ag >= 40 ? 'Borderline — could go either way' :
                  'Likely non-wetsuit';
    var band = prob.ag >= 80 ? 'likely' : prob.ag >= 40 ? 'borderline' : 'unlikely';

    /* Human-readable Stage-1 → Stage-2 walk, so the sea-surface forecast and
       the measurement offsets are never conflated in the UI. */
    var chainSteps = [
      { label: 'Ensemble sea-surface forecast (daily max)', delta: null, value: sst.blend,
        note: methods.length + ' methods, ' + Math.round(sst.currentYearWeightPct) + '% of weight on current-year data' },
      { label: 'Cold-event (NE-wind) risk allowance', delta: cold.shift, value: sst.temp,
        note: 'P(collapse) = ' + Math.round(cold.p * 100) + '% — left tail widened ' + (Math.round(cold.extraColdSd * 100) / 100) + '°C' },
      { label: 'Diurnal: 6am vs daily max', delta: chain.diurnal, value: sst.at6am,
        note: 'measured −' + Math.abs(chain.diurnal) + ' ± ' + chain.diurnalSd + '°C, n=90 September days' },
      { label: 'IM reading vs model SST at 6am', delta: chain.reading, value: reading.temp,
        note: 'coastal 60cm vs offshore grid cell — median of n=' + chain.readingN +
              ' race days, ± ' + (Math.round(chain.readingSd * 100) / 100) +
              '°C (mean would be ' + (Math.round(chain.readingMean * 100) / 100) +
              '°C, dragged by the disputed 2023 reading)' }
    ];

    return {
      dataUpdated: C.DATA_UPDATED,
      raceYear: raceYear, raceDayOfSept: raceDayOfSept, augustTemp: augustTemp,
      leadDays: leadDays, todayISO: todayISO,
      offsetChain: chain,
      chainSteps: chainSteps,
      bias: legacyBias,          // seatemperature.info offset — provenance only
      m1: m1, m2: m2, m3: m3, m4: m4, m5: m5, m6: m6, m7: m7,
      wind: wind, cold: cold,
      methods: methods,
      sst: sst,
      reading: reading,
      // Back-compat alias: `ensemble` has always meant the predicted OFFICIAL
      // READING, and still does.
      ensemble: {
        temp: reading.temp, sd: reading.sd, sdLo: reading.sdLo,
        lo: reading.lo, hi: reading.hi,
        analyticSd: reading.analyticSd,
        raw: sst.temp,
        calibration: { backtestRmse: bt.rmse, sdFloorApplied: reading.sdFloorApplied }
      },
      backtest: bt,
      prob: prob,
      verdict: verdict,
      band: band,
      histTemps: core.histTemps
    };
  }

  return {
    RACE_HISTORY: RACE_HISTORY,
    CONSTANTS: CONSTANTS,
    normalCDF: normalCDF,
    splitNormalCDF: splitNormalCDF,
    defaultRace: defaultRace,
    readingOffsetStats: readingOffsetStats,
    climDeflation: climDeflation,
    coldEventRisk: coldEventRisk,
    backtest: backtest,
    predictWater: predictWater
  };
});
