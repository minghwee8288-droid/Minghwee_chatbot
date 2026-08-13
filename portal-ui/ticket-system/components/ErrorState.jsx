import React from 'react';

/**
 * Supabase hands back an object, not a string. Rendering it directly throws
 * ("objects are not valid as a React child"), which replaces a readable
 * failure with a blank screen.
 */
function toMessage(message) {
  if (!message) return 'Something went wrong.';
  if (typeof message === 'string') return message;
  return message.message || message.details || message.hint || 'Something went wrong.';
}

export default function ErrorState({ message, onRetry }) {
  return (
    <div className="flex items-start gap-3 rounded-lg border border-red-200 bg-red-50 px-4 py-3">
      <svg
        className="mt-0.5 h-5 w-5 flex-shrink-0 text-[#DF0000]"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        strokeWidth="2"
        aria-hidden="true"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M12 9v4m0 4h.01M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z"
        />
      </svg>
      <div className="min-w-0 flex-1">
        <p className="text-sm font-bold text-red-900">Could not load this</p>
        <p className="mt-0.5 break-words text-sm text-red-800">{toMessage(message)}</p>
      </div>
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="flex-shrink-0 rounded-md border border-red-300 bg-white px-3 py-1 text-sm font-bold text-red-800 hover:bg-red-100 focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          Retry
        </button>
      ) : null}
    </div>
  );
}
