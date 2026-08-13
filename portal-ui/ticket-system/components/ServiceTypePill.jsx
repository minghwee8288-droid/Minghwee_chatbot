import React from 'react';
import { serviceTypeLabel } from '../utils/formatters';

/**
 * One value from cb_tickets.service_type. `tone="accent"` marks the topic the
 * ticket was really raised for when the column had to store a substitute.
 */
export default function ServiceTypePill({ value, label, tone = 'default', title }) {
  const classes =
    tone === 'accent'
      ? 'bg-blue-50 text-[#003E60] ring-1 ring-inset ring-blue-200'
      : 'bg-gray-100 text-gray-700';
  return (
    <span
      title={title || undefined}
      className={`inline-block whitespace-nowrap rounded-full px-2 py-0.5 text-xs ${classes}`}
    >
      {label || serviceTypeLabel(value)}
    </span>
  );
}
