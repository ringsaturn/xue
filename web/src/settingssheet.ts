/**
 * The settings sheet behind the gear in the viewer's top-right row: the
 * theme, the language, the clock valid times read in (`timezone.ts`, on
 * pages that stamp them), the display units (`units.ts`), the coordinate
 * grids and the format a pinned point's coordinates are written in
 * (`mapprefs.ts`).
 *
 * Every row is a radio group or a pair of switches built from the live
 * preferences, and the whole panel is rebuilt after any change — a preset
 * changes six rows at once — so nothing here keeps its own copy of a
 * choice. Choices apply in place, without a reload, through the modules
 * that own them.
 */

import { onLocaleChange, t, type MessageKey } from "./i18n";
import { COORDINATE_FORMATS, coordinateFormat, grid, onMapPrefsChange, setCoordinateFormat, setGrid } from "./mapprefs";
import { GRID_KINDS, type GridKind } from "./urlstate";
import { createSheet, mountLanguageList } from "./sheet";
import { isDark, onThemeChange, toggleTheme } from "./theme";
import { browserZone, knownZones, matchZone, onDisplayZoneChange, searchZones, setZonePref, ZONE_MODES, zoneOffsetLabel, zonePref } from "./timezone";
import {
  browserRegion,
  onUnitsChange,
  setUnitOverride,
  setUnitPreset,
  UNIT_LABELS,
  UNIT_OPTIONS,
  UNIT_PRESETS,
  UNIT_QUANTITIES,
  unitPrefs,
  units,
  type UnitPreset,
  type UnitQuantity,
} from "./units";

const PRESET_KEYS: Record<UnitPreset, MessageKey> = { auto: "unitsAuto", metric: "unitsMetric", imperial: "unitsImperial" };
const QUANTITY_KEYS: Record<UnitQuantity, MessageKey> = {
  temperature: "unitTemperature",
  wind: "unitWind",
  pressure: "unitPressure",
  precipitation: "unitPrecipitation",
  height: "unitHeight",
  distance: "unitDistance",
};

/** The grids by their own codes, which are never translated. */
const GRID_CODES: Record<Exclude<GridKind, "latlon">, string> = { mgrs: "MGRS", utm: "UTM", gars: "GARS", bng: "BNG" };

interface Choice {
  label: string;
  checked: boolean;
  pick: () => void;
  title?: string;
}

/** One captioned row of choices: a radio group, or (`switches`) buttons
 * that each switch on and off. */
function field(caption: string, choices: readonly Choice[], switches = false): HTMLElement {
  const wrapper = document.createElement("div");
  wrapper.className = "export-field settings-field";
  const label = document.createElement("span");
  label.className = "export-caption";
  label.textContent = caption;
  const group = document.createElement("div");
  group.className = "export-choices";
  group.setAttribute("role", switches ? "group" : "radiogroup");
  group.setAttribute("aria-label", caption);
  for (const choice of choices) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = choice.label;
    if (switches) {
      button.setAttribute("aria-pressed", String(choice.checked));
      // The stylesheet marks a chosen chip by aria-checked; a switch reads
      // the same.
      button.setAttribute("aria-checked", String(choice.checked));
    } else {
      button.setAttribute("role", "radio");
      button.setAttribute("aria-checked", String(choice.checked));
    }
    if (choice.title) button.title = choice.title;
    button.addEventListener("click", choice.pick);
    group.append(button);
  }
  wrapper.append(label, group);
  return wrapper;
}

const ZONE_MODE_KEYS: Record<string, MessageKey> = { auto: "zoneAuto", local: "zoneLocal" };

/** How many zones the search box offers at once. */
const ZONE_SUGGESTIONS = 8;

/** The clock row: the three modes as chips, and a search box for every
 * other zone under them. The suggestions are the sheet's own buttons, a
 * handful at a time, not a native `<datalist>`: handed the engine's four
 * hundred ids, the browser's own popup can hang the page (WebKit on macOS
 * does). Enter takes the zone the query names, or failing that the first
 * suggestion; a zone picked there unchecks the chips. */
