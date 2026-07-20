import { useEffect, useRef } from "react";
import {
  KeyboardShortcutController,
  type KeyboardShortcut,
} from "./KeyboardShortcutController";

export interface KeyboardShortcutShellProps {
  shortcuts: readonly KeyboardShortcut[];
}

export function KeyboardShortcutShell({
  shortcuts,
}: KeyboardShortcutShellProps) {
  const controllerRef = useRef<KeyboardShortcutController | null>(null);
  controllerRef.current ??= new KeyboardShortcutController();

  useEffect(() => {
    const controller = controllerRef.current!;
    controller.update(shortcuts);
    controller.attach();
    return () => controller.detach();
  }, [shortcuts]);

  return null;
}
