import { describe, expect, it } from "vitest";

import { escapes, EscapeStack } from "../../web/src/escapestack";

function surface(name: string, log: string[]) {
  const made = {
    open: true,
    isOpen: () => made.open,
    close: () => {
      made.open = false;
      log.push(name);
    },
  };
  return made;
}

describe("the Escape stack", () => {
  it("closes the newest surface alone, one a press", () => {
    const log: string[] = [];
    const stack = new EscapeStack();
    const panel = surface("panel", log);
    const sheet = surface("sheet", log);
    stack.opened(panel);
    stack.opened(sheet);
    expect(stack.press()).toBe(true);
    expect(log).toEqual(["sheet"]);
    expect(stack.press()).toBe(true);
    expect(log).toEqual(["sheet", "panel"]);
    expect(stack.press()).toBe(false);
  });

  it("passes over what closed by other means, and keeps the place of what has stepped aside", () => {
    const log: string[] = [];
    const stack = new EscapeStack();
    const section = surface("section", log);
    const probe = surface("probe", log);
    stack.opened(section);
    stack.opened(probe);
    section.open = false;
    expect(stack.press()).toBe(true);
    section.open = true;
    expect(stack.press()).toBe(true);
    expect(log).toEqual(["probe", "section"]);
  });

  it("makes a surface opened again the newest, and forgets one gone for good", () => {
    const log: string[] = [];
    const stack = new EscapeStack();
    const first = surface("first", log);
    const second = surface("second", log);
    const card = surface("card", log);
    stack.opened(first);
    stack.opened(second);
    stack.opened(first);
    stack.opened(card);
    stack.forget(card);
    stack.press();
    expect(log).toEqual(["first"]);
  });

  it("stays on a surface an Escape leaves open", () => {
    const stack = new EscapeStack();
    let shape = true;
    let armed = true;
    stack.opened({
      isOpen: () => armed,
      close: () => {
        if (shape) shape = false;
        else armed = false;
      },
    });
    stack.press();
    expect([shape, armed]).toEqual([false, true]);
    stack.press();
    expect(armed).toBe(false);
    expect(stack.press()).toBe(false);
  });
});

describe("the page's Escape", () => {
  it("leaves the key to who has answered it, to a composition and to a list", () => {
    const log: string[] = [];
    const held = surface("held", log);
    escapes.opened(held);
    const press = (target: EventTarget, init: KeyboardEventInit = {}, answered = false) => {
      const event = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true, ...init });
      if (answered) event.preventDefault();
      target.dispatchEvent(event);
    };
    const select = document.createElement("select");
    document.body.append(select);
    press(select);
    press(window, { isComposing: true });
    press(window, {}, true);
    press(window, { key: "Enter" });
    expect(log).toEqual([]);
    press(document.body);
    expect(log).toEqual(["held"]);
    escapes.forget(held);
    select.remove();
  });
});
