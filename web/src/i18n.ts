/** UI locale support: eleven languages. The locale is resolved before the first
 * render — detection order is the `?lang=` URL param, then the choice the
 * picker stored on this device, then the browser language — and the basemap
 * label language and `<html lang>` follow it. Only the URL param is ever
 * explicit; a shared link carries none unless written by hand, so it opens
 * in each reader's own.
 *
 * Picking another language never reloads: `setLocale` swaps the dictionary,
 * rewrites the static markup and lets every `onLocaleChange` listener redo
 * the copy it owns, so the session keeps its decoded frames and playhead.
 * `locale`, `htmlLang` and `basemapLang` are live bindings — read them when
 * needed rather than capturing them at module load.
 *
 * The dictionary itself lives in `locales/`, one module per language, each
 * typed `Record<MessageKey, string>` against `locales/en.ts` — the source of
 * truth, and the only file carrying the design notes on what a string has to
 * fit. Every module is imported statically: the locale is settled before the
 * first render and `t` is synchronous, so there is nothing to await.
 *
 * Technical diagnostics (thrown Error messages, the worker, debug info) stay
 * English in every locale; only human-facing UI copy lives here. */

import { de } from "./locales/de";
import { en, type MessageKey } from "./locales/en";
import { es } from "./locales/es";
import { fr } from "./locales/fr";
import { ja } from "./locales/ja";
import { ko } from "./locales/ko";
import { pt } from "./locales/pt";
import { ru } from "./locales/ru";
import { tr } from "./locales/tr";
import { zh } from "./locales/zh";
import { zhHant } from "./locales/zh-hant";

export type { MessageKey };

export type Locale = "zh" | "zh-Hant" | "en" | "ja" | "ko" | "de" | "fr" | "es" | "pt" | "tr" | "ru";

const STORAGE_KEY = "xue-locale";

interface LocaleDefinition {
  /** The language's own name, in its own script. Never translated: a picker
   * of eleven languages is only usable if each row reads as itself. */
  endonym: string;
  /** Value for `<html lang>`, and the Intl locale derived from it. */
  htmlLang: string;
  /** Protomaps `name:*` label language. Every code here is one the
   * `@protomaps/basemaps` `language_script_pairs` table declares, so the
   * basemap's own labels follow the UI in all eleven. */
  basemapLang: string;
  messages: Record<MessageKey, string>;
}

/** The eleven languages, in the order the picker lists them: the two Chinese
 * scripts and English first (the project's own), then the rest by script and
 * proximity — the Latin-script ones together, Turkish closing them before
 * Cyrillic. */
const DEFINITIONS: Record<Locale, LocaleDefinition> = {
  zh: { endonym: "简体中文", htmlLang: "zh-CN", basemapLang: "zh-Hans", messages: zh },
  "zh-Hant": { endonym: "繁體中文", htmlLang: "zh-TW", basemapLang: "zh-Hant", messages: zhHant },
  en: { endonym: "English", htmlLang: "en", basemapLang: "en", messages: en },
  ja: { endonym: "日本語", htmlLang: "ja", basemapLang: "ja", messages: ja },
  ko: { endonym: "한국어", htmlLang: "ko", basemapLang: "ko", messages: ko },
  de: { endonym: "Deutsch", htmlLang: "de", basemapLang: "de", messages: de },
  fr: { endonym: "Français", htmlLang: "fr", basemapLang: "fr", messages: fr },
  es: { endonym: "Español", htmlLang: "es", basemapLang: "es", messages: es },
  pt: { endonym: "Português", htmlLang: "pt", basemapLang: "pt", messages: pt },
  tr: { endonym: "Türkçe", htmlLang: "tr", basemapLang: "tr", messages: tr },
  ru: { endonym: "Русский", htmlLang: "ru", basemapLang: "ru", messages: ru },
};

/** What the picker lists, in order. */
export const LOCALES: readonly { code: Locale; endonym: string }[] = (
  Object.keys(DEFINITIONS) as Locale[]
).map((code) => ({ code, endonym: DEFINITIONS[code].endonym }));

/** The `<html lang>` tag of any of the eleven — what the picker stamps on each
 * row so a screen reader reads each endonym in its own language. */
export function localeHtmlLang(code: Locale): string {
  return DEFINITIONS[code].htmlLang;
}

/** Chinese is the one language here written in two scripts, and a tag names
 * the script either directly (`zh-Hant`) or through a region. Taiwan, Hong
 * Kong and Macau write traditional; the mainland and Singapore simplified. */
const TRADITIONAL_REGIONS = new Set(["tw", "hk", "mo"]);

