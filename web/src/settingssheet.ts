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
import { browserZone, displayZone, knownZones, onDisplayZoneChange, setZonePref, zoneOffsetLabel, zonePref } from "./timezone";
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

/** The clock row: the three modes as chips, and every other zone in a list
 * under them. A zone picked from the list unchecks the chips. The list is
 * the engine's own (some four hundred ids), built when the row is. */
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
  const select = document.createElement("select");
  select.className = "settings-select";
  select.setAttribute("aria-label", t("zoneOther"));
  const custom = !["auto", "local", "utc"].includes(zonePref);
  const placeholder = new Option(`${t("zoneOther")}…`, "");
  placeholder.disabled = true;
  select.append(placeholder);
  for (const zone of knownZones()) {
    select.append(new Option(zone === displayZone && custom ? `${zone} · ${zoneOffsetLabel(zone, now)}` : zone, zone));
  }
  select.value = custom ? zonePref : "";
  select.addEventListener("change", () => {
    if (select.value) setZonePref(select.value);
  });
  wrapper.append(select);
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
  const buttons = [...row.querySelectorAll<HTMLElement>("button, select")];
  return [[...row.parentElement.children].indexOf(row), buttons.indexOf(element)];
}

function restoreFocus(body: HTMLElement, [row, index]: [number, number]): void {
  body.children[row]?.querySelectorAll<HTMLElement>("button, select")[index]?.focus();
}
