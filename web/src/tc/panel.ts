/** The storm sheet: the systems the product carries, grouped by level, and
 * the toggles for what to draw of them. Pure DOM over a small state; the
 * shell (`main.ts`) owns the state and answers the callbacks. Built the
 * way the model and language sheets are — the same `.model-sheet` panel,
 * rows that are buttons — so it opens under the rail tile on desktop and
 * docks to the bottom on phones without a stylesheet of its own. */

import { t } from "../i18n";
import { agencyCode, TC_AGENCIES, TC_MODELS } from "./agencies";
import type { TcIndex, TcIndexEntry } from "./schema";

export interface TcPanelState {
  index: TcIndex | null;
  /** The focused storm, or null for every named system. */
  selected: string | null;
  hidden: boolean;
  potential: boolean;
  agencies: ReadonlySet<string>;
  models: ReadonlySet<string>;
  members: boolean;
  best: boolean;
}

export interface TcPanelChoices {
  /** The agency and model keys present on the storms drawn, in order. */
  agencies: readonly string[];
  models: readonly string[];
}

export interface TcPanelHandlers {
  onSelect(id: string | null): void;
  onAgency(id: string, on: boolean): void;
  onModel(id: string, on: boolean): void;
  onMembers(on: boolean): void;
  onBest(on: boolean): void;
  onPotential(on: boolean): void;
  onHidden(hidden: boolean): void;
}

function row(
  label: string,
  detail: string | null,
  pressed: boolean,
  onClick: () => void,
  extra?: string,
): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "tc-row";
  button.setAttribute("aria-pressed", String(pressed));
  const name = document.createElement("span");
  name.className = "tc-row-name";
  name.textContent = label;
  button.append(name);
  if (detail !== null) {
    const small = document.createElement("small");
    small.textContent = detail;
    button.append(small);
  }
  if (extra) {
    const tag = document.createElement("span");
    tag.className = "tc-row-tag";
    tag.textContent = extra;
    button.append(tag);
  }
  const check = document.createElement("span");
  check.className = "model-check";
  check.setAttribute("aria-hidden", "true");
  button.append(check);
  button.addEventListener("click", onClick);
  return button;
}

function chip(
  label: string,
  pressed: boolean,
  color: string | null,
  onClick: () => void,
): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "tc-chip";
  button.setAttribute("aria-pressed", String(pressed));
  if (color) button.style.setProperty("--chip-ink", color);
  button.textContent = label;
  button.addEventListener("click", onClick);
  return button;
}

function group(heading: string): HTMLElement {
  const label = document.createElement("p");
  label.className = "tc-group";
  label.textContent = heading;
  return label;
}

/** The storm's row: its name (or its id when unnamed), the basin and id,
 * and its class as the source that classed it spells it. */
function stormRow(
  entry: TcIndexEntry,
  pressed: boolean,
  onClick: () => void,
): HTMLButtonElement {
  const title = entry.name ?? (entry.level === "A" ? entry.id : t("tcUnnamed"));
  const detail =
    entry.level === "A" ? `${entry.basin} · ${entry.id}` : entry.basin;
  const tag = entry.class
    ? `${entry.class}${entry.classAgency ? ` ${agencyCode(entry.classAgency)}` : ""}`
    : entry.level === "C"
      ? t("tcModelOnly")
      : "";
  const button = row(title, detail, pressed, onClick, tag || undefined);
  button.dataset.storm = entry.id;
  button.dataset.level = entry.level;
  return button;
}

export function renderTcPanel(
  list: HTMLElement,
  state: TcPanelState,
  choices: TcPanelChoices,
  handlers: TcPanelHandlers,
): void {
  const children: HTMLElement[] = [];
  const storms = state.index?.storms ?? [];
  const named = storms.filter((entry) => entry.level === "A");
  const disturbances = storms.filter((entry) => entry.level === "B");
  const potential = storms.filter((entry) => entry.level === "C");
  const drawn = !state.hidden;

  if (
    named.length + disturbances.length === 0 &&
    (!state.potential || potential.length === 0)
  ) {
    const none = document.createElement("p");
    none.className = "tc-empty";
    none.textContent = t("tcNone");
    children.push(none);
  } else {
    children.push(
      row(t("tcAllSystems"), null, drawn && state.selected === null, () =>
        handlers.onSelect(null),
      ),
    );
    for (const entry of named) {
      children.push(
        stormRow(entry, drawn && state.selected === entry.id, () =>
          handlers.onSelect(entry.id),
        ),
      );
    }
    if (disturbances.length) {
      children.push(group(t("tcDisturbances")));
      for (const entry of disturbances) {
        children.push(
          stormRow(entry, drawn && state.selected === entry.id, () =>
            handlers.onSelect(entry.id),
          ),
        );
      }
    }
  }
  if (potential.length) {
    children.push(group(t("tcPotential")));
    children.push(
      chip(
        t("tcPotentialShow", { count: potential.length }),
        state.potential,
        null,
        () => handlers.onPotential(!state.potential),
      ),
    );
    if (state.potential) {
      for (const entry of potential) {
        children.push(
          stormRow(entry, drawn && state.selected === entry.id, () =>
            handlers.onSelect(entry.id),
          ),
        );
      }
    }
  }
  if (choices.agencies.length) {
    children.push(group(t("tcAgencies")));
    const chips = document.createElement("div");
    chips.className = "tc-chips";
    for (const id of choices.agencies) {
      chips.append(
        chip(
          agencyCode(id),
          state.agencies.has(id),
          TC_AGENCIES[id]?.color ?? null,
          () => handlers.onAgency(id, !state.agencies.has(id)),
        ),
      );
    }
    children.push(chips);
  }
  if (choices.models.length) {
    children.push(group(t("tcModels")));
    const chips = document.createElement("div");
    chips.className = "tc-chips";
    for (const id of choices.models) {
      chips.append(
        chip(agencyCode(id), state.models.has(id), null, () =>
          handlers.onModel(id, !state.models.has(id)),
        ),
      );
    }
    if (choices.models.some((id) => TC_MODELS[id]?.ensemble)) {
      chips.append(
        chip(t("tcMembers"), state.members, null, () =>
          handlers.onMembers(!state.members),
        ),
      );
    }
    children.push(chips);
  }
  if (storms.length) {
    const chips = document.createElement("div");
    chips.className = "tc-chips";
    chips.append(
      chip(t("tcBest"), state.best, null, () => handlers.onBest(!state.best)),
    );
    chips.append(
      chip(state.hidden ? t("tcShow") : t("tcHide"), state.hidden, null, () =>
        handlers.onHidden(!state.hidden),
      ),
    );
    children.push(chips);
  }
  list.replaceChildren(...children);
}
