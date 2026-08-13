import React from 'react';

export default function EmptyState({ message, title = 'No tickets found' }) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-16 text-center">
      <svg
        className="mb-3 h-10 w-10 text-gray-300"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        strokeWidth="1.5"
        aria-hidden="true"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M16.5 6h-9A2.5 2.5 0 0 0 5 8.5v1a2 2 0 0 1 0 4v1A2.5 2.5 0 0 0 7.5 17h9a2.5 2.5 0 0 0 2.5-2.5v-1a2 2 0 0 1 0-4v-1A2.5 2.5 0 0 0 16.5 6Z"
        />
      </svg>
      <h3 className="text-sm font-bold text-gray-700">{title}</h3>
      {message ? <p className="mt-1 max-w-sm text-sm text-gray-500">{message}</p> : null}
    </div>
  );
}
