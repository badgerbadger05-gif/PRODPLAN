import {
  createElement,
  type ComponentType,
  type ReactNode,
  useEffect,
  useRef,
} from "react";

export type DialogComponentProps<Props> = Props & {
  close: () => void;
};

export type DialogRegistry = Record<
  string,
  ComponentType<DialogComponentProps<never>>
>;

type PropsFor<Component> =
  Component extends ComponentType<DialogComponentProps<infer Props>>
    ? Props
    : never;

export type DialogRequest<Registry extends DialogRegistry> = {
  [Name in keyof Registry]: {
    name: Name;
    props: PropsFor<Registry[Name]>;
    accessibleName?: string;
  };
}[keyof Registry];

export interface DialogHostProps<Registry extends DialogRegistry> {
  dialog: DialogRequest<Registry> | null;
  registry: Registry;
  onClose: () => void;
  fallback?: (name: PropertyKey) => ReactNode;
}

const focusableSelector = [
  "button:not([disabled])",
  "[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

export function DialogHost<Registry extends DialogRegistry>({
  dialog,
  registry,
  onClose,
  fallback = (name) => <>Не удалось открыть диалог «{String(name)}».</>,
}: DialogHostProps<Registry>) {
  const panelRef = useRef<HTMLDivElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!dialog) return;
    returnFocusRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;

    const firstFocusable =
      panelRef.current?.querySelector<HTMLElement>(focusableSelector);
    (firstFocusable ?? panelRef.current)?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key === "Tab") {
        const panel = panelRef.current;
        if (!panel) return;
        const focusable = [...panel.querySelectorAll<HTMLElement>(focusableSelector)];
        if (!focusable.length) {
          event.preventDefault();
          panel.focus();
          return;
        }
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        const active = document.activeElement;
        if (event.shiftKey && (active === first || !panel.contains(active))) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && (active === last || !panel.contains(active))) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKeyDown);

    return () => {
      document.removeEventListener("keydown", onKeyDown);
      returnFocusRef.current?.focus();
      returnFocusRef.current = null;
    };
  }, [dialog, onClose]);

  if (!dialog) return null;

  const Component = registry[dialog.name] as
    | ComponentType<Record<string, unknown> & { close: () => void }>
    | undefined;

  return (
    <div
      data-testid="dialog-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 1000,
        display: "grid",
        placeItems: "center",
        background: "rgb(0 0 0 / 45%)",
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={dialog.accessibleName ?? String(dialog.name)}
        tabIndex={-1}
      >
        {Component ? (
          createElement(Component, {
            ...(dialog.props as object),
            close: onClose,
          })
        ) : (
          fallback(dialog.name)
        )}
      </div>
    </div>
  );
}