/** Map a BCP-47 tag onto one of the eleven, or null. Case-insensitive, and
 * exported for the unit tests and for the canonical-URL logic. */
export function normalizeLocale(value: string | null | undefined): Locale | null {
  if (!value) return null;
  const parts = value.trim().toLowerCase().split(/[-_]/).filter(Boolean);
  const language = parts[0];
  if (!language) return null;
  if (language === "zh") {
    // The script subtag wins where there is one; otherwise the region says
    // which script the reader expects, and a bare `zh` means simplified.
    if (parts.includes("hant")) return "zh-Hant";
    if (parts.includes("hans")) return "zh";
    return parts.some((part) => TRADITIONAL_REGIONS.has(part)) ? "zh-Hant" : "zh";
  }
  // Every other language here has one script, so the region is dropped:
  // pt-BR and pt-PT are both `pt`, en-GB is `en`.
  return language in DEFINITIONS ? (language as Locale) : null;
}

function detectLocale(): Locale {
  // Unit tests import this module under jsdom/node; never require a browser.
  if (typeof window === "undefined") return "en";
  const fromUrl = normalizeLocale(new URLSearchParams(window.location.search).get("lang"));
  if (fromUrl) return fromUrl;
  try {
    const stored = normalizeLocale(localStorage.getItem(STORAGE_KEY));
    if (stored) return stored;
  } catch {
    // Storage can be unavailable (privacy modes); browser language decides.
  }
  for (const tag of navigator.languages ?? [navigator.language]) {
    const match = normalizeLocale(tag);
    if (match) return match;
  }
  return "en";
}

export let locale: Locale = detectLocale();

let active = DEFINITIONS[locale];

/** Language for the Protomaps basemap labels — the map follows the UI. */
export let basemapLang: string = active.basemapLang;

/** Value for <html lang>, and the tag every Intl formatter is built on. */
export let htmlLang: string = active.htmlLang;

const listeners = new Set<() => void>();

/** Run `listener` after every language switch, once the dictionary and the
 * static markup already read in the new language; the listener rewrites
 * whatever copy it composed itself. */
export function onLocaleChange(listener: () => void): void {
  listeners.add(listener);
}

export function t(key: MessageKey, params?: Record<string, string | number>): string {
  let text: string = active.messages[key];
  if (params) {
    for (const [name, value] of Object.entries(params)) {
      text = text.replace(`{${name}}`, String(value));
    }
  }
  return text;
}

/** Rewrites every element carrying data-i18n / data-i18n-aria /
 * data-i18n-content / data-i18n-tip from the dictionary, and stamps <html
 * lang> and the meta description. The markup ships the English copy as its
 * pre-JS fallback. */
export function applyStaticMessages(): void {
  document.documentElement.lang = htmlLang;
  for (const element of document.querySelectorAll<HTMLElement>("[data-i18n]")) {
    element.textContent = t(element.dataset.i18n as MessageKey);
  }
  for (const element of document.querySelectorAll<HTMLElement>("[data-i18n-aria]")) {
    element.setAttribute("aria-label", t(element.dataset.i18nAria as MessageKey));
  }
  for (const element of document.querySelectorAll<HTMLElement>("[data-i18n-content]")) {
    element.setAttribute("content", t(element.dataset.i18nContent as MessageKey));
  }
  // The stylesheet's hover tooltip reads `data-tip` (attr()), so the copy
  // lands there rather than in a title, which would show a second, native one.
  for (const element of document.querySelectorAll<HTMLElement>("[data-i18n-tip]")) {
    element.dataset.tip = t(element.dataset.i18nTip as MessageKey);
  }
}

/** Switch languages in place: remember the choice on this device, swap the
 * dictionary, rewrite the static markup and let every listener redo its own
 * copy. The choice is deliberately not written into the URL — a link copied
 * afterwards would carry it to everyone it is shared with, overriding their
 * browser's language — and an explicit `?lang=` already on the page is
 * dropped for the same reason. */
export function setLocale(next: Locale): void {
  if (next === locale) return;
  try {
    localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // Without storage the choice lasts this page load only.
  }
  const params = new URLSearchParams(window.location.search);
  params.delete("lang");
  const search = params.size > 0 ? `?${params.toString()}` : "";
  if (search !== window.location.search) {
    window.history.replaceState(null, "", `${window.location.pathname}${search}${window.location.hash}`);
  }
  locale = next;
  active = DEFINITIONS[next];
  basemapLang = active.basemapLang;
  htmlLang = active.htmlLang;
  applyStaticMessages();
  for (const listener of listeners) listener();
}
