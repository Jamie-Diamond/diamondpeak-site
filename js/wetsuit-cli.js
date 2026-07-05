#!/usr/bin/env node
/*
 * wetsuit-cli.js — thin JSON bridge over wetsuit-engine.js so non-JS callers
 * (the ClaudeCoach Python bot, via lib/plan_tools.py) run the SAME prediction
 * engine as cycling/cervia-wetsuit.html. The only logic here is the Open-Meteo
 * Marine fetch for the `live` command — the maths all lives in the engine.
 *
 *   node wetsuit-cli.js predict '{"raceYear":2026,"raceDayOfSept":19,...}'
 *   node wetsuit-cli.js live    '{"raceYear":2026,"raceDayOfSept":19}'   (fetches live SST)
 *   node wetsuit-cli.js live    '{}'                                     (defaults to next race)
 *
 * Prints one JSON object to stdout; on error prints {"error":...} and exits 1.
 */
'use strict';
var WE = require('./wetsuit-engine.js');

var MARINE_BASE = 'https://marine-api.open-meteo.com/v1/marine' +
  '?latitude=' + WE.CONSTANTS.LOCATION.lat + '&longitude=' + WE.CONSTANTS.LOCATION.lon +
  '&daily=sea_surface_temperature_max&timezone=Europe/Rome';

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

/* Fetch the latest SST reading + last August's average, then run the engine. */
function runLive(params) {
  var now = new Date();
  var todayISO = params.todayISO || isoDay(now);
  var today = new Date(todayISO + 'T00:00:00Z');
  var def = WE.defaultRace(today);
  var raceYear = params.raceYear || def.year;
  var raceDayOfSept = params.raceDayOfSept ||
    (raceYear === def.year ? def.dayOfSept : (WE.CONSTANTS.RACE_DATES[raceYear] || 20));

  var start = new Date(today.getTime() - 60 * 86400000);
  var augYear = today.getUTCMonth() >= 7 ? today.getUTCFullYear() : today.getUTCFullYear() - 1;
  var recentUrl = MARINE_BASE + '&start_date=' + isoDay(start) + '&end_date=' + todayISO;
  var augUrl = MARINE_BASE + '&start_date=' + augYear + '-08-01&end_date=' + augYear + '-08-31';

  return Promise.all([
    getJSON(recentUrl),
    getJSON(augUrl).catch(function () { return null; })
  ]).then(function (results) {
    var recent = results[0], aug = results[1];
    var times = recent.daily.time, temps = recent.daily.sea_surface_temperature_max;
    var lastIdx = temps.length - 1;
    while (lastIdx >= 0 && temps[lastIdx] == null) lastIdx--;
    if (lastIdx < 0) throw new Error('no recent SST data from Open-Meteo');

    var augAvg = null;
    if (aug && aug.daily) {
      var augTemps = aug.daily.sea_surface_temperature_max.filter(function (t) { return t != null; });
      if (augTemps.length > 0) augAvg = augTemps.reduce(function (a, b) { return a + b; }, 0) / augTemps.length;
    }

    var prediction = WE.predictWater({
      raceYear: raceYear,
      raceDayOfSept: raceDayOfSept,
      augustAvgSST: params.augustAvgSST != null ? params.augustAvgSST : augAvg,
      liveTemp: temps[lastIdx],
      liveDateISO: times[lastIdx]
    });
    return {
      live: { temp: temps[lastIdx], date: times[lastIdx], augAvg: augAvg, augYear: augYear, source: 'Open-Meteo Marine API' },
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
} else if (cmd === 'live') {
  runLive(params).then(function (out) {
    process.stdout.write(JSON.stringify(out));
  }).catch(function (e) {
    process.stdout.write(JSON.stringify({ error: String(e && e.message || e) }));
    process.exit(1);
  });
} else {
  process.stdout.write(JSON.stringify({ error: 'unknown command: ' + cmd + ' (use predict|live)' }));
  process.exit(1);
}
