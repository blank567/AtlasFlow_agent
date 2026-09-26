"use client";

import { useEffect, useRef } from "react";

export function ConfirmDialog({ title, description, confirmLabel, cancelLabel = "返回", tone = "default", onConfirm, onCancel }: {
  title: string;
  description: string;
  confirmLabel: string;
  cancelLabel?: string;
  tone?: "default" | "danger";
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const onCancelRef = useRef(onCancel);
  onCancelRef.current = onCancel;
  useEffect(() => {
    cancelRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") onCancelRef.current(); };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return <div className="modalBackdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) onCancel(); }}>
    <section className={`modal confirmDialog confirmDialog-${tone}`} role="dialog" aria-modal="true" aria-labelledby="confirm-dialog-title" aria-describedby="confirm-dialog-description">
      <div className="confirmMark" aria-hidden="true">{tone === "danger" ? "!" : "?"}</div>
      <h2 id="confirm-dialog-title">{title}</h2>
      <p id="confirm-dialog-description">{description}</p>
      <div className="modalActions"><button ref={cancelRef} type="button" className="secondaryButton" onClick={onCancel}>{cancelLabel}</button><button type="button" className={tone === "danger" ? "dangerButton" : "primaryButton"} onClick={onConfirm}>{confirmLabel}</button></div>
    </section>
  </div>;
}
