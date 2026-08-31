// Drive the Diamond Peak fuelling planner headlessly with Jamie's IM Italy 2026 setup.
'use strict';
const fs = require('fs');
const path = require('path');
const FE = require(path.join(__dirname, '..', '..', 'js', 'fuelling-engine.js'));
const localStorage = { getItem: () => null, setItem: () => {} }; // stub
// Extract the planner's computational core from the live calculator page
const html = fs.readFileSync(path.join(__dirname, '..', '..', 'cycling', 'fuelling-calculator.html'), 'utf8');
const startM = html.indexOf('var PRESETS');
let core = html.slice(startM, html.indexOf('function render()', startM));
// Expose internals by evaluating the core in this scope
core += `\nmodule.exports = { state, FOODS, calcRace, suggestTargets, generateStrategy, getStratLegs,
  checkPlan, calcAbsorption, calcGutModel, calcCaffeine, calcFluidTimeline, calcEnergyBalance,
  updatePreRaceOffset, getFoodById, fmtClock, fmtHM, aidStationTimes, getAllFoods };\n`;
const mod = { exports: {} };
new Function('FE', 'localStorage', 'module', core)(FE, localStorage, mod);
const P = mod.exports;
const st = P.state;

// ── Jamie's race setup ─────────────────────────────────────────────
st.preset = 'Ironman'; st.swimDist = 3.8; st.bikeDist = 180; st.runDist = 42.2;
st.swimPaceMin = 1; st.swimPaceSec = 46;         // ~1:07 swim
st.bikeSpeed = 38;                               // ~4:44 bike (predictor tapered 4:43)
st.runPace = 5.25;                               // ~3:41 run (between 3:30 target & predictor 3:46)
st.t1 = 3; st.t2 = 2;                            // ≤5:30 combined target
st.startTime = '07:30'; st.wakeTime = '04:15';
st.bodyWeight = 82.5;                            // Aug morning-series average
st.sweatRate = 1200; st.sweatNa = 1100;          // no sweat test: mid assumed band
st.gutTrained = true;                            // 25 Aug: 69 g/hr on a 32 km run; 400 g+ sessions logged
st.gelMode = 'packet';

// Custom bottles: his mixes (320 concentration cap = 320/500 ml → 1.5 sachet max in 750)
st.customFoods = [
  {id:'jd-fudge', name:'SiS GO Bar Choco Fudge', carbs:30, glucose:20, fructose:10, ml:0, na:80, caf:0, form:'solid', icon:'F'},
  {id:'jd-750',  name:'750: Maurten 320 x1 + CAF x0.5 + PH1500', carbs:120, glucose:72, fructose:48, ml:750, na:1095, caf:50,  form:'liquid', icon:'B'},
  {id:'jd-aero', name:'Aero 610: Maurten 320 + PH1500',          carbs:80,  glucose:48, fructose:32, ml:610, na:980,  caf:0,   form:'liquid', icon:'A'},
  {id:'jd-oats', name:'M&S Salted Caramel Overnight Oats',       carbs:45,  glucose:33, fructose:12, ml:0,   na:200,  caf:0,   form:'solid', meal:true, icon:'O'},
  {id:'jd-smoothie', name:'M&S Very Berry Smoothie',             carbs:48,  glucose:26, fructose:22, ml:330, na:30,   caf:0,   form:'liquid', meal:true, icon:'S'},
];
st.bikeCages = [
  {name:'Bottle 1', foodId:'jd-750',  ml:750, swap:true},
  {name:'Bottle 2', foodId:'jd-750',  ml:750, swap:true},
  {name:'Aero',     foodId:'jd-aero', ml:610, swap:false},
  {name:'Frame',    foodId:'water',   ml:400, swap:true},   // douse/refill
];
st.aidStations = [
  {km:30,discipline:'bike',type:'water'},{km:60,discipline:'bike',type:'full'},
  {km:87,discipline:'bike',type:'full'},{km:97,discipline:'bike',type:'water'},
  {km:125,discipline:'bike',type:'full'},{km:157,discipline:'bike',type:'full'},
];
st.schedule = []; st.strategies = {};
P.updatePreRaceOffset();

