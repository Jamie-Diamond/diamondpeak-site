/**
 * Parse GPX XML into structured trackpoints with derived values.
 */

const DEG2RAD = Math.PI / 180;
const R_EARTH = 6371000; // metres

function haversine(lat1, lon1, lat2, lon2) {
  const dLat = (lat2 - lat1) * DEG2RAD;
  const dLon = (lon2 - lon1) * DEG2RAD;
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1 * DEG2RAD) * Math.cos(lat2 * DEG2RAD) * Math.sin(dLon / 2) ** 2;
  return R_EARTH * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function bearing(lat1, lon1, lat2, lon2) {
  const dLon = (lon2 - lon1) * DEG2RAD;
  const y = Math.sin(dLon) * Math.cos(lat2 * DEG2RAD);
  const x =
    Math.cos(lat1 * DEG2RAD) * Math.sin(lat2 * DEG2RAD) -
    Math.sin(lat1 * DEG2RAD) * Math.cos(lat2 * DEG2RAD) * Math.cos(dLon);
  return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
}

function extractTemp(trkpt) {
  const extensions = trkpt.getElementsByTagName('extensions')[0];
  if (!extensions) return null;

  const tags = ['gpxtpx:atemp', 'ns3:atemp', 'atemp'];
  for (const tag of tags) {
    const el = extensions.getElementsByTagName(tag)[0];
    if (el && el.textContent) {
      const v = parseFloat(el.textContent);
      if (!isNaN(v)) return v;
    }
  }
  // Check nested (e.g. TrackPointExtension)
  for (const child of extensions.childNodes) {
    if (child.nodeType !== 1) continue;
    for (const sub of child.childNodes) {
      if (sub.nodeType !== 1) continue;
      const subName = sub.localName || sub.nodeName.split(':').pop();
      if (subName === 'atemp') {
        const v = parseFloat(sub.textContent);
        if (!isNaN(v)) return v;
      }
    }
  }
  return null;
}

function extractPower(trkpt) {
  const extensions = trkpt.getElementsByTagName('extensions')[0];
  if (!extensions) return null;

  const tags = [
    'gpxtpx:PowerInWatts',
    'ns3:PowerInWatts',
    'PowerInWatts',
    'power',
  ];
  for (const tag of tags) {
    const el = extensions.getElementsByTagName(tag)[0];
    if (el && el.textContent) {
      const v = parseFloat(el.textContent);
      if (!isNaN(v)) return v;
    }
  }
  // Also check direct children with local name matching
  for (const child of extensions.childNodes) {
    if (child.nodeType !== 1) continue;
    const localName = child.localName || child.nodeName.split(':').pop();
    if (localName === 'PowerInWatts' || localName === 'power') {
      const v = parseFloat(child.textContent);
      if (!isNaN(v)) return v;
    }
    // Check nested (e.g. TrackPointExtension)
    for (const sub of child.childNodes) {
      if (sub.nodeType !== 1) continue;
      const subName = sub.localName || sub.nodeName.split(':').pop();
      if (subName === 'PowerInWatts' || subName === 'power') {
        const v = parseFloat(sub.textContent);
        if (!isNaN(v)) return v;
      }
    }
  }
  return null;
}

function rollingAverage(arr, windowSize) {
  const half = Math.floor(windowSize / 2);
  const result = new Array(arr.length);
  for (let i = 0; i < arr.length; i++) {
    let sum = 0;
    let count = 0;
    for (let j = Math.max(0, i - half); j <= Math.min(arr.length - 1, i + half); j++) {
      if (arr[j] != null) {
        sum += arr[j];
        count++;
      }
    }
    result[i] = count > 0 ? sum / count : 0;
  }
  return result;
}

