import React from 'react';
import { PRIORITY_LABELS } from '../utils/constants';
import { formatKey } from '../utils/formatters';

const CLASSES = {
  high: 'bg-red-100 text-red-800',
  medium: 'bg-blue-100 text-blue-700',
  low: 'bg-gray-100 text-gray-500',
};

export default function PriorityBadge({ value }) {
  if (!value) return <span className="text-gray-400">—</span>;
  const className = CLASSES[value] || 'bg-gray-100 text-gray-600';
  return (
    <span
      className={`inline-block whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-bold ${className}`}
    >
      {PRIORITY_LABELS[value] || formatKey(value)}
    </span>
  );
}
