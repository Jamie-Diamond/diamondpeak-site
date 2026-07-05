/*
 * fuelling-engine.js — Diamond Peak Race Fuelling physiology engine.
 *
 * SINGLE SOURCE OF TRUTH for the fuelling maths. Loaded two ways:
 *   • the browser (cycling/fuelling-calculator.html) as window.FuellingEngine
 *   • Node.js (ClaudeCoach bot, via lib/fuelling.py) as require(...)
 * so the web planner and the coach bot always give identical advice.
 *
 * Pure functions only — no DOM, no I/O. Constants follow Jeukendrup
 * carbohydrate-oxidation research (multiple transportable carbs), the
 * ACSM/IOC hydration and sodium positions, and caffeine ergogenic-dose
 * consensus. Change a number here and BOTH consumers change together.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api; // Node
  if (root) root.FuellingEngine = api;                                       // browser
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // Intestinal carbohydrate absorption caps (g/hr). Glucose is taken up via
  // SGLT1, fructose via GLUT5 — separate transporters, so the two add. Standard
  // guts manage ~60/30 (90 total); gut-trained athletes reach ~72/48 (120).
  function transporterCaps(gutTrained) {
    return gutTrained ? { glu: 72, fru: 48 } : { glu: 60, fru: 30 };
  }

  // Race carb rate (g/hr) with headroom below the combined transporter cap.
  // Untrained guts top out ~75 g/hr for long races; 90-110 is gut-trained only.
  function suggestCarbRate(raceHours, gutTrained) {
    var caps = transporterCaps(gutTrained), maxRate = caps.glu + caps.fru;
    var rH = raceHours;
    var base = rH < 1 ? 30 : rH < 1.5 ? 45 : rH < 2 ? 60 : 75;
    if (gutTrained && rH >= 2) base = rH >= 2.5 ? 110 : 90;
    return Math.min(base, maxRate - 5);
  }

  // Fluid rate (ml/hr) holding total loss near 1.5% of body weight, capped at a
  // gut-tolerable ceiling and never above sweat rate (avoid over-drinking).
  function suggestFluidRate(raceHours, bodyKg, sweatMlHr) {
    var allow = raceHours > 0 ? (bodyKg * 0.015 * 1000) / raceHours : 0;
    var fl = sweatMlHr - allow;
    fl = Math.max(sweatMlHr * 0.5, Math.min(fl, sweatMlHr, 900));
    return Math.round(fl / 50) * 50;
  }

  // Sodium rate (mg/hr) replacing ~65% of sweat-sodium losses (range 50-80%).
  function suggestSodiumRate(sweatMlHr, sweatNaMgL) {
    return Math.min(1500, Math.round(sweatNaMgL * sweatMlHr / 1000 * 0.65 / 50) * 50);
  }

  // Full evidence-based race fuelling targets. All inputs optional except hours.
  function suggestRaceTargets(o) {
    o = o || {};
    var rH = o.raceHours || 0;
    var wt = o.bodyKg || 75;
    var sweat = o.sweatMlHr || 1000;
    var sweatNa = o.sweatNaMgL || 950;
    var gt = !!o.gutTrained;
    var caps = transporterCaps(gt);
    var carb = suggestCarbRate(rH, gt);
    var runCarb = Math.max(30, Math.round(carb * 0.85 / 5) * 5);
    var caffeine = Math.round(wt * 4); // 4 mg/kg, mid ergogenic range
    return {
      raceHours: Math.round(rH * 100) / 100,
      gutTrained: gt,
      capsGHr: { glucose: caps.glu, fructose: caps.fru, combined: caps.glu + caps.fru },
      bikeCarbGHr: carb,
      runCarbGHr: runCarb,
      fluidMlHr: suggestFluidRate(rH, wt, sweat),
      sodiumMgHr: suggestSodiumRate(sweat, sweatNa),
      caffeineTotalMg: caffeine,
      caffeineMgKg: Math.round(caffeine / wt * 10) / 10,
      carbSplitHint: 'glucose:fructose ~2:1 up to 1:0.8 when above 60 g/hr'
    };
  }

  // Red-flag review of an intended fuelling RATE against the evidence:
  // glucose transporter cap, glucose:fructose ratio, hydration, sodium,
  // caffeine. (Gut-backlog and fuelling-gap checks need a minute-by-minute
  // schedule and live in the web planner only.) Returns [{level,text}],
  // level = ok|warn|risk.
  function checkFuellingRates(o) {
    o = o || {};
    var carb = o.carbGHr || 0, glu = o.glucoseGHr || 0, fru = o.fructoseGHr || 0;
    var fluid = o.fluidMlHr || 0, sodium = o.sodiumMgHr || 0, caf = o.caffeineTotalMg || 0;
    var rH = o.raceHours || 0, wt = o.bodyKg || 75;
    var sweat = o.sweatMlHr || 1000, sweatNa = o.sweatNaMgL || 950, gt = !!o.gutTrained;
    var caps = transporterCaps(gt), out = [];
    // 1. Glucose over the SGLT1 cap
    if (glu > caps.glu * 1.05)
      out.push({ level: 'risk', text: 'Glucose ' + Math.round(glu) + ' g/hr is over the ~' + caps.glu +
        ' g/hr SGLT1 cap. Swap glucose-heavy items for glucose+fructose mixes.' });
    // 2. Glucose:fructose ratio when fuelling high
    if (carb > 62 && (glu + fru) > 0) {
      var fruShare = fru / (glu + fru);
      if (fruShare < 0.2)
        out.push({ level: 'warn', text: 'Only ' + Math.round(fruShare * 100) +
          '% fructose. Above 60 g/hr aim for ~2:1 to 1:0.8 glucose:fructose.' });
      else if (fruShare > 0.5)
        out.push({ level: 'warn', text: Math.round(fruShare * 100) +
          '% fructose is beyond the 1:0.8 the gut can use — shift toward glucose.' });
    }
    // 3. Hydration vs 2-3% body-weight loss
    var lossPct = (sweat - fluid) * rH / 1000 / wt * 100;
    if (lossPct > 3.5)
      out.push({ level: 'risk', text: 'Projected fluid deficit ~' + lossPct.toFixed(1) + '% of body weight (' +
        Math.round(fluid) + ' vs ' + Math.round(sweat) + ' ml/hr sweat). Add fluid.' });
    else if (lossPct > 2.5)
      out.push({ level: 'warn', text: 'Projected fluid deficit ~' + lossPct.toFixed(1) +
        '% of body weight. OK in cool conditions; tight in heat.' });
    if (fluid > sweat * 1.25)
      out.push({ level: 'warn', text: 'Drinking ' + Math.round(fluid) + ' ml/hr vs ' + Math.round(sweat) +
        ' ml/hr sweat — over-drinking risks hyponatraemia.' });
    // 4. Sodium vs sweat losses (races > 2 hr)
    var naLoss = sweatNa * sweat / 1000;
    if (rH > 2 && sodium < naLoss * 0.4)
      out.push({ level: 'warn', text: 'Sodium ' + Math.round(sodium) + ' mg/hr vs ~' + Math.round(naLoss) +
        ' mg/hr sweat losses. Over 2 hr aim to replace 50-80%.' });
    // 5. Caffeine vs 3-6 mg/kg
    if (caf > 0) {
      var mgKg = caf / wt;
      if (mgKg < 2.5)
        out.push({ level: 'warn', text: 'Caffeine ' + Math.round(caf) + ' mg (' + mgKg.toFixed(1) +
          ' mg/kg) is below the ergogenic 3-6 mg/kg.' });
      else if (mgKg > 6)
        out.push({ level: 'risk', text: 'Caffeine ' + Math.round(caf) + ' mg (' + mgKg.toFixed(1) +
          ' mg/kg) is above 3-6 mg/kg — jitters and GI upset likely.' });
    }
    if (out.length === 0)
      out.push({ level: 'ok', text: 'No red flags: carb rate, glucose:fructose ratio, hydration, sodium and caffeine are all in range.' });
    return out;
  }

  return {
    transporterCaps: transporterCaps,
    suggestCarbRate: suggestCarbRate,
    suggestFluidRate: suggestFluidRate,
    suggestSodiumRate: suggestSodiumRate,
    suggestRaceTargets: suggestRaceTargets,
    checkFuellingRates: checkFuellingRates
  };
});
