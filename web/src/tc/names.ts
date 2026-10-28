/** The local names of the Western North Pacific's storms: the ESCAP/WMO
 * Typhoon Committee's list is written in each member's own script, and
 * the shell names a storm the way its readers' weather service does — the
 * China Meteorological Administration's simplified Chinese for `zh`, the
 * Central Weather Administration's (else the Hong Kong and Macao) form
 * for `zh-Hant`, the Japan Meteorological Agency's katakana for `ja`, the
 * Korea Meteorological Administration's hangul for `ko`. Every other
 * locale, and every other basin, keeps the name as the centres issue it.
 *
 * The table holds the current list and every name the archive has used
 * since 2023, retired ones included, keyed by the English name in capitals
 * with only its letters kept (`BANG-LANG` → `BANGLANG`). A name it lacks,
 * or lacks in one script, is shown as issued. Compiled 2026-10-28 from the
 * Chinese, Japanese and Korean Wikipedia lists and season articles, which
 * cite the agencies' own tables; a name added to the list later is added
 * here by hand. */

import { locale, type Locale } from "../i18n";
import { stormNameIn } from "./namestable";

/** The name to show for a storm: its local name in the shell's language
 * when it is a Western North Pacific storm the table knows, else the name
 * as issued (null stays null). Reads the live locale on every call. */
export function localStormName(name: string | null, basin: string, language: Locale = locale): string | null {
  return stormNameIn(name, basin, language);
}
