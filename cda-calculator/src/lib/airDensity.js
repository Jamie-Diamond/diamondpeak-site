/**
 * Compute air density from elevation and temperature.
 * ρ = 1.225 × (1 - 0.0000226 × elevation)^5.256 × (288 / (273 + temp_C))
 */
export function airDensity(elevation_m, temp_C) {
  return (
    1.225 *
    Math.pow(1 - 0.0000226 * elevation_m, 5.256) *
    (288 / (273 + temp_C))
  );
}
