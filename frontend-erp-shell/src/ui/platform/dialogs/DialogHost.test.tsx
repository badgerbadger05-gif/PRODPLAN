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

const registry = { confirm: ConfirmDialog };
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
