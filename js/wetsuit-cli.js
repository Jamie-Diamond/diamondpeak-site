#!/usr/bin/env node
/*
 * wetsuit-cli.js — thin JSON bridge over wetsuit-engine.js so non-JS callers
 * (the ClaudeCoach Python bot, via lib/plan_tools.py) run the SAME prediction
 * engine as cycling/cervia-wetsuit.html. The only logic here is the Open-Meteo
 * fetching for the `live` command — the maths all lives in the engine.
 *
 *   node wetsuit-cli.js predict  '{"raceYear":2026,"raceDayOfSept":19,...}'
 *   node wetsuit-cli.js live     '{"raceYear":2026,"raceDayOfSept":19}'   (fetches live SST,
 *                                 ocean-model forecast and race-week wind)
 *   node wetsuit-cli.js live     '{}'                                     (defaults to next race)
 *   node wetsuit-cli.js backtest                                          (leave-one-out skill report)
 *
 * Prints one JSON object to stdout; on error prints {"error":...} and exits 1.
 */
'use strict';
var WE = require('./wetsuit-engine.js');

var LOC = WE.CONSTANTS.LOCATION;
var MARINE_BASE = 'https://marine-api.open-meteo.com/v1/marine' +
  '?latitude=' + LOC.lat + '&longitude=' + LOC.lon +
  '&daily=sea_surface_temperature_max&timezone=Europe/Rome';
var WEATHER_BASE = 'https://api.open-meteo.com/v1/forecast' +
  '?latitude=' + LOC.lat + '&longitude=' + LOC.lon +
  '&daily=wind_speed_10m_max,wind_direction_10m_dominant&timezone=Europe/Rome';

function getJSON(url) {
  if (typeof fetch === 'function') {
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status + ' from Open-Meteo');
      return r.json();
    });
  }
  // Node < 18 fallback
  var https = require('https');
  return new Promise(function (resolve, reject) {
    https.get(url, function (res) {
      if (res.statusCode !== 200) { reject(new Error('HTTP ' + res.statusCode + ' from Open-Meteo')); res.resume(); return; }
      var body = '';
      res.on('data', function (c) { body += c; });
      res.on('end', function () { try { resolve(JSON.parse(body)); } catch (e) { reject(e); } });
    }).on('error', reject);
  });
}

function isoDay(d) { return d.toISOString().slice(0, 10); }

/* Fetch latest SST, last-August average, the ocean-model forecast and the
   race-week wind forecast, then run the engine. Forecast/wind fetches are
   best-effort — the engine simply leaves methods inactive without them. */