export function parseGPX(xmlString, filename) {
  const parser = new DOMParser();
  const doc = parser.parseFromString(xmlString, 'text/xml');

  const parserError = doc.querySelector('parsererror');
  if (parserError) {
    throw new Error('Invalid GPX file: XML parse error');
  }

  const trkpts = doc.getElementsByTagName('trkpt');
  if (trkpts.length === 0) {
    throw new Error('No trackpoints found in GPX file');
  }

  // Extract raw points
  const raw = [];
  let powerCount = 0;
  const temps = [];
  for (const pt of trkpts) {
    const lat = parseFloat(pt.getAttribute('lat'));
    const lon = parseFloat(pt.getAttribute('lon'));
    const eleEl = pt.getElementsByTagName('ele')[0];
    const ele = eleEl ? parseFloat(eleEl.textContent) : 0;
    const timeEl = pt.getElementsByTagName('time')[0];
    const time = timeEl ? new Date(timeEl.textContent) : null;
    const power = extractPower(pt);
    if (power !== null) powerCount++;
    const temp = extractTemp(pt);
    if (temp !== null) temps.push(temp);

    if (time && !isNaN(lat) && !isNaN(lon)) {
      raw.push({ lat, lon, ele: isNaN(ele) ? 0 : ele, time, power });
    }
  }

  const hasPower = powerCount / raw.length > 0.5;
  const hasTemp = temps.length > raw.length * 0.5;
  const meanTemp_C = hasTemp ? temps.reduce((s, v) => s + v, 0) / temps.length : null;

  // Compute deltas
  const points = [];
  for (let i = 0; i < raw.length; i++) {
    const pt = { ...raw[i] };
    if (i < raw.length - 1) {
      pt.distance_delta = haversine(raw[i].lat, raw[i].lon, raw[i + 1].lat, raw[i + 1].lon);
      pt.time_delta = (raw[i + 1].time - raw[i].time) / 1000;
    } else {
      pt.distance_delta = 0;
      pt.time_delta = 0;
    }
    points.push(pt);
  }

  // Filter out bad time deltas
  const filtered = points.filter(
    (p) => p.time_delta >= 0.5 && p.time_delta <= 5
  );
  // Keep last point always
  if (points.length > 0 && !filtered.includes(points[points.length - 1])) {
    filtered.push(points[points.length - 1]);
  }

  // Compute raw ground speed
  const rawSpeed = filtered.map((p) =>
    p.time_delta > 0 ? p.distance_delta / p.time_delta : 0
  );

  // Rolling 5-point centred average for speed
  const smoothSpeed = rollingAverage(rawSpeed, 5);

  // 10-point smoothed power (like cycling's "10s power") for steadier segment detection
  const rawPowers = filtered.map((p) => p.power);
  const smoothPower = rollingAverage(rawPowers, 10);

  // Compute raw gradient
  const rawGradient = [];
  for (let i = 0; i < filtered.length; i++) {
    const lo = Math.max(0, i - 2);
    const hi = Math.min(filtered.length - 1, i + 2);
    let distSum = 0;
    for (let j = lo; j < hi; j++) {
      distSum += filtered[j].distance_delta;
    }
    const eleChange = filtered[hi].ele - filtered[lo].ele;
    rawGradient.push(distSum > 0 ? eleChange / distSum : 0);
  }

  // 10-point smoothed gradient
  const smoothGradient = rollingAverage(rawGradient, 10);

  // Compute bearing
  const bearings = [];
  for (let i = 0; i < filtered.length - 1; i++) {
    bearings.push(bearing(filtered[i].lat, filtered[i].lon, filtered[i + 1].lat, filtered[i + 1].lon));
  }
  bearings.push(bearings.length > 0 ? bearings[bearings.length - 1] : 0);

  // Assemble trackpoints
  const startTime = filtered[0].time;
  let cumDist = 0;
  let elevGain = 0;

  const trackpoints = filtered.map((p, i) => {
    cumDist += i > 0 ? filtered[i - 1].distance_delta : 0;
    if (i > 0 && filtered[i].ele > filtered[i - 1].ele) {
      elevGain += filtered[i].ele - filtered[i - 1].ele;
    }
    return {
      lat: p.lat,
      lon: p.lon,
      ele: p.ele,
      time: p.time,
      power: p.power,
      power_smooth: smoothPower[i],
      distance_delta: p.distance_delta,
      time_delta: p.time_delta,
      v_ground: smoothSpeed[i],
      gradient: smoothGradient[i],
      bearing: bearings[i],
      elapsed_s: (p.time - startTime) / 1000,
      cumulative_dist: cumDist,
    };
  });

  const lastPt = trackpoints[trackpoints.length - 1];
  return {
    filename,
    hasPower,
    hasTemp,
    meanTemp_C,
    pointCount: trackpoints.length,
    durationS: lastPt ? lastPt.elapsed_s : 0,
    distanceM: cumDist + (lastPt ? lastPt.distance_delta : 0),
    elevationGainM: elevGain,
    trackpoints,
  };
}
