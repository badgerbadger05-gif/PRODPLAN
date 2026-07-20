export interface KeyboardShortcut {
  id: string;
  keys: string;
  run: () => void;
  enabled?: () => boolean;
  allowInEditable?: boolean;
  allowInInteractive?: boolean;
  allowRepeat?: boolean;
  scope?: "global" | "resource" | "modal";
  preventDefault?: boolean;
}

function isEditable(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && Boolean(
    target.closest("input, textarea, select, [contenteditable='true'], [role='textbox']"),
  );
}

function isInteractive(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && Boolean(
    target.closest("button, a[href], [role='button'], [role='menuitem']"),
  );
}

export function normalizeShortcut(value: string): string {
  const aliases: Record<string, string> = {
    command: "meta",
    cmd: "meta",
    control: "ctrl",
    option: "alt",
    esc: "escape",
    " ": "space",
  };
  const order = ["ctrl", "alt", "shift", "meta"];
  const parts = value
    .toLowerCase()
    .split("+")
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => aliases[part] ?? part);
  const key = parts.find((part) => !order.includes(part));
  return [...order.filter((modifier) => parts.includes(modifier)), key]
    .filter(Boolean)
    .join("+");
}

function shortcutFromEvent(event: KeyboardEvent): string {
  const modifiers = [
    event.ctrlKey && "ctrl",
    event.altKey && "alt",
    event.shiftKey && "shift",
    event.metaKey && "meta",
  ].filter(Boolean);
  const rawKey = event.key.toLowerCase();
  const keyAliases: Record<string, string> = { " ": "space", esc: "escape" };
  const key = keyAliases[rawKey] ?? rawKey;
  return [...modifiers, key].join("+");
}

export class KeyboardShortcutController {
  private shortcuts: KeyboardShortcut[] = [];
  private target: Document | null = null;

  private readonly handleKeyDown = (event: KeyboardEvent) => {
    if (event.defaultPrevented || event.isComposing || event.keyCode === 229) return;
    const pressed = shortcutFromEvent(event);
    const modalOpen = Boolean(document.querySelector('[role="dialog"][aria-modal="true"]'));
    const shortcut = this.shortcuts.find(
      (candidate) =>
        normalizeShortcut(candidate.keys) === pressed &&
        (!event.repeat || candidate.allowRepeat) &&
        (modalOpen ? candidate.scope === "modal" : candidate.scope !== "modal") &&
        (candidate.enabled?.() ?? true) &&
        (candidate.allowInEditable || !isEditable(event.target)) &&
        (candidate.allowInInteractive || !isInteractive(event.target)),
    );
    if (!shortcut) return;
    if (shortcut.preventDefault !== false) event.preventDefault();
    shortcut.run();
  };

  update(shortcuts: readonly KeyboardShortcut[]) {
    const ids = new Set<string>();
    for (const shortcut of shortcuts) {
      if (ids.has(shortcut.id)) {
        throw new Error(`Duplicate keyboard shortcut id: ${shortcut.id}`);
      }
      ids.add(shortcut.id);
    }
    this.shortcuts = [...shortcuts];
  }

  attach(target: Document = document) {
    if (this.target === target) return;
    this.detach();
    this.target = target;
    target.addEventListener("keydown", this.handleKeyDown);
  }

  detach() {
    this.target?.removeEventListener("keydown", this.handleKeyDown);
    this.target = null;
  }
}
