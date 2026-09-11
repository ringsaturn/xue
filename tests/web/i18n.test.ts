import { describe, expect, it } from "vitest";

import { LOCALES, localeHtmlLang, normalizeLocale, type Locale } from "../../web/src/i18n";
import { de } from "../../web/src/locales/de";
import { en, type MessageKey } from "../../web/src/locales/en";
import { es } from "../../web/src/locales/es";
import { fr } from "../../web/src/locales/fr";
import { ja } from "../../web/src/locales/ja";
import { ko } from "../../web/src/locales/ko";
import { pt } from "../../web/src/locales/pt";
import { ru } from "../../web/src/locales/ru";
import { zh } from "../../web/src/locales/zh";
import { zhHant } from "../../web/src/locales/zh-hant";

/** Every dictionary, keyed the way `i18n.ts` keys them. The modules are typed
 * `Record<MessageKey, string>`, so a missing key is already a compile error;
 * these tests cover what the type cannot see — the placeholders inside the
 * strings, and that the set of locales here is the set the picker offers. */
const DICTIONARIES: Record<Locale, Record<MessageKey, string>> = {
  zh,
  "zh-Hant": zhHant,
  en,
  ja,
  ko,
  de,
  fr,
  es,
  pt,
  ru,
};

const KEYS = Object.keys(en) as MessageKey[];

function placeholders(text: string): string[] {
  return [...text.matchAll(/\{([a-zA-Z]+)\}/g)].map((match) => match[1]!).sort();
}

describe("locale detection", () => {
  it("maps the plain tags", () => {
    expect(normalizeLocale("en")).toBe("en");
    expect(normalizeLocale("ja")).toBe("ja");
    expect(normalizeLocale("ko")).toBe("ko");
    expect(normalizeLocale("de")).toBe("de");
    expect(normalizeLocale("fr")).toBe("fr");
    expect(normalizeLocale("es")).toBe("es");
    expect(normalizeLocale("pt")).toBe("pt");
    expect(normalizeLocale("ru")).toBe("ru");
  });

  it("reads the Chinese script out of the script subtag", () => {
    expect(normalizeLocale("zh-Hant")).toBe("zh-Hant");
    expect(normalizeLocale("zh-Hans")).toBe("zh");
    expect(normalizeLocale("zh-Hant-TW")).toBe("zh-Hant");
    expect(normalizeLocale("zh-Hans-HK")).toBe("zh");
  });

  it("reads the Chinese script out of the region where there is no script", () => {
    expect(normalizeLocale("zh")).toBe("zh");
    expect(normalizeLocale("zh-CN")).toBe("zh");
    expect(normalizeLocale("zh-SG")).toBe("zh");
    expect(normalizeLocale("zh-TW")).toBe("zh-Hant");
    expect(normalizeLocale("zh-HK")).toBe("zh-Hant");
    expect(normalizeLocale("zh-MO")).toBe("zh-Hant");
  });

  it("drops the region of every single-script language", () => {
    expect(normalizeLocale("pt-BR")).toBe("pt");
    expect(normalizeLocale("pt-PT")).toBe("pt");
    expect(normalizeLocale("en-GB")).toBe("en");
    expect(normalizeLocale("es-419")).toBe("es");
    expect(normalizeLocale("de-AT")).toBe("de");
  });

  it("is case-insensitive and tolerates an underscore", () => {
    expect(normalizeLocale("ZH-tw")).toBe("zh-Hant");
    expect(normalizeLocale("PT_br")).toBe("pt");
    expect(normalizeLocale("EN")).toBe("en");
  });

  it("returns null for anything it does not carry", () => {
    expect(normalizeLocale("it")).toBeNull();
    expect(normalizeLocale("nl-NL")).toBeNull();
    expect(normalizeLocale("")).toBeNull();
    expect(normalizeLocale(null)).toBeNull();
    expect(normalizeLocale(undefined)).toBeNull();
    expect(normalizeLocale("  ")).toBeNull();
  });
});