// ── Targets from the shared engine, then leg strategies ────────────
P.suggestTargets();
const race = P.calcRace();
const legs = P.getStratLegs(race);

// Leg food picks (his products only; liquid-only aid pickups)
const picks = {
  'Pre-Race':  ['pf30-chew','water'],
  'Bike':      ['sis-beta-chew','water'],            // cages auto-join
  'Run':       ['sis-beta-chew','water'],                       // flask + one CAF gel added below
};
for (const leg of legs) {
  const s = st.strategies[leg.name];
  if (leg.name === 'Breakfast') continue;                    // hand-built from his real foods
  s.foods = picks[leg.name] || s.foods;
  if (leg.name === 'Bike') { s.target = 95; s.cafTarget = 100; }   // bottles carry it; no tabs
  if (leg.name === 'Run')  { s.target = 85; s.cafTarget = 0; }   // one CAF gel placed manually
  P.generateStrategy(leg.name, leg.start, leg.dur, s);
}

// Breakfast: his actual training-morning foods (from nutrition log), one sitting.
st.schedule.push(
  {foodId:'jd-oats',     timeMin:-190, portion:1, fromAid:false, reason:'Breakfast: overnight oats (usual)'},
  {foodId:'jd-smoothie', timeMin:-185, portion:1, fromAid:false, reason:'Breakfast: berry smoothie (usual)'},
  {foodId:'hi5-caf-hit', timeMin:-180, portion:1, fromAid:false, reason:'Caf Hit with breakfast — 200mg, caffeine in early as usual'},
  {foodId:'banana',      timeMin:-170, portion:1, fromAid:false, reason:'Breakfast top-up'},
  {foodId:'rice-pud',    timeMin:-165, portion:0.5, fromAid:false, reason:'Breakfast top-up (half pot)'},
  {foodId:'water',       timeMin:-150, portion:1, fromAid:false, reason:'Fluid with breakfast'},
);

const runLeg  = legs.find(l => l.name === 'Run');
const bikeLeg = legs.find(l => l.name === 'Bike');

// ── Bike: hand-built. Bottles are the PRIMARY fuel (all 320g drunk), chews top up. ──
st.schedule = st.schedule.filter(e => !(e.timeMin >= bikeLeg.start && e.timeMin < bikeLeg.start+bikeLeg.dur));
const B = Math.round(bikeLeg.start);
// Each 750 lasts ~90 min: 1/6-bottle sip every 15 min. Aero over 80 min after.
for (let i=0;i<6;i++) st.schedule.push({foodId:'jd-750', timeMin:B+10+i*15, portion:1/6, fromAid:false, reason:'Sip Bottle 1 (empties ~1:25, drop at km 60)'});
for (let i=0;i<6;i++) st.schedule.push({foodId:'jd-750', timeMin:B+100+i*15, portion:1/6, fromAid:false, reason:'Sip Bottle 2 (empties ~2:55, drop at km 125)'});
for (let i=0;i<4;i++) st.schedule.push({foodId:'jd-aero', timeMin:B+190+i*17, portion:1/4, fromAid:false, reason:'Sip Aero (empty by ~4:05, carb-free run-in to T2)'});
// Choco fudge bar in hour 1 (gut freshest), then half a Beta Fuel chew hourly
st.schedule.push({foodId:'jd-fudge', timeMin:B+35, portion:1, fromAid:false, reason:'Choco fudge bar — solid food early'});
for (const m of [75,135,195,255]) st.schedule.push({foodId:'sis-beta-chew', timeMin:B+m, portion:0.5, fromAid:false, reason:'Half chew — hourly top-up'});
// Swim-loss payback: drain the frame 400ml over the first half hour
st.schedule.push({foodId:'water', timeMin:B+12, portion:0.4, fromAid:false, reason:'Frame bottle — pay back swim sweat'});
st.schedule.push({foodId:'water', timeMin:B+26, portion:0.4, fromAid:false, reason:'Frame bottle — finish it, refill at km 30 for dousing'});
// Aid-station water / PH1000: drink ~250-350ml at each of the 6 stations
const stMin=[47,95,137,153,197,248];
stMin.forEach((m,i)=>st.schedule.push({foodId:(i===1||i===3||i===5)?'jd-ph-bottle':'water', timeMin:B+m, portion:1.0, fromAid:true, reason:'Aid-station pickup: drink, douse the rest'}));


