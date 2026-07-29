import { afterEach, describe, expect, it, vi } from "vitest";
import {
  KeyboardShortcutController,
  normalizeShortcut,
} from "./KeyboardShortcutController";

describe("KeyboardShortcutController", () => {
  const controller = new KeyboardShortcutController();
  afterEach(() => {
    controller.detach();
    document.body.replaceChildren();
  });

  it("normalizes platform aliases and modifier order", () => {
    expect(normalizeShortcut("Shift + Control + K")).toBe("ctrl+shift+k");
    expect(normalizeShortcut("Cmd+Esc")).toBe("meta+escape");
  });

  it("runs matching enabled commands and prevents browser defaults", () => {
    const run = vi.fn();
    controller.update([{ id: "search", keys: "Ctrl+K", run }]);
    controller.attach();
    const event = new KeyboardEvent("keydown", {
      key: "k",
      ctrlKey: true,
      cancelable: true,
      bubbles: true,
    });
    document.dispatchEvent(event);

    expect(run).toHaveBeenCalledOnce();
    expect(event.defaultPrevented).toBe(true);
  });

  it("ignores editable controls unless explicitly allowed", () => {
    const blocked = vi.fn();
    const allowed = vi.fn();
    const input = document.createElement("input");
    document.body.append(input);
    controller.update([
      { id: "blocked", keys: "Ctrl+B", run: blocked },
      {
        id: "allowed",
        keys: "Ctrl+Enter",
        run: allowed,
        allowInEditable: true,
      },
    ]);
    controller.attach();

    input.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "b",
        ctrlKey: true,
        bubbles: true,
      }),
    );
    input.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Enter",
        ctrlKey: true,
        bubbles: true,
      }),
    );

    expect(blocked).not.toHaveBeenCalled();
    expect(allowed).toHaveBeenCalledOnce();
    input.remove();
  });

  it("rejects duplicate command ids", () => {
    expect(() =>
      controller.update([
        { id: "save", keys: "Ctrl+S", run: vi.fn() },
        { id: "save", keys: "Meta+S", run: vi.fn() },
      ]),
    ).toThrow("Duplicate keyboard shortcut id: save");
  });

  it("leaves an event already handled by a local control alone", () => {
    const run = vi.fn();
    controller.update([{ id: "open", keys: "Enter", run }]);
    controller.attach();
    const event = new KeyboardEvent("keydown", {
      key: "Enter",
      cancelable: true,
      bubbles: true,
    });
    event.preventDefault();
    document.dispatchEvent(event);

    expect(run).not.toHaveBeenCalled();
  });

  it("ignores IME composition events and legacy keyCode 229", () => {
    const run = vi.fn();
    controller.update([{ id: "open", keys: "Enter", run }]);
    controller.attach();

    document.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Enter",
        isComposing: true,
        bubbles: true,
      }),
    );
    const legacyComposition = new KeyboardEvent("keydown", {
      key: "Enter",
      bubbles: true,
    });
    Object.defineProperty(legacyComposition, "keyCode", { value: 229 });
    document.dispatchEvent(legacyComposition);

    expect(run).not.toHaveBeenCalled();
  });

  it("ignores repeated keydown unless the shortcut opts in", () => {
    const once = vi.fn();
    const repeated = vi.fn();
    controller.update([
      { id: "once", keys: "F5", run: once },
      {
        id: "repeated",
        keys: "ArrowDown",
        run: repeated,
        allowRepeat: true,
      },
    ]);
    controller.attach();

    document.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "F5",
        repeat: true,
        bubbles: true,
      }),
    );
    document.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "ArrowDown",
        repeat: true,
        bubbles: true,
      }),
    );

    expect(once).not.toHaveBeenCalled();
    expect(repeated).toHaveBeenCalledOnce();
  });

  it("suspends resource shortcuts while a modal owns the keyboard", () => {
    const resource = vi.fn();
    const modal = vi.fn();
    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    document.body.append(dialog);
    controller.update([
      {
        id: "resource-back",
        keys: "Escape",
        run: resource,
        scope: "resource",
      },
      {
        id: "modal-close",
        keys: "Escape",
        run: modal,
        scope: "modal",
      },
    ]);
    controller.attach();

    dialog.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Escape",
        bubbles: true,
      }),
    );

    expect(resource).not.toHaveBeenCalled();
    expect(modal).toHaveBeenCalledOnce();
  });

  it("guards descendants of editable and interactive controls independently", () => {
    const blocked = vi.fn();
    const interactiveAllowed = vi.fn();
    const editable = document.createElement("div");
    editable.setAttribute("contenteditable", "true");
    const editableChild = document.createElement("span");
    editable.append(editableChild);
    const button = document.createElement("button");
    const buttonChild = document.createElement("span");
    button.append(buttonChild);
    document.body.append(editable, button);
    controller.update([
      { id: "blocked-editable", keys: "Enter", run: blocked },
      {
        id: "interactive-allowed",
        keys: "Space",
        run: interactiveAllowed,
        allowInInteractive: true,
      },
    ]);
    controller.attach();

    editableChild.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Enter",
        bubbles: true,
      }),
    );
    buttonChild.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Enter",
        bubbles: true,
      }),
    );
    buttonChild.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: " ",
        bubbles: true,
      }),
    );

    expect(blocked).not.toHaveBeenCalled();
    expect(interactiveAllowed).toHaveBeenCalledOnce();
  });
});
