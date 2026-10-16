"use client";

import { ReactNode, useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";

/** Native top-layer dialog keeps background controls inert while details load. */
export function EvaluationDetailDialog({ description, onClose, children }: {
  description: string; onClose: () => void; children: ReactNode;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose); onCloseRef.current = onClose;
  const titleId = useId(), descriptionId = useId();
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (!restoreFocusRef.current) restoreFocusRef.current = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const cancel = (event: Event) => { event.preventDefault(); onCloseRef.current(); };
    // StrictMode may reopen this same element before an earlier queued close
    // event is dispatched. Only a currently closed dialog should dismiss state.
    const closed = () => { if (!dialog.open) onCloseRef.current(); };
    const keydown = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const controls = Array.from(dialog.querySelectorAll<HTMLElement>(
        "button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex='0']",
      )).filter(control => control.getClientRects().length > 0 && control.getAttribute("aria-hidden") !== "true");
      const first = controls[0], last = controls[controls.length - 1];
      if (!first || !last) { event.preventDefault(); closeRef.current?.focus(); return; }
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    dialog.addEventListener("cancel", cancel);
    dialog.addEventListener("close", closed);
    dialog.addEventListener("keydown", keydown);
    if (!dialog.open) dialog.showModal();
    closeRef.current?.focus({ preventScroll: true });
    return () => {
      dialog.removeEventListener("cancel", cancel);
      dialog.removeEventListener("close", closed);
      dialog.removeEventListener("keydown", keydown);
      if (dialog.open) dialog.close();
      document.body.style.overflow = previousOverflow;
      if (restoreFocusRef.current?.isConnected) restoreFocusRef.current.focus({ preventScroll: true });
    };
  }, []);
  if (typeof document === "undefined") return null;
  return createPortal(<dialog ref={dialogRef} className="evaluationDetailDialog" data-evaluation-dialog="true"
    role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={descriptionId}
    onClick={event => {
      if (event.target !== event.currentTarget) return;
      const bounds = event.currentTarget.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) onCloseRef.current();
    }}>
    <header className="evaluationDetailHeader"><div><h2 id={titleId}>逐题详情</h2><p id={descriptionId}>{description}</p></div>
      <button ref={closeRef} className="secondaryButton" type="button" onClick={() => onCloseRef.current()}>关闭详情</button>
    </header>
    <div className="evaluationV2 evaluationDetailContent">{children}</div>
  </dialog>, document.body);
}