// Clear the gut before T2: no carbs in the final 25 min of the bike
st.schedule = st.schedule.filter(e => {
  const f = P.getFoodById(e.foodId);
  return !(f && f.carbs>0 && e.timeMin >= bikeLeg.start+bikeLeg.dur-25 && e.timeMin < bikeLeg.start+bikeLeg.dur);
});

// Run flask: 500 ml plain 320 from T2, sipped and DITCHED by ~25 min (his call).
// Remove any generated carb items inside the flask window so the gut isn't doubled up.
st.schedule = st.schedule.filter(e => {
  const f = P.getFoodById(e.foodId);
  return !(f && f.carbs>0 && e.timeMin >= runLeg.start && e.timeMin < runLeg.start+32);
});
// Thin generated run chews to ~85 g/hr: hand-placed, every 16 min then 20 min
st.schedule = st.schedule.filter(e => {
  const f = P.getFoodById(e.foodId);
  return !(f && f.id==='sis-beta-chew' && e.timeMin >= runLeg.start);
});
for (const m of [36,52,84,100,132,164,196]) st.schedule.push({foodId:'sis-beta-chew', timeMin:Math.round(runLeg.start)+m, portion:0.5, fromAid:false, reason:'Half chew'});
for (const m of [68,116]) st.schedule.push({foodId:'pf30-chew', timeMin:Math.round(runLeg.start)+m, portion:1, fromAid:false, reason:'PF 30 chew pack'});
st.schedule.push(
  {foodId:'maurten-d320caf', timeMin: Math.round(runLeg.start+4),  portion: 1/3, fromAid:false, reason:'Flask sip 1 (T2 flask, 500ml 320 CAF)'},
  {foodId:'maurten-d320caf', timeMin: Math.round(runLeg.start+14), portion: 1/3, fromAid:false, reason:'Flask sip 2'},
  {foodId:'maurten-d320caf', timeMin: Math.round(runLeg.start+24), portion: 1/3, fromAid:false, reason:'Flask sip 3 — ditch flask'},
);


// Run sodium: PH1000 cups from aid stations instead of every other water
// (his pickup plan; salt caps in pocket only as backup).
st.customFoods.push({id:'jd-ph-cup', name:'PH1000 cup (aid station, ~200ml)', carbs:0, glucose:0, fructose:0, ml:200, na:200, caf:0, form:'liquid', icon:'P'});
let wCount=0;
for (const e of st.schedule) {
  const f=P.getFoodById(e.foodId);
  if (f && f.id==='water' && e.timeMin>runLeg.start+30) {
    wCount++;
    if (wCount%2===1){e.foodId='jd-ph-cup';e.portion=1;e.reason='PH1000 cup at aid station (sodium)';}
  }
}

st.customFoods.push({id:'jd-ph-bottle', name:'PH1000 course bottle (500ml pickup)', carbs:0, glucose:0, fructose:0, ml:500, na:500, caf:0, form:'liquid', icon:'P'});
let bw=0;
for (const e of st.schedule) {
  const f=P.getFoodById(e.foodId);
  if (f && f.id==='water' && e.timeMin>bikeLeg.start+80 && e.timeMin<bikeLeg.start+bikeLeg.dur) {
    bw++;
    if (bw%3===0){e.foodId='jd-ph-bottle';e.portion=0.5;e.reason='PH1000 pickup — drink half, cage the rest';}
  }
}

// Model bottle doses as two spaced sips (real drinking, not a 375ml gulp).
const split=[];
for (const e of st.schedule) {
  const f=P.getFoodById(e.foodId);
  if (f && (f.id==='jd-750'||f.id==='jd-aero') && e.portion>0.3) {
    split.push({...e, portion:e.portion/2},
               {...e, timeMin:e.timeMin+6, portion:e.portion/2, reason:'(second half of the sip block)'});
  } else split.push(e);
}
st.schedule=split;

