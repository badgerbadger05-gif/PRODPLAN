import { afterEach, describe, expect, it, vi } from "vitest";
import {
  KeyboardShortcutController,
  normalizeShortcut,
} from "./KeyboardShortcutController";

describe("KeyboardShortcutController", () => {
  const controller = new KeyboardShortcutController();
  afterEach(() => controller.detach());

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
});
