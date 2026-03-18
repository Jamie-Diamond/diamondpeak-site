/**
 * Fetch historical weather from Open-Meteo for a ride's date and location.
 * Free API, no key required.
 */

export async function fetchRideWeather(rideData) {
  const { trackpoints } = rideData;
  if (!trackpoints || trackpoints.length === 0) return null;

  // Use the midpoint of the ride for location
  const midIdx = Math.floor(trackpoints.length / 2);
  const lat = trackpoints[midIdx].lat.toFixed(4);
  const lon = trackpoints[midIdx].lon.toFixed(4);

  // Get the ride date in YYYY-MM-DD format
  const rideDate = trackpoints[0].time;
  const dateStr = rideDate.toISOString().slice(0, 10);

  // Get ride start/end hours
  const startHour = rideDate.getUTCHours();
  const endTime = trackpoints[trackpoints.length - 1].time;
  const endHour = endTime.getUTCHours() + 1;

  const url = `https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}&hourly=wind_speed_10m,wind_direction_10m,temperature_2m&start_date=${dateStr}&end_date=${dateStr}&timezone=auto`;

  try {
    const resp = await fetch(url);
    if (!resp.ok) return null;
    const data = await resp.json();

    const hours = data.hourly.time;
    const windSpeeds = data.hourly.wind_speed_10m;
    const windDirs = data.hourly.wind_direction_10m;
    const temps = data.hourly.temperature_2m;

    // Average over the ride hours
    let speedSum = 0, dirSin = 0, dirCos = 0, tempSum = 0, count = 0;

    for (let i = 0; i < hours.length; i++) {
      const h = new Date(hours[i]).getHours();
      if (h >= startHour && h <= endHour) {
        speedSum += windSpeeds[i];
        dirSin += Math.sin(windDirs[i] * Math.PI / 180);
        dirCos += Math.cos(windDirs[i] * Math.PI / 180);
        tempSum += temps[i];
        count++;
      }
    }

    if (count === 0) return null;

    const avgSpeed = speedSum / count;
    const avgDir = ((Math.atan2(dirSin / count, dirCos / count) * 180 / Math.PI) + 360) % 360;
    const avgTemp = tempSum / count;

    const dirs = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
    const cardinal = dirs[Math.round(avgDir / 22.5) % 16];

    return {
      wind_speed_kmh: Math.round(avgSpeed * 10) / 10,
      wind_dir_deg: Math.round(avgDir),
      wind_dir_cardinal: cardinal,
      temp_C: Math.round(avgTemp * 10) / 10,
      hours_sampled: count,
      start_hour: startHour,
      end_hour: endHour,
      source: 'Open-Meteo',
    };
  } catch {
    return null;
  }
}