// ── Report ──────────────────────────────────────────────────────────
st.schedule.sort((a,b)=>a.timeMin-b.timeMin);
const abs = P.calcAbsorption(race.total), gm = P.calcGutModel(race.total),
      caf = P.calcCaffeine(race.total), fl = P.calcFluidTimeline(race.total),
      en  = P.calcEnergyBalance(race);

console.log('RACE:', JSON.stringify({swim:P.fmtHM(race.swimMin), bike:P.fmtHM(race.bikeMin), run:P.fmtHM(race.runMin), total:P.fmtHM(race.total)}));
console.log('\nTARGETS PER LEG:');
for (const leg of legs) {
  const s = st.strategies[leg.name];
  console.log(` ${leg.name}: carbs ${s.target}g${leg.name==='Bike'||leg.name==='Run'?'/hr':''} fluid ${s.fluidTarget} na ${s.sodiumTarget} caf ${s.cafTarget}`);
  if (s.pattern) console.log('   pattern:', s.pattern);
}
console.log('\nSCHEDULE (clock | race-time | item | portion | reason):');
let legTotals = {};
for (const e of st.schedule) {
  const f = P.getFoodById(e.foodId); if (!f) continue;
  const legName = e.timeMin < -60 ? 'Breakfast' : e.timeMin < 0 ? 'Pre-Race' :
    race.segs.find(sg => e.timeMin >= sg.start && e.timeMin < sg.start+sg.dur)?.name || '?';
  const t = legTotals[legName] = legTotals[legName] || {c:0,ml:0,na:0,caf:0};
  const p = e.portion||1;
  t.c += f.carbs*p; t.ml += (f.ml||0)*p; t.na += (f.na||0)*p; t.caf += (f.caf||0)*p;
  const pct = p===1?'1':p>=0.66?'⅔':p>=0.49?'½':p>=0.32?'⅓':p>=0.24?'¼':(''+Math.round(p*100)+'%');
  console.log(` ${P.fmtClock(e.timeMin)} | ${e.timeMin<0?'-':''}${P.fmtHM(Math.abs(e.timeMin))} | ${f.name} ×${pct} | ${Math.round(f.carbs*p)}g | ${e.reason.slice(0,80)}`);
}
console.log('\nLEG TOTALS:');
for (const [k,v] of Object.entries(legTotals)) {
  const legDef = legs.find(l=>l.name===k);
  const hrs = legDef ? legDef.dur/60 : 1;
  console.log(` ${k}: ${Math.round(v.c)}g carbs (${Math.round(v.c/hrs)} g/hr) | ${Math.round(v.ml)}ml (${Math.round(v.ml/hrs)}/hr) | ${Math.round(v.na)}mg Na (${Math.round(v.na/hrs)}/hr) | ${Math.round(v.caf)}mg caf`);
}
console.log('\nPLAN CHECK:');
for (const c of P.checkPlan(race, abs, gm)) console.log(` [${c.level}] ${c.text}`);

// Gut backlog peak, caffeine plasma at key moments, energy deficit
let peakB=0, peakT=0;
for (let m=0;m<gm.minutes;m++) if (gm.backlog[m]>peakB){peakB=gm.backlog[m];peakT=m;}
console.log('\nGUT: peak backlog', Math.round(peakB)+'g at', P.fmtClock(peakT-195));
const OFF=195;
const keyPts = [['start gun',0],['bike start',race.segs.find(s=>s.name==='Bike').start],['run start',runLeg.start],['run halfway',runLeg.start+runLeg.dur/2],['finish',race.total-1]];
console.log('CAFFEINE PLASMA (mg circulating):');
for (const [lbl,t] of keyPts) console.log(` ${lbl}: ${Math.round(caf.plasma[Math.round(t)+OFF])}mg`);
console.log('\nENERGY: burned', Math.round(en.totalBurned), 'kcal | carb intake', Math.round(en.totalConsumed), 'kcal | deficit', Math.round(en.totalBurned-en.totalConsumed));
console.log('\nAID STATIONS (bike):');
for (const a of P.aidStationTimes(race)) console.log(` km ${a.km} at ${P.fmtHM(a.timeMin-race.segs.find(s=>s.name==='Bike').start)} into bike (${P.fmtClock(a.timeMin)}) [${a.type}]`);
