import React, { useEffect } from 'react';
import { createPortal } from 'react-dom';

const AUTO_DISMISS_MS = 3000;

export default function Toast({ message, type = 'success', onDismiss }) {
  useEffect(() => {
    if (!message) return undefined;
    const timer = setTimeout(() => {
      if (onDismiss) onDismiss();
    }, AUTO_DISMISS_MS);
    return () => clearTimeout(timer);
    // Re-armed per message so a second toast gets its own full three seconds.
  }, [message, onDismiss]);

  if (!message) return null;

  const isError = type === 'error';

  // Into <body> for the same reason as the drawer: a fixed element is still a
  // flow child, so a parent's space-y-* margin would nudge it out of its corner.
  return createPortal(
    <div
      role="status"
      aria-live="polite"
      onClick={onDismiss}
      className={`fixed right-4 top-4 z-50 flex max-w-sm cursor-pointer items-start gap-2 rounded-lg px-4 py-3 text-sm font-bold text-white shadow-lg ${
        isError ? 'bg-[#DF0000]' : 'bg-green-600'
      }`}
    >
      <svg
        className="mt-0.5 h-4 w-4 flex-shrink-0"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        strokeWidth="3"
        aria-hidden="true"
      >
        {isError ? (
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 6l12 12M18 6 6 18" />
        ) : (
          <path strokeLinecap="round" strokeLinejoin="round" d="m5 13 4 4L19 7" />
        )}
      </svg>
      <span>{message}</span>
    </div>,
    document.body,
  );
}