function zoneField(): HTMLElement {
  const now = Date.now();
  const wrapper = field(
    t("settingsZone"),
    (["auto", "local", "utc"] as const).map((mode) => ({
      // Local says which offset it reads, as an instrument code.
      label: mode === "utc" ? "UTC" : mode === "local" ? `${t(ZONE_MODE_KEYS[mode]!)} · ${zoneOffsetLabel(browserZone(), now)}` : t(ZONE_MODE_KEYS[mode]!),
      checked: zonePref === mode,
      pick: () => setZonePref(mode),
      ...(mode === "auto" ? { title: t("zoneAutoHint") } : mode === "local" ? { title: browserZone() } : {}),
    })),
  );
  const custom = !(ZONE_MODES as readonly string[]).includes(zonePref);
  const input = document.createElement("input");
  input.type = "search";
  input.className = "settings-select";
  input.placeholder = `${t("zoneOther")}…`;
  input.setAttribute("aria-label", t("zoneOther"));
  input.autocomplete = "off";
  input.spellcheck = false;
  input.value = custom ? zonePref : "";
  if (custom) input.setAttribute("aria-current", "true");
  const suggestions = document.createElement("div");
  suggestions.className = "settings-zone-suggestions";
  suggestions.id = "settings-zone-suggestions";
  suggestions.setAttribute("role", "listbox");
  suggestions.setAttribute("aria-label", t("zoneOther"));
  input.setAttribute("aria-controls", suggestions.id);
  let shown: string[] = [];

  const pick = (zone: string) => {
    if (zone !== zonePref) setZonePref(zone);
    else {
      input.value = zone;
      suggestions.replaceChildren();
    }
  };
  const suggest = () => {
    input.removeAttribute("aria-invalid");
    shown = input.value.trim() === "" ? [] : searchZones(input.value, knownZones(), ZONE_SUGGESTIONS);
    suggestions.replaceChildren(
      ...shown.map((zone) => {
        const button = document.createElement("button");
        button.type = "button";
        button.setAttribute("role", "option");
        const name = document.createElement("span");
        name.textContent = zone;
        const offset = document.createElement("span");
        offset.className = "settings-zone-offset";
        offset.textContent = zoneOffsetLabel(zone, now);
        button.append(name, offset);
        button.addEventListener("click", () => pick(zone));
        button.addEventListener("keydown", (event) => moveFocus(event, button));
        return button;
      }),
    );
  };
  const moveFocus = (event: KeyboardEvent, from: HTMLElement) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    const buttons = [...suggestions.querySelectorAll<HTMLElement>("button")];
    const index = from === input ? -1 : buttons.indexOf(from);
    const next = event.key === "ArrowDown" ? index + 1 : index - 1;
    (next < 0 ? input : buttons[Math.min(next, buttons.length - 1)])?.focus();
  };
  input.addEventListener("input", suggest);
  input.addEventListener("keydown", (event) => {
    moveFocus(event, input);
    if (event.key !== "Enter") return;
    const zone = matchZone(input.value, knownZones()) ?? shown[0] ?? null;
    if (zone !== null) pick(zone);
    else input.setAttribute("aria-invalid", String(input.value.trim() !== ""));
  });
  wrapper.append(input, suggestions);
  return wrapper;
}

/** Wire the sheet to its trigger and fill its body. `zone` adds the clock
 * row, on a page whose times follow it. */
export function mountSettingsSheet(
  trigger: HTMLElement,
  sheet: HTMLElement,
  body: HTMLElement,
  languages: HTMLElement,
  options: { zone?: boolean } = {},
): void {
  createSheet({ trigger, sheet });
  const renderLanguages = mountLanguageList(languages);
  const languageField = languages.closest<HTMLElement>(".settings-field");

  function render(): void {
    // Keep focus on the row the reader just used across the rebuild.
    const focused = document.activeElement instanceof HTMLElement && body.contains(document.activeElement) ? focusPath(document.activeElement) : null;
    const region = browserRegion();
    const rows: HTMLElement[] = [
      field(t("settingsTheme"), [
        { label: t("settingsThemeLight"), checked: !isDark, pick: () => isDark && toggleTheme() },
        { label: t("settingsThemeDark"), checked: isDark, pick: () => !isDark && toggleTheme() },
      ]),
    ];
    if (languageField) rows.push(languageField);
    if (options.zone) rows.push(zoneField());
    rows.push(
      field(
        t("settingsUnits"),
        UNIT_PRESETS.map((preset) => ({
          // Auto says which region it read, as an instrument code.
          label: preset === "auto" && region !== null ? `${t(PRESET_KEYS[preset])} · ${region}` : t(PRESET_KEYS[preset]),
          checked: unitPrefs.preset === preset && Object.keys(unitPrefs.overrides).length === 0,
          pick: () => setUnitPreset(preset),
        })),
      ),
    );
    for (const quantity of UNIT_QUANTITIES) {
      const labels = UNIT_LABELS[quantity] as Record<string, string>;
      rows.push(
        field(
          t(QUANTITY_KEYS[quantity]),
          (UNIT_OPTIONS[quantity] as readonly string[]).map((option) => ({
            label: quantity === "precipitation" ? `${labels[option]} · ${labels[option]}/h` : labels[option]!,
            checked: units[quantity] === option,
            pick: () => setUnitOverride(quantity, option as never),
          })),
        ),
      );
    }
    rows.push(
      field(
        t("settingsGrid"),
        GRID_KINDS.map((kind) => ({
          label: kind === "latlon" ? t("gridLatlon") : GRID_CODES[kind],
          checked: grid[kind],
          pick: () => setGrid({ ...grid, [kind]: !grid[kind] }),
        })),
        true,
      ),
      field(
        t("settingsCoordinates"),
        COORDINATE_FORMATS.map((format) => ({
          label: format === "latlon" ? t("coordsLatlon") : GRID_CODES[format],
          checked: coordinateFormat === format,
          pick: () => setCoordinateFormat(format),
        })),
      ),
    );
    body.replaceChildren(...rows);
    if (focused) restoreFocus(body, focused);
  }

  render();
  onThemeChange(render);
  onUnitsChange(render);
  onMapPrefsChange(render);
  if (options.zone) onDisplayZoneChange(render);
  onLocaleChange(() => {
    renderLanguages();
    render();
  });
}

/** Where a focused button sits among the sheet's rows: row, then button. */
function focusPath(element: HTMLElement): [number, number] | null {
  const row = element.closest(".settings-field");
  if (!row?.parentElement) return null;
  const buttons = [...row.querySelectorAll<HTMLElement>("button, input")];
  return [[...row.parentElement.children].indexOf(row), buttons.indexOf(element)];
}

function restoreFocus(body: HTMLElement, [row, index]: [number, number]): void {
  body.children[row]?.querySelectorAll<HTMLElement>("button, input")[index]?.focus();
}