function runLive(params) {
  var now = new Date();
  var todayISO = params.todayISO || isoDay(now);
  var today = new Date(todayISO + 'T00:00:00Z');
  var def = WE.defaultRace(today);
  var raceYear = params.raceYear || def.year;
  var raceDayOfSept = params.raceDayOfSept ||
    (raceYear === def.year ? def.dayOfSept : (WE.CONSTANTS.RACE_DATES[raceYear] || 20));
  var raceDate = new Date(Date.UTC(raceYear, 8, raceDayOfSept));
  var daysToRace = Math.round((raceDate - today) / 86400000);

  var start = new Date(today.getTime() - 60 * 86400000);
  var augYear = today.getUTCMonth() >= 7 ? today.getUTCFullYear() : today.getUTCFullYear() - 1;
  var recentUrl = MARINE_BASE + '&start_date=' + isoDay(start) + '&end_date=' + todayISO;
  var augUrl = MARINE_BASE + '&start_date=' + augYear + '-08-01&end_date=' + augYear + '-08-31';
  // Forecast horizons: marine SST ~8-9 days, weather wind ~16 days. Only worth
  // fetching once the race is close enough for the series to reach it.
  var fcUrl = daysToRace >= 0 && daysToRace <= 9 ? MARINE_BASE + '&forecast_days=10' : null;
  var windUrl = daysToRace >= 0 && daysToRace <= 15 ? WEATHER_BASE + '&forecast_days=16' : null;

  // Seasonal-anomaly baseline: the same ~30-day window in up to 3 prior years
  // (the marine archive starts 2023). Only relevant when the race is beyond
  // Method 5's 30-day window — inside it the live anomaly supersedes.
  var baseUrls = [];
  if (daysToRace > 30) {
    var winStart = new Date(today.getTime() - 30 * 86400000);
    for (var by = today.getUTCFullYear() - 1; by >= Math.max(2023, today.getUTCFullYear() - 3); by--) {
      var s = isoDay(winStart).replace(/^\d{4}/, String(by));
      var e = todayISO.replace(/^\d{4}/, String(by));
      baseUrls.push(MARINE_BASE + '&start_date=' + s + '&end_date=' + e);
    }
  }

  return Promise.all([
    getJSON(recentUrl),
    getJSON(augUrl).catch(function () { return null; }),
    fcUrl ? getJSON(fcUrl).catch(function () { return null; }) : Promise.resolve(null),
    windUrl ? getJSON(windUrl).catch(function () { return null; }) : Promise.resolve(null),
    Promise.all(baseUrls.map(function (u) { return getJSON(u).catch(function () { return null; }); }))
  ]).then(function (results) {
    var recent = results[0], aug = results[1], fc = results[2], wf = results[3], baseline = results[4];
    var times = recent.daily.time, temps = recent.daily.sea_surface_temperature_max;
    var lastIdx = temps.length - 1;
    while (lastIdx >= 0 && temps[lastIdx] == null) lastIdx--;
    if (lastIdx < 0) throw new Error('no recent SST data from Open-Meteo');

    var augAvg = null;
    if (aug && aug.daily) {
      var augTemps = aug.daily.sea_surface_temperature_max.filter(function (t) { return t != null; });
      if (augTemps.length > 0) augAvg = augTemps.reduce(function (a, b) { return a + b; }, 0) / augTemps.length;
    }

    var forecastSeries = null;
    if (fc && fc.daily) {
      forecastSeries = fc.daily.time.map(function (t, i) {
        return { date: t, temp: fc.daily.sea_surface_temperature_max[i] };
      });
    }

    var windDays = null;
    if (wf && wf.daily) {
      windDays = wf.daily.time.map(function (t, i) {
        return { date: t, speedKmh: wf.daily.wind_speed_10m_max[i],
                 dirDeg: wf.daily.wind_direction_10m_dominant[i] };
      });
    }

    // Current-summer anomaly: mean of the last ~30 observed days minus the
    // mean of the same window across the baseline years.
    var summerAnomalyC = null, summerBaselineYears = 0;
    var recent30 = temps.slice(Math.max(0, lastIdx - 29), lastIdx + 1).filter(function (t) { return t != null; });
    var baseMeans = (baseline || []).filter(Boolean).map(function (b) {
      var vals = ((b.daily || {}).sea_surface_temperature_max || []).filter(function (t) { return t != null; });
      return vals.length ? vals.reduce(function (a, x) { return a + x; }, 0) / vals.length : null;
    }).filter(function (v) { return v != null; });
    if (recent30.length >= 15 && baseMeans.length) {
      var recentMean = recent30.reduce(function (a, x) { return a + x; }, 0) / recent30.length;
      var baseMean = baseMeans.reduce(function (a, x) { return a + x; }, 0) / baseMeans.length;
      summerAnomalyC = recentMean - baseMean;
      summerBaselineYears = baseMeans.length;
    }

    var prediction = WE.predictWater({
      raceYear: raceYear,
      raceDayOfSept: raceDayOfSept,
      augustAvgSST: params.augustAvgSST != null ? params.augustAvgSST : augAvg,
      liveTemp: temps[lastIdx],
      liveDateISO: times[lastIdx],
      forecastSeries: forecastSeries,
      windDays: windDays,
      summerAnomalyC: summerAnomalyC,
      summerLeadDays: summerAnomalyC != null ? daysToRace : null,
      summerBaseline: summerAnomalyC != null ?
        'same 30-day window, mean of ' + summerBaselineYears + ' archive year(s)' : null
    });
    return {
      live: { temp: temps[lastIdx], date: times[lastIdx], augAvg: augAvg, augYear: augYear,
              daysToRace: daysToRace, summerAnomalyC: summerAnomalyC,
              forecastFetched: !!forecastSeries, windFetched: !!windDays,
              source: 'Open-Meteo Marine + Forecast APIs' },
      prediction: prediction
    };
  });
}

var cmd = process.argv[2];
var params;
try {
  params = JSON.parse(process.argv[3] || '{}');
} catch (e) {
  process.stdout.write(JSON.stringify({ error: 'bad JSON params: ' + e.message }));
  process.exit(1);
}

if (cmd === 'predict') {
  try {
    process.stdout.write(JSON.stringify(WE.predictWater(params)));
  } catch (e) {
    process.stdout.write(JSON.stringify({ error: String(e && e.message || e) }));
    process.exit(1);
  }
} else if (cmd === 'backtest') {
  process.stdout.write(JSON.stringify(WE.backtest()));
} else if (cmd === 'live') {
  runLive(params).then(function (out) {
    process.stdout.write(JSON.stringify(out));
  }).catch(function (e) {
    process.stdout.write(JSON.stringify({ error: String(e && e.message || e) }));
    process.exit(1);
  });
} else {
  process.stdout.write(JSON.stringify({ error: 'unknown command: ' + cmd + ' (use predict|live|backtest)' }));
  process.exit(1);
}
