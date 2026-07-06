/*
 * wetsuit-engine.js — Cervia IRONMAN water-temperature / wetsuit prediction engine.
 *
 * SINGLE SOURCE OF TRUTH for the wetsuit-predictor maths. Loaded two ways:
 *   • the browser (cycling/cervia-wetsuit.html) as window.WetsuitEngine
 *   • Node.js (ClaudeCoach bot, via js/wetsuit-cli.js) as require(...)
 * so the web predictor and the coach bot always give identical answers.
 *
 * Pure functions only — no DOM, no I/O, no clock reads (callers pass dates and
 * any fetched series in). Six methods triangulate the official race-morning
 * water temperature:
 *   1. Historical race-day average (empirical)
 *   2. Climatological normal + Mediterranean warming trend (empirical)
 *   3. Seasonal exponential cooling curve from the August peak (model)
 *   4. August anomaly propagation into September (model)
 *   5. Live SST anomaly with decaying persistence (observation, ≤30 days out)
 *   6. Physical ocean-model FORECAST SST (Open-Meteo marine, ≤~8 days out)
 * plus two calibration layers:
 *   • leave-one-out BACKTEST over the real historical years — the ensemble's
 *     uncertainty is floored at the empirically measured error, so the quoted
 *     probability can't be more confident than the model's actual track record
 *     (methods 2-4 share an August/climatology signal; hand weights alone
 *     would double-count their agreement)
 *   • a Bora/NE-wind cold-tail adjustment — the shallow north Adriatic can
 *     drop several degrees in days under NE wind (2024: 21.5°C, mandatory
 *     wetsuits), so forecast NE wind in race week shifts and fattens the COLD
 *     side of the distribution (split-normal), raising wetsuit probability.
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
   * augSST    = that year's August mean SST (Open-Meteo marine archive; the
   *             archive starts 2023 — earlier years null → climatological
   *             normal is used where an August input is needed)
   * estimated = no official reading found; waterTemp inferred from satellite
   *             SST — excluded from bias statistics AND from the backtest.   */
  var RACE_HISTORY = [
    { year: 2017, date: 'Sep 23', dayOfSept: 23, waterTemp: 22.0, onlineSST: 23.0, augSST: null, wetsuit: 'Yes (AG)', source: 'Estimated from satellite SST', onlineSource: 'seatemperature.net (2022–25 archive)', estimated: true },
    { year: 2018, date: 'Sep 22', dayOfSept: 22, waterTemp: 24.8, onlineSST: 25.5, augSST: null, wetsuit: 'No (too warm)', source: 'Race reports — European heatwave year', onlineSource: 'seatemperature.info satellite' },
    { year: 2019, date: 'Sep 21', dayOfSept: 21, waterTemp: 24.6, onlineSST: 25.2, augSST: null, wetsuit: 'Yes (jellyfish exception)', source: 'Race reports — above limit, exception granted', onlineSource: 'seatemperature.info satellite' },
    { year: 2021, date: 'Sep 18', dayOfSept: 18, waterTemp: 24.0, onlineSST: 24.5, augSST: null, wetsuit: 'Yes (AG)', source: 'Satellite SST data', onlineSource: 'seatemperature.info satellite' },
    { year: 2022, date: 'Sep 18', dayOfSept: 18, waterTemp: 21.0, onlineSST: null, augSST: null, wetsuit: 'Yes (AG — wetsuit swim)', source: 'Athlete-reported ~21°C (research 6 Jul 2026). Race storm-delayed 17→18 Sep; the previous 25.0 was satellite for the PRE-storm window — a wind cold-drop like 2024', onlineSource: 'no same-day satellite (archive starts 2023)', reported: true },
    { year: 2023, date: 'Sep 16', dayOfSept: 16, waterTemp: 22.0, onlineSST: 23.5, augSST: 27.77, wetsuit: 'Yes (AG)', source: 'Race reports confirmed', onlineSource: 'seatemperature.info: 23.5°C (field previously 24.0 — corrected to match source)' },
    { year: 2024, date: 'Sep 21', dayOfSept: 21, waterTemp: 21.5, onlineSST: 21.5, augSST: 29.75, wetsuit: 'Yes (mandatory)', source: 'Race reports — mandatory wetsuits (Bora-type cold drop)', onlineSource: 'seatemperature.info: 21.5°C' },
    { year: 2025, date: 'Sep 20', dayOfSept: 20, waterTemp: 24.5, onlineSST: 25.2, augSST: 27.44, wetsuit: 'Yes (AG)', source: 'Athlete raced it — wetsuit-legal, so official reading ≤24.5; exact value unknown', onlineSource: 'Open-Meteo Marine archive: 25.2°C', estimated: true }
  ];

  var CONSTANTS = {
    // Bump whenever the race history, climatology or model constants change
    DATA_UPDATED: '2026-07-06',
    DATA_UPDATED_LABEL: '6 July 2026',
    SEPT_CLIM_START: 26.0,       // Sept 1 climatological normal (1991-2020)
    SEPT_CLIM_END: 21.5,         // Sept 30 normal
    AUG_NORMAL: 25.7,            // 30-year August average
    TREND_RATE: 0.04,            // Mediterranean warming °C/year
    TREND_BASE_YEAR: 2010,
    T_WINTER: 14.0,              // cooling-curve winter asymptote
    LAMBDA: 0.0085,              // cooling-curve decay constant
    PERSISTENCE: 0.65,           // Aug→Sept anomaly persistence factor
    ANOMALY_DECAY: 0.05,         // live-anomaly regression to normal, per day
    // Seasonal (marine-heatwave scale) anomaly persistence — much slower than
    // the day-scale ANOMALY_DECAY above. E-folding ~65 days is typical for
    // Mediterranean summer SST anomalies (standard oceanography, not fitted
    // to this dataset — the marine archive is too short to fit it).
    SEASONAL_EFOLD_DAYS: 65,
    PRO_THRESHOLD: 21.9,         // wetsuit limits (°C)
    AG_THRESHOLD: 24.5,
    DEFAULT_AUG_SST: 26.2,
    // Bora heuristic (flagged as judgement, not fitted — n is far too small):
    // a day counts when dominant direction is NE-ish and daily max wind is
    // strong; each such day within 5 days of the race pulls the mean down and
    // fattens the cold tail.
    BORA_DIR_MIN: 10, BORA_DIR_MAX: 100,    // degrees, NE sector
    BORA_SPEED_KMH: 30,                     // daily max ≥ this counts
    BORA_WINDOW_DAYS: 5,                    // days before race considered
    BORA_SHIFT_PER_DAY: -0.5, BORA_SHIFT_CAP: -1.5,
    BORA_COLD_SD_PER_DAY: 0.4, BORA_COLD_SD_CAP: 1.2,
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
     the mean; integrates to 1. Used for the Bora cold-tail adjustment. */
  function splitNormalCDF(t, mean, sdLo, sdHi) {
    var A = 2 * sdLo / (sdLo + sdHi), B = 2 * sdHi / (sdLo + sdHi);
    if (t < mean) return A * normalCDF((t - mean) / sdLo);
    return A / 2 + B * (normalCDF((t - mean) / sdHi) - 0.5);
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

  /* Core methods 1-4 for a given race (year, dayOfSept) computed from a given
     set of history rows — factored out so the backtest can rerun them with a
     year held out. augustTemp: that year's August mean SST (or normal). */
  function _coreMethods(raceYear, raceDayOfSept, augustTemp, histRows) {
    var C = CONSTANTS;
    var histTemps = histRows.map(function (r) { return r.waterTemp; });
    var m1 = { mean: _mean(histTemps), sd: histTemps.length > 1 ? _sd(histTemps) : 1.5 };
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

  function _blend(methods, avgBias, biasSd) {
    var totalWeight = 0, weightedTemp = 0, invVar = 0, i;
    for (i = 0; i < methods.length; i++) {
      totalWeight += methods[i].weight;
      weightedTemp += methods[i].temp * methods[i].weight;
      invVar += methods[i].weight / (methods[i].sd * methods[i].sd);
    }
    var raw = weightedTemp / totalWeight;
    return {
      raw: raw,
      temp: raw + avgBias,
      sd: Math.sqrt(1 / invVar + biasSd * biasSd)
    };
  }

  var _HAND_WEIGHTS = { m1: 1.0, m2: 1.2, m3: 1.3, m4: 1.4 };

  /*
   * backtest() — leave-one-out over the REAL (non-estimated) historical years.
   * For each such year: rebuild bias stats and the m1 mean from the OTHER real
   * years, run methods 1-4 with that year's August SST (or the climatological
   * normal where the archive has none), blend with the hand weights, and score
   * against the actual official reading. Returns per-year rows plus MAE/RMSE
   * and how often the wetsuit CALL (≤24.5) was right. n is small (6) — that is
   * exactly why the quoted ensemble sd is FLOORED at this RMSE rather than
   * per-method weights being fitted to it.
   */
  function backtest() {
    var C = CONSTANTS;
    var real = RACE_HISTORY.filter(function (r) { return !r.estimated; });
    var rows = [], errs = [], calls = 0;
    for (var i = 0; i < real.length; i++) {
      var y = real[i];
      var others = real.filter(function (r) { return r.year !== y.year && r.onlineSST != null; });
      var biases = others.map(function (r) { return r.waterTemp - r.onlineSST; });
      var avgBias = _mean(biases), biasSd = _sd(biases);
      // m1 history: everything except the held-out year (estimated rows allowed
      // for the mean, as in live prediction — only the SCORING year must be real)
      var histRows = RACE_HISTORY.filter(function (r) { return r.year !== y.year; });
      var core = _coreMethods(y.year, y.dayOfSept, y.augSST != null ? y.augSST : C.AUG_NORMAL, histRows);
      var methods = [
        { key: 'm1', temp: core.m1.mean, sd: core.m1.sd, weight: _HAND_WEIGHTS.m1 },
        { key: 'm2', temp: core.m2.temp, sd: core.m2.sd, weight: _HAND_WEIGHTS.m2 },
        { key: 'm3', temp: core.m3.temp, sd: core.m3.sd, weight: _HAND_WEIGHTS.m3 },
        { key: 'm4', temp: core.m4.temp, sd: core.m4.sd, weight: _HAND_WEIGHTS.m4 }
      ];
      var blend = _blend(methods, avgBias, biasSd);
      var err = blend.temp - y.waterTemp;
      var callPred = blend.temp <= C.AG_THRESHOLD, callActual = y.waterTemp <= C.AG_THRESHOLD;
      if (callPred === callActual) calls++;
      errs.push(err);
      rows.push({ year: y.year, predicted: Math.round(blend.temp * 10) / 10,
                  actual: y.waterTemp, error: Math.round(err * 100) / 100,
                  augInput: y.augSST != null ? 'archive' : 'normal',
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
   *                             race day, or a day within 2 days of it, is in
   *                             the series)
   *   windDays                  [{date:'YYYY-MM-DD', speedKmh, dirDeg}] daily
   *                             max wind forecast (optional; Bora adjustment
   *                             uses days within BORA_WINDOW_DAYS of the race)
   */
  function predictWater(params) {
    var C = CONSTANTS;
    var raceYear = params.raceYear;
    var raceDayOfSept = params.raceDayOfSept;
    if (!raceYear || !raceDayOfSept) throw new Error('raceYear and raceDayOfSept are required');
    var augustTemp = params.augustAvgSST != null ? params.augustAvgSST : C.DEFAULT_AUG_SST;
    var raceDate = new Date(Date.UTC(raceYear, 8, raceDayOfSept));
    var raceISO = raceDate.toISOString().slice(0, 10);

    /* Measurement bias — genuine official readings with same-day satellite only
       (2022 is athlete-reported with no valid same-day satellite: in the mean
       and backtest, but not the bias stats) */
    var biasRows = RACE_HISTORY.filter(function (r) { return !r.estimated && r.onlineSST != null; });
    var biases = biasRows.map(function (r) { return r.waterTemp - r.onlineSST; });
    var avgBias = _mean(biases);
    var biasSd = _sd(biases);

    /* Methods 1-4 (shared with the backtest) */
    var core = _coreMethods(raceYear, raceDayOfSept, augustTemp, RACE_HISTORY);
    var m1 = core.m1, m2 = core.m2, m3 = core.m3, m4 = core.m4;

    /* Method 5 — live anomaly with decaying persistence */
    var m5 = null;
    if (params.liveTemp != null && params.liveDateISO) {
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

    /* Method 6 — physical ocean-model forecast SST. The strongest signal when
       available: an assimilated forecast beats any statistical decay rule.
       Uses the forecast for race day itself, or the last forecast day if it
       falls at most 2 days short (uncertainty grows with the gap). */
    var m6 = null;
    if (params.forecastSeries && params.forecastSeries.length) {
      var usable = params.forecastSeries.filter(function (f) {
        return f.temp != null && f.date <= raceISO;
      });
      if (usable.length) {
        var last = usable[usable.length - 1];
        var exact = usable.filter(function (f) { return f.date === raceISO; })[0];
        var pick = exact || last;
        var gapDays = Math.round((raceDate - _utcFromISO(pick.date)) / 86400000);
        if (gapDays <= 2) {
          m6 = { forecastDate: pick.date, gapDays: gapDays };
          // Forecast is a sea-surface value like satellite SST — the official
          // reading bias is applied at ensemble level like every other method.
          m6.temp = pick.temp;
          m6.sd = 0.4 + 0.15 * gapDays;
          m6.lo = m6.temp - m6.sd; m6.hi = m6.temp + m6.sd;
          m6.weight = gapDays === 0 ? 4.0 : gapDays === 1 ? 3.0 : 2.0;
        }
      }
    }

    /* Method 7 — CURRENT-summer anomaly with seasonal persistence. The sea's
       temperature TODAY vs the same dates in recent years carries real signal
       months ahead (marine heatwaves persist), which methods 4/5 ignore until
       August / race month. Active only OUTSIDE Method 5's window (>30 days
       out) — inside it the live anomaly supersedes this. summerAnomalyC is
       computed by the caller: mean(last ~30 days SST) minus the same-dates
       mean of the baseline years (2023-25 archive), so it is an anomaly vs
       RECENT climate — warm-biased baseline, hence conservative. */
    var m7 = null;
    if (params.summerAnomalyC != null && params.summerLeadDays != null && params.summerLeadDays > 30) {
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

    /* Bora / NE-wind cold-tail adjustment (heuristic — see CONSTANTS). Only
       wind days inside the window before the race count. */
    var wind = null;
    if (params.windDays && params.windDays.length) {
      var boraDays = params.windDays.filter(function (w) {
        if (w.date > raceISO || w.speedKmh == null || w.dirDeg == null) return false;
        var lead = Math.round((raceDate - _utcFromISO(w.date)) / 86400000);
        return lead >= 0 && lead <= C.BORA_WINDOW_DAYS &&
               w.dirDeg >= C.BORA_DIR_MIN && w.dirDeg <= C.BORA_DIR_MAX &&
               w.speedKmh >= C.BORA_SPEED_KMH;
      });
      if (boraDays.length) {
        wind = {
          boraDays: boraDays.map(function (w) { return w.date; }),
          shift: Math.max(C.BORA_SHIFT_CAP, C.BORA_SHIFT_PER_DAY * boraDays.length),
          extraColdSd: Math.min(C.BORA_COLD_SD_CAP, C.BORA_COLD_SD_PER_DAY * boraDays.length),
          note: 'NE (Bora-sector) wind forecast near race day — shallow-Adriatic cold-drop risk; ' +
                'cold tail fattened (this raises wetsuit probability, cf. 2024 mandatory-wetsuit race)'
        };
      } else {
        wind = { boraDays: [], shift: 0, extraColdSd: 0,
                 note: 'no significant NE wind forecast in the pre-race window' };
      }
    }

    /* Ensemble — weighted blend, uncertainty FLOORED at the backtest RMSE */
    var methods = [
      { key: 'm1', name: 'Historical Average', temp: m1.mean, sd: m1.sd, weight: _HAND_WEIGHTS.m1 },
      { key: 'm2', name: 'Climatological Normal', temp: m2.temp, sd: m2.sd, weight: _HAND_WEIGHTS.m2 },
      { key: 'm3', name: 'Cooling Curve', temp: m3.temp, sd: m3.sd, weight: _HAND_WEIGHTS.m3 },
      { key: 'm4', name: 'August Anomaly', temp: m4.temp, sd: m4.sd, weight: _HAND_WEIGHTS.m4 }
    ];
    if (m5) methods.push({ key: 'm5', name: 'Live Anomaly', temp: m5.temp, sd: m5.sd, weight: m5.weight });
    if (m6) methods.push({ key: 'm6', name: 'Ocean-Model Forecast', temp: m6.temp, sd: m6.sd, weight: m6.weight });
    if (m7) methods.push({ key: 'm7', name: 'Summer Anomaly (seasonal)', temp: m7.temp, sd: m7.sd, weight: m7.weight });

    var bt = backtest();
    var blend = _blend(methods, avgBias, biasSd);
    var ensemble = { raw: blend.raw, analyticSd: blend.sd };
    // Calibration: methods 2-4 are correlated (shared climatology/August
    // signal), so the analytic inverse-variance sd overstates confidence.
    // The quoted sd can never be smaller than the measured LOO error — unless
    // a race-week forecast/observation is active, which genuinely adds
    // information the backtest years never had; then trust the analytic sd
    // down to a floor of half the RMSE.
    var sdFloor = (m6 || (m5 && m5.daysUntilRace <= 7)) ? bt.rmse * 0.5 : bt.rmse;
    ensemble.sd = Math.max(blend.sd, sdFloor);
    ensemble.temp = blend.temp;
    ensemble.calibration = { backtestRmse: bt.rmse, sdFloorApplied: ensemble.sd > blend.sd };
    ensemble.lo = ensemble.temp - 1.5 * ensemble.sd;
    ensemble.hi = ensemble.temp + 1.5 * ensemble.sd;

    /* Probabilities — split-normal when the Bora adjustment is active */
    var mean = ensemble.temp + (wind ? wind.shift : 0);
    var sdLo = ensemble.sd + (wind ? wind.extraColdSd : 0);
    var sdHi = ensemble.sd;
    var prob = {
      ag: splitNormalCDF(C.AG_THRESHOLD, mean, sdLo, sdHi) * 100,
      pro: splitNormalCDF(C.PRO_THRESHOLD, mean, sdLo, sdHi) * 100
    };
    var verdict = prob.ag >= 80 ? 'Likely wetsuit-legal' :
                  prob.ag >= 40 ? 'Borderline — could go either way' :
                  'Likely non-wetsuit';
    var band = prob.ag >= 80 ? 'likely' : prob.ag >= 40 ? 'borderline' : 'unlikely';

    return {
      dataUpdated: C.DATA_UPDATED,
      raceYear: raceYear, raceDayOfSept: raceDayOfSept, augustTemp: augustTemp,
      bias: { avg: avgBias, sd: biasSd, n: biases.length },
      m1: m1, m2: m2, m3: m3, m4: m4, m5: m5, m6: m6, m7: m7,
      wind: wind,
      methods: methods,
      ensemble: ensemble,
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
    backtest: backtest,
    predictWater: predictWater
  };
});
