import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import {
  DialogHost,
  type DialogComponentProps,
  type DialogRequest,
} from "./DialogHost";

function ConfirmDialog({
  message,
  close,
}: DialogComponentProps<{ message: string }>) {
  return (
    <section>
      <p>{message}</p>
      <button onClick={close}>Закрыть</button>
    </section>
  );
}

function FocusDialog({ close }: DialogComponentProps<object>) {
  return (
    <section>
      <button>Первое действие</button>
      <input aria-label="Комментарий" />
      <button onClick={close}>Последнее действие</button>
    </section>
  );
}

const registry = { confirm: ConfirmDialog, focus: FocusDialog };
type Request = DialogRequest<typeof registry>;

function Harness({ onClose = () => undefined }: { onClose?: () => void }) {
  const [dialog, setDialog] = useState<Request | null>(null);
  return (
    <>
      <button
        onClick={() =>
          setDialog({
            name: "confirm",
            props: { message: "Продолжить операцию?" },
            accessibleName: "Подтверждение",
          })
        }
      >
        Открыть
      </button>
      <DialogHost
        dialog={dialog}
        registry={registry}
        onClose={() => {
          onClose();
          setDialog(null);
        }}
      />
    </>
  );
}

describe("DialogHost", () => {
  it("renders a typed dialog with accessible modal semantics", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Открыть" }));

    const dialog = screen.getByRole("dialog", { name: "Подтверждение" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByText("Продолжить операцию?")).toBeVisible();
    expect(screen.getByRole("button", { name: "Закрыть" })).toHaveFocus();
  });

  it("closes on Escape and restores focus", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    const trigger = screen.getByRole("button", { name: "Открыть" });
    await user.click(trigger);
    await user.keyboard("{Escape}");

    expect(onClose).toHaveBeenCalledOnce();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("traps Tab and Shift+Tab focus inside the dialog", async () => {
    const user = userEvent.setup();
    render(
      <DialogHost
        dialog={{ name: "focus", props: {}, accessibleName: "Проверка фокуса" }}
        registry={registry}
        onClose={() => undefined}
      />,
    );

    const first = screen.getByRole("button", { name: "Первое действие" });
    const last = screen.getByRole("button", { name: "Последнее действие" });
    expect(first).toHaveFocus();

    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(last).toHaveFocus();

    await user.keyboard("{Tab}");
    expect(first).toHaveFocus();
  });

  it("closes only when the backdrop itself is clicked", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    await user.click(screen.getByRole("button", { name: "Открыть" }));

    await user.click(screen.getByText("Продолжить операцию?"));
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeVisible();

    await user.click(screen.getByTestId("dialog-backdrop"));
    expect(onClose).toHaveBeenCalledOnce();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("uses the fallback for a registry miss at runtime", () => {
    render(
      <DialogHost
        dialog={{ name: "removed", props: {} } as never}
        registry={registry}
        onClose={() => undefined}
        fallback={(name) => <p>Диалог {String(name)} недоступен</p>}
      />,
    );
    expect(screen.getByText("Диалог removed недоступен")).toBeVisible();
  });
});