describe("the picker's list", () => {
  it("offers every locale the dictionary carries, once", () => {
    const codes = LOCALES.map((item) => item.code);
    expect([...codes].sort()).toEqual(Object.keys(DICTIONARIES).sort());
    expect(new Set(codes).size).toBe(codes.length);
  });

  it("names each language in its own script", () => {
    expect(LOCALES.find((item) => item.code === "zh")?.endonym).toBe("简体中文");
    expect(LOCALES.find((item) => item.code === "zh-Hant")?.endonym).toBe("繁體中文");
    expect(LOCALES.find((item) => item.code === "ja")?.endonym).toBe("日本語");
    expect(LOCALES.find((item) => item.code === "ko")?.endonym).toBe("한국어");
    expect(LOCALES.find((item) => item.code === "ru")?.endonym).toBe("Русский");
  });

  it("round-trips every code through <html lang> back onto itself", () => {
    for (const { code } of LOCALES) {
      expect(normalizeLocale(localeHtmlLang(code))).toBe(code);
    }
  });
});

describe("the dictionaries", () => {
  it("all carry exactly the English key set", () => {
    for (const [code, messages] of Object.entries(DICTIONARIES)) {
      expect([...Object.keys(messages)].sort(), code).toEqual([...KEYS].sort());
    }
  });

  it("leave no string empty", () => {
    for (const [code, messages] of Object.entries(DICTIONARIES)) {
      for (const key of KEYS) {
        expect(messages[key].trim().length, `${code}.${key}`).toBeGreaterThan(0);
      }
    }
  });

  it("carry every placeholder the English string names, and no others", () => {
    for (const [code, messages] of Object.entries(DICTIONARIES)) {
      for (const key of KEYS) {
        expect(placeholders(messages[key]), `${code}.${key}`).toEqual(placeholders(en[key]));
      }
    }
  });

  it("keep the rail glyphs to one character", () => {
    const glyphKeys = KEYS.filter((key) => key.startsWith("glyph"));
    expect(glyphKeys.length).toBeGreaterThan(0);
    for (const [code, messages] of Object.entries(DICTIONARIES)) {
      for (const key of glyphKeys) {
        expect([...messages[key]].length, `${code}.${key}`).toBe(1);
      }
    }
  });

  it("keep the pressure-center letters to one character, per chart convention", () => {
    for (const [code, messages] of Object.entries(DICTIONARIES)) {
      expect([...messages.centerHigh].length, `${code}.centerHigh`).toBe(1);
      expect([...messages.centerLow].length, `${code}.centerLow`).toBe(1);
      expect(messages.centerHigh, code).not.toBe(messages.centerLow);
    }
  });

  it("follow each country's own high/low convention", () => {
    expect([de.centerHigh, de.centerLow]).toEqual(["H", "T"]);
    expect([fr.centerHigh, fr.centerLow]).toEqual(["A", "D"]);
    expect([es.centerHigh, es.centerLow]).toEqual(["A", "B"]);
    expect([pt.centerHigh, pt.centerLow]).toEqual(["A", "B"]);
    expect([ru.centerHigh, ru.centerLow]).toEqual(["В", "Н"]);
    expect([ko.centerHigh, ko.centerLow]).toEqual(["고", "저"]);
    expect([ja.centerHigh, ja.centerLow]).toEqual(["高", "低"]);
    expect([zhHant.centerHigh, zhHant.centerLow]).toEqual(["高", "低"]);
  });

  it("keep the rail-tile gloss short enough for a 44px tile's accessible name", () => {
    const glossKeys: MessageKey[] = [
      "varTemp",
      "varPrecip",
      "varWind",
      "varSolar",
      "varRadar",
      "varHumidity",
      "varVapourFlux",
      "varPressure",
      "varHeight",
      "varPressureField",
      "varUnknownLayer",
    ];
    for (const [code, messages] of Object.entries(DICTIONARIES)) {
      for (const key of glossKeys) {
        expect([...messages[key]].length, `${code}.${key}`).toBeLessThanOrEqual(8);
      }
    }
  });
});
