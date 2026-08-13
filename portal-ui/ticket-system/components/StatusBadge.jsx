import React from 'react';
import { STATUS_LABELS } from '../utils/constants';
import { formatKey } from '../utils/formatters';

const CLASSES = {
  open: 'bg-blue-100 text-blue-800',
  in_progress: 'bg-amber-100 text-amber-800',
  resolved: 'bg-green-100 text-green-800',
  closed: 'bg-gray-100 text-gray-500',
};

export default function StatusBadge({ value }) {
  if (!value) return <span className="text-gray-400">—</span>;
  const className = CLASSES[value] || 'bg-gray-100 text-gray-600';
  return (
    <span
      className={`inline-block whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-bold ${className}`}
    >
      {STATUS_LABELS[value] || formatKey(value)}
    </span>
  );
}
