#!/usr/bin/env node
/*
 * fuelling-cli.js — thin JSON bridge over fuelling-engine.js so non-JS callers
 * (the ClaudeCoach Python bot, via lib/fuelling.py) can run the SAME physiology
 * engine the web planner uses. No logic here — it only parses argv and dispatches.
 *
 *   node fuelling-cli.js targets '{"raceHours":9.4,"bodyKg":79,...}'
 *   node fuelling-cli.js check   '{"carbGHr":75,"glucoseGHr":50,...}'
 *
 * Prints one JSON object to stdout; on error prints {"error":...} and exits 1.
 */
'use strict';
var FE = require('./fuelling-engine.js');

try {
  var cmd = process.argv[2];
  var params = JSON.parse(process.argv[3] || '{}');
  var out;
  if (cmd === 'targets') out = FE.suggestRaceTargets(params);
  else if (cmd === 'check') out = { flags: FE.checkFuellingRates(params) };
  else if (cmd === 'caps') out = FE.transporterCaps(!!params.gutTrained);
  else throw new Error('unknown command: ' + cmd + ' (use targets|check|caps)');
  process.stdout.write(JSON.stringify(out));
} catch (e) {
  process.stdout.write(JSON.stringify({ error: String(e && e.message || e) }));
  process.exit(1);
}
