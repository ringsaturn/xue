import type { Map as MaplibreMap } from "maplibre-gl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { aimOffset, cardCrowdsCapsule, createProbeLayout, probeLayoutFor, probePageFor } from "../../web/src/probelayout";
import type { SoundingSection } from "../../web/src/sounding/section";
import type { ObservationSection } from "../../web/src/stations/obssection";

describe("probeLayoutFor", () => {
  it("gives a phone and a narrow window the tabs, a wide one the card", () => {
    expect(probeLayoutFor(390)).toBe("tabs");
    expect(probeLayoutFor(1119)).toBe("tabs");
    expect(probeLayoutFor(1120)).toBe("side");
    expect(probeLayoutFor(1920)).toBe("side");
  });
});

describe("cardCrowdsCapsule", () => {
  it("moves the capsule over only where the card would stand on it", () => {
    // 960 px of capsule, centred, and 476 px of card on the right.
    expect(cardCrowdsCapsule(1366)).toBe(true);
    expect(cardCrowdsCapsule(1911)).toBe(true);
    expect(cardCrowdsCapsule(1912)).toBe(false);
    expect(cardCrowdsCapsule(2560)).toBe(false);
  });
});

describe("probePageFor", () => {
  const all = { observations: true, sounding: true, soundingOpen: false };

  it("keeps the page it was on while its section is there", () => {
    expect(probePageFor("rows", all)).toBe("rows");
    expect(probePageFor("obs", all)).toBe("obs");
  });

  it("follows a sounding that opened itself", () => {
    expect(probePageFor("rows", { ...all, soundingOpen: true })).toBe("sounding");
    expect(probePageFor("obs", { ...all, soundingOpen: true })).toBe("sounding");
  });

  it("falls back to the rows when the page's section has gone or folded", () => {
    expect(probePageFor("sounding", all)).toBe("rows");
    expect(probePageFor("sounding", { ...all, sounding: false, soundingOpen: true })).toBe("rows");
    expect(probePageFor("obs", { ...all, observations: false })).toBe("rows");
  });
});

describe("aimOffset", () => {
  const map = { left: 0, top: 0, right: 1920, bottom: 937 };
  const panel = { left: 480, top: 512, right: 1440, bottom: 822 };
  const card = { left: 1456, top: 72, right: 1844, bottom: 876 };

  it("leaves a pin in the clear where it is", () => {
    expect(aimOffset({ x: 1095, y: 299 }, map, panel, card)).toBeNull();
    // Near the screen's edge is where the reader put it.
    expect(aimOffset({ x: 12, y: 300 }, map, panel, null)).toBeNull();
  });

  it("moves a pin under the panel to the middle of the map above it", () => {
    // Clear: x 24–1896, y 72–488.
    expect(aimOffset({ x: 900, y: 600 }, map, panel, null)).toEqual([0, 280 - 468.5]);
    // A pin just above the panel's edge is as good as under it.
    expect(aimOffset({ x: 900, y: 500 }, map, panel, null)).not.toBeNull();
  });

  it("keeps to the left of the card", () => {
    // Clear: x 24–1432, y 72–488.
    expect(aimOffset({ x: 1600, y: 300 }, map, panel, card)).toEqual([728 - 960, 280 - 468.5]);
    expect(aimOffset({ x: 900, y: 600 }, map, panel, card)).toEqual([728 - 960, 280 - 468.5]);
  });

  it("brings back a pin that is off the screen", () => {
    expect(aimOffset({ x: -40, y: 300 }, map, panel, null)).not.toBeNull();
  });

  it("does nothing where there is no clear map to move the pin into", () => {
    const tallPanel = { left: 12, top: 100, right: 378, bottom: 632 };
    expect(aimOffset({ x: 200, y: 400 }, { left: 0, top: 0, right: 390, bottom: 740 }, tallPanel, null)).toBeNull();
  });
});

/** A section as the layout sees one: something that is there or not, open
 * or not, with a body that can be docked elsewhere. */
function fakeSounding() {
  const root = document.createElement("section");
  const body = document.createElement("div");
  root.append(body);
  let open = false;
  const section = {
    root,
    isOpen: () => open,
    open: vi.fn(() => {
      open = true;
    }),
    close: vi.fn(() => {
      open = false;
    }),
    dock: vi.fn((host: HTMLElement | null) => {
      (host ?? root).append(body);
    }),
    caption: () => "47778 Shionomisaki · 12 km",
    render: vi.fn(),
  };
  return { section, body, openItself: () => (open = true) };
}

function fakeObservation() {
  const root = document.createElement("section");
  root.hidden = true;
  return { root, render: vi.fn(), setOpen: vi.fn() };
}

