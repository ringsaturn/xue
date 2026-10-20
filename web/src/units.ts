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
 * reads with the fraction glyph, short enough for the legend's unit line,
 * and a volume or an area per anything takes its superscript (m³/s, km²).
 */

import type { BundleVariable } from "./manifest";

const KELVIN_OFFSET = 273.15;
/** m^(2/3)/s, m^(2/3) s^-1, m2/3 s-1, m**(2/3) s**-1 and the like. */
const EDR_UNIT = /^m\s*(?:\^|\*\*)?\s*\(?2\/3\)?\s*(?:\/\s*s|s\s*(?:\^|\*\*)?\s*-1|s⁻¹)$/;

/** m3/s, m3 s-1, m**3 s**-1, m^3/s. */
const CUBIC_METRES_PER_SECOND = /^m\s*(?:\^|\*\*)?\s*3\s*(?:\/\s*s|s\s*(?:\^|\*\*)?\s*-1|s⁻¹)$/;
/** km2, km^2, km**2. */
const SQUARE_KILOMETRES = /^km\s*(?:\^|\*\*)?\s*2$/;

/** The unit a readout shows a file unit as. */
export function displayUnit(unit: string): string {
  if (unit === "K") return "°C";
  if (unit === "1") return "";
  if (EDR_UNIT.test(unit)) return "m⅔/s";
  if (CUBIC_METRES_PER_SECOND.test(unit)) return "m³/s";
  if (SQUARE_KILOMETRES.test(unit)) return "km²";
  return unit;
}

/** Whether a variable is river discharge, by its parameter block
 * (hydrology 1/0/7, `identity.ts`). */
function isDischarge(variable: BundleVariable): boolean {
  const parameter = variable.parameter;
  return parameter !== undefined && parameter.discipline === 1 && parameter.parameterCategory === 0 && parameter.parameterNumber === 7;
}

/** A value as a readout prints it, with the digits worth showing: a linear
 * codebook resolves exactly as far as its step, and a logarithmic one
 * resolves light rain much more finely than heavy. River discharge reads
 * whole above 100 m³/s and to a tenth below, as a hydrograph is read: a
 * brook's 3.4 m³/s means something, a river's 1240.5 does not. */
export function formatValueReadout(variable: BundleVariable, value: number): string {
  if (isDischarge(variable)) return value.toFixed(value >= 100 ? 0 : 1);
  if (variable.quantization.type === "linear") {
    const step = variable.quantization.scale;
    return value.toFixed(step >= 1 ? 0 : step >= 0.1 ? 1 : 2);
  }
  return value.toFixed(value >= 100 ? 0 : value >= 10 ? 1 : 2);
}

/** A value in the file's unit, as the readout shows it. */
export function displayValue(unit: string, value: number): number {
  return unit === "K" ? value - KELVIN_OFFSET : value;
}
