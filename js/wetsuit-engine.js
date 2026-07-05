/*
 * wetsuit-engine.js — Cervia IRONMAN water-temperature / wetsuit prediction engine.
 *
 * SINGLE SOURCE OF TRUTH for the wetsuit-predictor maths. Loaded two ways:
 *   • the browser (cycling/cervia-wetsuit.html) as window.WetsuitEngine
 *   • Node.js (ClaudeCoach bot, via js/wetsuit-cli.js) as require(...)
 * so the web predictor and the coach bot always give identical answers.
 *
 * Pure functions only — no DOM, no I/O, no clock reads (callers pass dates in).
 * Five methods triangulate the likely official race-morning water temperature:
 *   1. Historical race-day average (empirical)
 *   2. Climatological normal + Mediterranean warming trend (empirical)
 *   3. Seasonal exponential cooling curve from the August peak (model)
 *   4. August anomaly propagation into September (model)
 *   5. Live SST anomaly with decaying persistence (observation, ≤30 days out)
 * An inverse-variance weighted ensemble plus a measurement-bias correction
 * (official IRONMAN readings run cooler than satellite SST) gives the final
 * temperature and wetsuit probabilities.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api; // Node
  if (root) root.WetsuitEngine = api;                                        // browser
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  /* ── Historical race data ─────────────────────────────────────────────
   * waterTemp = official IRONMAN race-morning reading (swim start, 60cm depth)
   * onlineSST = satellite/coastal SST for the same date
   * estimated = no official reading found; waterTemp inferred from satellite
   *             SST — excluded from the measurement-bias statistics.       */
  var RACE_HISTORY = [
    { year: 2017, date: 'Sep 23', dayOfSept: 23, waterTemp: 22.0, onlineSST: 23.0, wetsuit: 'Yes (AG)', source: 'Estimated from satellite SST', onlineSource: 'seatemperature.net (2022–25 archive)', estimated: true },
    { year: 2018, date: 'Sep 22', dayOfSept: 22, waterTemp: 24.8, onlineSST: 25.5, wetsuit: 'No (too warm)', source: 'Race reports — European heatwave year', onlineSource: 'seatemperature.info satellite' },
    { year: 2019, date: 'Sep 21', dayOfSept: 21, waterTemp: 24.6, onlineSST: 25.2, wetsuit: 'Yes (jellyfish exception)', source: 'Race reports — above limit, exception granted', onlineSource: 'seatemperature.info satellite' },
    { year: 2021, date: 'Sep 18', dayOfSept: 18, waterTemp: 24.0, onlineSST: 24.5, wetsuit: 'Yes (AG)', source: 'Satellite SST data', onlineSource: 'seatemperature.info satellite' },
    { year: 2022, date: 'Sep 18', dayOfSept: 18, waterTemp: 25.0, onlineSST: 25.0, wetsuit: 'Borderline / No', source: 'Satellite SST — race delayed by storm', onlineSource: 'seatemperature.net' },
    { year: 2023, date: 'Sep 16', dayOfSept: 16, waterTemp: 22.0, onlineSST: 24.0, wetsuit: 'Yes (AG)', source: 'Race reports confirmed', onlineSource: 'seatemperature.info: 23.5°C' },
    { year: 2024, date: 'Sep 21', dayOfSept: 21, waterTemp: 21.5, onlineSST: 21.5, wetsuit: 'Yes (mandatory)', source: 'Race reports — mandatory wetsuits', onlineSource: 'seatemperature.info: 21.5°C' },
    { year: 2025, date: 'Sep 20', dayOfSept: 20, waterTemp: 24.6, onlineSST: 25.2, wetsuit: 'Unknown (borderline)', source: 'Official reading not found — estimated as satellite SST minus avg bias', onlineSource: 'Open-Meteo Marine archive: 25.2°C', estimated: true }
  ];

  var CONSTANTS = {
    SEPT_CLIM_START: 26.0,       // Sept 1 climatological normal (1991-2020)
    SEPT_CLIM_END: 21.5,         // Sept 30 normal
    AUG_NORMAL: 25.7,            // 30-year August average
    TREND_RATE: 0.04,            // Mediterranean warming °C/year
    TREND_BASE_YEAR: 2010,
    T_WINTER: 14.0,              // cooling-curve winter asymptote
    LAMBDA: 0.0085,              // cooling-curve decay constant
    PERSISTENCE: 0.65,           // Aug→Sept anomaly persistence factor
    ANOMALY_DECAY: 0.05,         // live-anomaly regression to normal, per day
    PRO_THRESHOLD: 21.9,         // wetsuit limits (°C)
    AG_THRESHOLD: 24.5,
    DEFAULT_AUG_SST: 26.2,
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

  /* Next upcoming race edition for a given date ('YYYY-MM-DD' or Date). */
  function defaultRace(today) {
    var d = today instanceof Date ? today : new Date(String(today) + 'T00:00:00Z');
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
   * predictWater(params) — the whole model.
   *   raceYear, raceDayOfSept  race edition (required)
   *   augustAvgSST             this year's August average SST (optional)
   *   liveTemp, liveDateISO    most recent SST observation (optional; Method 5
   *                            activates when 0-30 days before the race)
   */
  function predictWater(params) {
    var C = CONSTANTS;
    var raceYear = params.raceYear;
    var raceDayOfSept = params.raceDayOfSept;
    if (!raceYear || !raceDayOfSept) throw new Error('raceYear and raceDayOfSept are required');
    var augustTemp = params.augustAvgSST != null ? params.augustAvgSST : C.DEFAULT_AUG_SST;

    /* Measurement bias — genuine official readings only */
    var biasRows = RACE_HISTORY.filter(function (r) { return !r.estimated; });
    var biases = biasRows.map(function (r) { return r.waterTemp - r.onlineSST; });
    var avgBias = _mean(biases);
    var biasSd = _sd(biases);

    /* Method 1 — historical race-day average */
    var histTemps = RACE_HISTORY.map(function (r) { return r.waterTemp; });
    var m1 = { mean: _mean(histTemps), sd: _sd(histTemps) };
    m1.lo = m1.mean - m1.sd; m1.hi = m1.mean + m1.sd;

    /* Method 2 — climatological normal + warming trend */
    var m2 = {};
    m2.baseline = C.SEPT_CLIM_START - C.SEPT_COOLING_RATE * (raceDayOfSept - 1);
    m2.trend = C.TREND_RATE * (raceYear - C.TREND_BASE_YEAR);
    m2.temp = m2.baseline + m2.trend;
    m2.sd = 1.5;
    m2.lo = m2.temp - m2.sd; m2.hi = m2.temp + m2.sd;

    /* Method 3 — seasonal cooling curve from the August peak */
    var m3 = { daysSincePeak: raceDayOfSept + 16 };
    m3.temp = C.T_WINTER + (augustTemp - C.T_WINTER) * Math.exp(-C.LAMBDA * m3.daysSincePeak);
    m3.sd = 1.2;
    m3.lo = m3.temp - m3.sd; m3.hi = m3.temp + m3.sd;

    /* Method 4 — August anomaly propagation */
    var m4 = { anomaly: augustTemp - C.AUG_NORMAL };
    m4.propagated = C.PERSISTENCE * m4.anomaly;
    m4.base = m2.baseline;
    m4.temp = m4.base + m2.trend + m4.propagated;
    m4.sd = 1.1;
    m4.lo = m4.temp - m4.sd; m4.hi = m4.temp + m4.sd;

    /* Method 5 — live anomaly with decaying persistence */
    var m5 = null;
    if (params.liveTemp != null && params.liveDateISO) {
      var raceDate = new Date(Date.UTC(raceYear, 8, raceDayOfSept));
      var obsDate = _utcFromISO(params.liveDateISO);
      var daysUntilRace = Math.round((raceDate - obsDate) / 86400000);
      if (daysUntilRace >= 0 && daysUntilRace <= 30) {
        var obsDayOfSept = obsDate.getUTCMonth() === 8 ? obsDate.getUTCDate() : 1;
        m5 = { daysUntilRace: daysUntilRace, obsDayOfSept: obsDayOfSept, obsTemp: params.liveTemp };
        m5.obsClim = C.SEPT_CLIM_START - C.SEPT_COOLING_RATE * (obsDayOfSept - 1) + m2.trend;
        m5.liveAnomaly = params.liveTemp - m5.obsClim;
        m5.anomalyAtRace = m5.liveAnomaly * Math.exp(-C.ANOMALY_DECAY * daysUntilRace);
        m5.temp = m2.temp + m5.anomalyAtRace;
        m5.sd = 0.3 + daysUntilRace * 0.04;
        m5.lo = m5.temp - m5.sd; m5.hi = m5.temp + m5.sd;
        m5.confidence = daysUntilRace <= 3 ? 'HIGH' : daysUntilRace <= 7 ? 'MEDIUM' :
                        daysUntilRace <= 14 ? 'LOW' : 'VERY LOW';
        m5.weight = daysUntilRace <= 3 ? 3.0 : daysUntilRace <= 7 ? 2.0 :
                    daysUntilRace <= 14 ? 1.0 : 0.5;
      }
    }

    /* Ensemble — weighted average, inverse-variance uncertainty, bias-corrected */
    var methods = [
      { key: 'm1', name: 'Historical Average', temp: m1.mean, sd: m1.sd, weight: 1.0 },
      { key: 'm2', name: 'Climatological Normal', temp: m2.temp, sd: m2.sd, weight: 1.2 },
      { key: 'm3', name: 'Cooling Curve', temp: m3.temp, sd: m3.sd, weight: 1.3 },
      { key: 'm4', name: 'August Anomaly', temp: m4.temp, sd: m4.sd, weight: 1.4 }
    ];
    if (m5) methods.push({ key: 'm5', name: 'Live Anomaly', temp: m5.temp, sd: m5.sd, weight: m5.weight });

    var totalWeight = 0, weightedTemp = 0, invVar = 0, i;
    for (i = 0; i < methods.length; i++) {
      totalWeight += methods[i].weight;
      weightedTemp += methods[i].temp * methods[i].weight;
      invVar += methods[i].weight / (methods[i].sd * methods[i].sd);
    }
    var ensemble = { raw: weightedTemp / totalWeight };
    ensemble.sd = Math.sqrt(1 / invVar + biasSd * biasSd);
    ensemble.temp = ensemble.raw + avgBias;
    ensemble.lo = ensemble.temp - 1.5 * ensemble.sd;
    ensemble.hi = ensemble.temp + 1.5 * ensemble.sd;

    var prob = {
      ag: normalCDF((C.AG_THRESHOLD - ensemble.temp) / ensemble.sd) * 100,
      pro: normalCDF((C.PRO_THRESHOLD - ensemble.temp) / ensemble.sd) * 100
    };
    var verdict = prob.ag >= 80 ? 'Likely wetsuit-legal' :
                  prob.ag >= 40 ? 'Borderline — could go either way' :
                  'Likely non-wetsuit';
    var band = prob.ag >= 80 ? 'likely' : prob.ag >= 40 ? 'borderline' : 'unlikely';

    return {
      raceYear: raceYear, raceDayOfSept: raceDayOfSept, augustTemp: augustTemp,
      bias: { avg: avgBias, sd: biasSd, n: biases.length },
      m1: m1, m2: m2, m3: m3, m4: m4, m5: m5,
      methods: methods,
      ensemble: ensemble,
      prob: prob,
      verdict: verdict,
      band: band,
      histTemps: histTemps
    };
  }

  return {
    RACE_HISTORY: RACE_HISTORY,
    CONSTANTS: CONSTANTS,
    normalCDF: normalCDF,
    defaultRace: defaultRace,
    predictWater: predictWater
  };
});