function build(width: number) {
  vi.stubGlobal("innerWidth", width);
  const parent = document.createElement("div");
  const panel = document.createElement("section");
  const head = document.createElement("div");
  const close = document.createElement("button");
  head.append(close);
  panel.append(head);
  parent.append(panel);
  document.body.append(parent);
  const sounding = fakeSounding();
  const observation = fakeObservation();
  const easeTo = vi.fn();
  const layout = createProbeLayout({
    map: {
      getContainer: () => parent,
      project: () => ({ x: 0, y: 0 }),
      easeTo,
    } as unknown as MaplibreMap,
    panel,
    head,
    close,
    observation: observation as unknown as ObservationSection,
    sounding: sounding.section as unknown as SoundingSection,
    pin: () => ({ longitude: 135, latitude: 34 }),
  });
  const tab = (page: string): HTMLButtonElement => parent.querySelector<HTMLButtonElement>(`.probe-tab[data-page="${page}"]`)!;
  const card = parent.querySelector<HTMLElement>(".sounding-card")!;
  return { parent, panel, sounding, observation, layout, tab, card };
}

describe("the panel's layout", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe(): void {}
        disconnect(): void {}
      },
    );
    // The pin is aimed a frame after the layout changed; here, at once. A
    // page without layout has no clear map, so the map is never moved.
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      callback(0);
      return 0;
    });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    document.body.replaceChildren();
  });

  it("shows one page at a time on a phone, at half height until asked for more", () => {
    const { panel, sounding, layout, tab, card, parent } = build(390);
    layout.sync();
    expect(panel.dataset.layout).toBe("tabs");
    expect(panel.dataset.page).toBe("rows");
    expect(panel.dataset.tall).toBe("false");
    expect(parent.querySelector<HTMLElement>(".probe-tabs")!.hidden).toBe(false);
    // No station nearby: no page for its observations.
    expect(tab("obs").hidden).toBe(true);
    expect(tab("sounding").hidden).toBe(false);
    expect(tab("rows").getAttribute("aria-selected")).toBe("true");
    expect(tab("rows").textContent).toBe("Time series");
    expect(tab("sounding").textContent).toBe("Sounding");

    tab("sounding").click();
    expect(sounding.section.open).toHaveBeenCalledTimes(1);
    expect(panel.dataset.page).toBe("sounding");
    expect(tab("sounding").getAttribute("aria-selected")).toBe("true");
    // The body stays in the panel: there is no card on a phone.
    expect(card.hidden).toBe(true);
    expect(sounding.body.parentElement).toBe(sounding.section.root);

    // Leaving the page folds the chart.
    tab("rows").click();
    expect(sounding.section.close).toHaveBeenCalled();
    expect(panel.dataset.page).toBe("rows");

    parent.querySelector<HTMLButtonElement>(".probe-grow")!.click();
    expect(panel.dataset.tall).toBe("true");
  });

  it("turns to the sounding's page when the section opens itself", () => {
    const { panel, sounding, layout, tab } = build(390);
    layout.sync();
    sounding.openItself();
    layout.sync();
    expect(panel.dataset.page).toBe("sounding");
    expect(tab("sounding").getAttribute("aria-selected")).toBe("true");
  });

  it("has no tabs to show with nothing but the rows", () => {
    const { parent, sounding, layout } = build(390);
    sounding.section.root.hidden = true;
    layout.sync();
    expect(parent.querySelector<HTMLElement>(".probe-tabs")!.hidden).toBe(true);
  });

  it("docks the sounding in a card on a wide window, and moves the capsule over for it", () => {
    const { parent, panel, sounding, layout, card } = build(1366);
    layout.sync();
    expect(panel.dataset.layout).toBe("side");
    expect(parent.querySelector<HTMLElement>(".probe-tabs")!.hidden).toBe(true);
    expect(sounding.body.parentElement).toBe(card.querySelector(".sounding-card-body"));
    // Folded: no card, and the capsule where it always is.
    expect(card.hidden).toBe(true);
    expect(parent.style.getPropertyValue("--dock-right")).toBe("");

    sounding.openItself();
    layout.sync();
    expect(card.hidden).toBe(false);
    expect(card.querySelector(".sounding-card-caption")!.textContent).toBe("47778 Shionomisaki · 12 km");
    expect(parent.style.getPropertyValue("--dock-right")).toBe("476px");

    card.querySelector<HTMLButtonElement>(".sounding-card-close")!.click();
    expect(sounding.section.close).toHaveBeenCalledTimes(1);
    layout.sync();
    expect(card.hidden).toBe(true);
    expect(parent.style.getPropertyValue("--dock-right")).toBe("");
  });

  it("leaves the capsule alone where the card stands clear of it", () => {
    const { parent, sounding, layout, card } = build(1920);
    sounding.openItself();
    layout.sync();
    expect(card.hidden).toBe(false);
    expect(parent.style.getPropertyValue("--dock-right")).toBe("");
  });

  it("takes the card down with the panel", () => {
    const { panel, sounding, layout, card } = build(1920);
    sounding.openItself();
    layout.sync();
    expect(card.hidden).toBe(false);
    panel.hidden = true;
    layout.sync();
    expect(card.hidden).toBe(true);
  });
});
