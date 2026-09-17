/**
 * Display units: what a value is shown in when that differs from the unit
 * the file carries.
 *
 * The one rule so far is kelvin. A satellite brightness temperature is
 * published in the product's own kelvin (the file, its codebook and its
 * palette stops all read in K, and a reader of the store gets K), but a
 * weather map reads cloud tops as −50 °C, not 223 K, and every other
 * temperature on the shell is Celsius. The conversion is display only:
 * nothing here changes a value that is stored or compared.
 */

const KELVIN_OFFSET = 273.15;

/** The unit a readout shows a file unit as. */
export function displayUnit(unit: string): string {
  return unit === "K" ? "°C" : unit;
}

/** A value in the file's unit, as the readout shows it. */
export function displayValue(unit: string, value: number): number {
  return unit === "K" ? value - KELVIN_OFFSET : value;
}
