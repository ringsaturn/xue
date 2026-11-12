/** The local names of the Western North Pacific's storms: the ESCAP/WMO
 * Typhoon Committee's list is written in each member's own script, and
 * the shell names a storm the way its readers' weather service does — the
 * China Meteorological Administration's simplified Chinese for `zh`, the
 * Central Weather Administration's (else the Hong Kong and Macao) form
 * for `zh-Hant`, the Japan Meteorological Agency's katakana for `ja`, the
 * Korea Meteorological Administration's hangul for `ko`. Outside the
 * western Pacific only `zh` has local names: the CMA's for every other
 * basin's lists. Every other locale keeps the name as the centres issue it.
 *
 * The tables are keyed by the English name in capitals with only its
 * letters kept (`BANG-LANG` → `BANGLANG`). The western Pacific one holds
 * the current list and every name the archive has used since 2023, retired
 * ones included; a name it lacks, or lacks in one script, is shown as
 * issued. Its `ja`, `ko` and `zh-Hant` columns were compiled 2026-10-28 from
 * the Japanese, Korean and Chinese Wikipedia lists, which cite the
 * agencies' own tables. Every `zh` name, in every basin, is the National
 * Meteorological Centre's (nmc.cn, 台风命名: 西北太平洋 and 全球其它海域),
 * checked 2026-11-12. A name added to a list later is added here by hand. */

import { locale, type Locale } from "../i18n";
import { stormNameIn } from "./namestable";

/** The name to show for a storm: its local name in the shell's language
 * when it is a Western North Pacific storm the table knows, else the name
 * as issued (null stays null). Reads the live locale on every call. */
export function localStormName(name: string | null, basin: string, language: Locale = locale): string | null {
  return stormNameIn(name, basin, language);
}
