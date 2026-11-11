/**
 * The settings sheet behind the gear in the viewer's top-right row: the
 * theme, the language, the display units (`units.ts`), the coordinate grids
 * and the format a pinned point's coordinates are written in
 * (`mapprefs.ts`).
 *
 * Every row is a radio group or a pair of switches built from the live
 * preferences, and the whole panel is rebuilt after any change — a preset
 * changes six rows at once — so nothing here keeps its own copy of a
 * choice. Choices apply in place, without a reload, through the modules
 * that own them.
 */

import { onLocaleChange, t, type MessageKey } from "./i18n";
import { coordinateFormat, grid, onMapPrefsChange, setCoordinateFormat, setGrid } from "./mapprefs";
import { createSheet, mountLanguageList } from "./sheet";
import { isDark, onThemeChange, toggleTheme } from "./theme";
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

interface Choice {
  label: string;
  checked: boolean;
  pick: () => void;
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
    button.addEventListener("click", choice.pick);
    group.append(button);
  }
  wrapper.append(label, group);
  return wrapper;
}

/** Wire the sheet to its trigger and fill its body. A page without the
 * coordinate grids (the storm map) leaves out their rows (`grids: false`). */
export function mountSettingsSheet(
  trigger: HTMLElement,
  sheet: HTMLElement,
  body: HTMLElement,
  languages: HTMLElement,
  { grids = true }: { grids?: boolean } = {},
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
    if (grids) {
      rows.push(
        field(
          t("settingsGrid"),
          [
            { label: "MGRS", checked: grid.mgrs, pick: () => setGrid({ ...grid, mgrs: !grid.mgrs }) },
            { label: t("gridLatlon"), checked: grid.latlon, pick: () => setGrid({ ...grid, latlon: !grid.latlon }) },
          ],
          true,
        ),
        field(t("settingsCoordinates"), [
          { label: t("coordsLatlon"), checked: coordinateFormat === "latlon", pick: () => setCoordinateFormat("latlon") },
          { label: "MGRS", checked: coordinateFormat === "mgrs", pick: () => setCoordinateFormat("mgrs") },
        ]),
      );
    }
    body.replaceChildren(...rows);
    if (focused) restoreFocus(body, focused);
  }

  render();
  onThemeChange(render);
  onUnitsChange(render);
  onMapPrefsChange(render);
  onLocaleChange(() => {
    renderLanguages();
    render();
  });
}

/** Where a focused button sits among the sheet's rows: row, then button. */
function focusPath(element: HTMLElement): [number, number] | null {
  const row = element.closest(".settings-field");
  if (!row?.parentElement) return null;
  const buttons = [...row.querySelectorAll("button")];
  return [[...row.parentElement.children].indexOf(row), buttons.indexOf(element as HTMLButtonElement)];
}

function restoreFocus(body: HTMLElement, [row, index]: [number, number]): void {
  body.children[row]?.querySelectorAll<HTMLButtonElement>("button")[index]?.focus();
}
