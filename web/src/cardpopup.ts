// The cards the map opens on a mark or an area (storm points, stations,
// fires, air quality, warnings) are MapLibre popups with the sheet's surface
// put on (style.css, `.tc-popup` / `.station-popup`). Each carries a close
// button: Escape and a click elsewhere also close a card, but a phone has no
// Escape, and a click inside a warning area opens that area's card again.
//
// A card opens towards the side of its point with room — above it when the
// point is low on the screen — and is never taller than that room: the
// panels along the bottom (the point data, the time controls) sit over the
// map, so a card that ran under them could not be read. A longer card
// scrolls inside itself.

import { Popup, type Map as MaplibreMap, type PopupOptions } from "maplibre-gl";
import { t } from "./i18n";

/** What covers the map's lower edge when it is showing. */
const BOTTOM_PANELS = ["#probe-panel", "#timeline-panel"];
const MARGIN = 12;
/** Below this much room under the point, a card opens above it if there is
 * more room there. */
const ROOM_BELOW = 260;

export function cardPopup(options: Omit<PopupOptions, "closeButton" | "closeOnClick" | "anchor">): Popup {
  const popup = new Popup({ ...options, closeButton: true, closeOnClick: false });
  let maxHeight: number | null = null;
  const addTo = popup.addTo.bind(popup);
  popup.addTo = (map: MaplibreMap) => {
    const place = placement(map, popup, options.offset);
    if (place) {
      popup.options.anchor = place.anchor;
      maxHeight = place.maxHeight;
    }
    return addTo(map);
  };
  popup.on("open", () => {
    const element = popup.getElement();
    if (!element) return;
    if (maxHeight !== null) element.style.setProperty("--card-max-height", `${maxHeight}px`);
    // MapLibre labels its button in English; the label is read when the card
    // opens, so a card opened after a language switch speaks the new one.
    element.querySelector(".maplibregl-popup-close-button")?.setAttribute("aria-label", t("cardCloseAria"));
    // The wheel and a finger on a card scroll the card, not zoom or pan the
    // map underneath.
    const content = element.querySelector(".maplibregl-popup-content");
    for (const type of ["wheel", "touchstart", "touchmove"] as const) {
      content?.addEventListener(type, (event) => event.stopPropagation(), { passive: true });
    }
  });
  return popup;
}

function placement(
  map: MaplibreMap,
  popup: Popup,
  offset: PopupOptions["offset"],
): { anchor: "top" | "bottom"; maxHeight: number } | null {
  let lngLat;
  try {
    lngLat = popup.getLngLat();
  } catch {
    return null;
  }
  const frame = map.getContainer().getBoundingClientRect();
  const y = frame.top + map.project(lngLat).y;
  let bottom = frame.bottom;
  for (const selector of BOTTOM_PANELS) {
    const panel = document.querySelector<HTMLElement>(selector);
    if (!panel || panel.hidden) continue;
    const rect = panel.getBoundingClientRect();
    if (rect.height > 0 && rect.top < bottom && rect.top > y) bottom = rect.top;
  }
  const gap = (typeof offset === "number" ? offset : 0) + MARGIN;
  const above = y - frame.top - gap;
  const below = bottom - y - gap;
  const anchor = below >= ROOM_BELOW || below >= above ? "top" : "bottom";
  // The card's body scrolls; its padding and the tip take the rest.
  const room = (anchor === "top" ? below : above) - 24;
  return { anchor, maxHeight: Math.max(120, Math.round(room)) };
}
