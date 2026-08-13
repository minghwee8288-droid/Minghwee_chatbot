import React from 'react';

/**
 * Placeholder rows shaped like the table they stand in for.
 * `variant="block"` is the same pulse in a single panel, for the charts.
 */
export default function LoadingSkeleton({ rows = 5, columns = 9, variant = 'rows', height = 'h-64' }) {
  if (variant === 'block') {
    return <div className={`w-full animate-pulse rounded-lg bg-gray-100 ${height}`} />;
  }

  return (
    <div className="animate-pulse divide-y divide-gray-100" aria-hidden="true">
      {Array.from({ length: rows }).map((_, rowIndex) => (
        <div key={rowIndex} className="flex items-center gap-4 px-4 py-4">
          {Array.from({ length: columns }).map((__, colIndex) => (
            <div
              key={colIndex}
              className={`h-3 rounded bg-gray-200 ${colIndex === 0 ? 'w-28' : 'flex-1'}`}
            />
          ))}
        </div>
      ))}
    </div>
  );
}
