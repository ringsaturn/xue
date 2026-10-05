/**
 * Display units: what a value is shown in when that differs from the unit
 * the file carries.
 *
 * The one conversion is kelvin. A satellite brightness temperature is
 * published in the product's own kelvin (the file, its codebook and its
 * palette stops all read in K, and a reader of the store gets K), but a
 * weather map reads cloud tops as −50 °C, not 223 K, and every other
 * temperature on the shell is Celsius. The conversion is display only:
 * nothing here changes a value that is stored or compared. The other rule
 * is that a dimensionless quantity — an optical depth, whose unit the
 * encoder writes as `1` the way a CF file does — shows no unit at all.
 * The EDR's m^(2/3) s^-1, however an encoder spells the exponent in ASCII,
 * reads with the fraction glyph, short enough for the legend's unit line.
 */

const KELVIN_OFFSET = 273.15;
/** m^(2/3)/s, m^(2/3) s^-1, m2/3 s-1, m**(2/3) s**-1 and the like. */
const EDR_UNIT = /^m\s*(?:\^|\*\*)?\s*\(?2\/3\)?\s*(?:\/\s*s|s\s*(?:\^|\*\*)?\s*-1|s⁻¹)$/;

/** The unit a readout shows a file unit as. */
export function displayUnit(unit: string): string {
  if (unit === "K") return "°C";
  if (unit === "1") return "";
  if (EDR_UNIT.test(unit)) return "m⅔/s";
  return unit;
}

/** A value in the file's unit, as the readout shows it. */
export function displayValue(unit: string, value: number): number {
  return unit === "K" ? value - KELVIN_OFFSET : value;
}
