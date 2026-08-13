import React from 'react';
import { formatKey, formatValue } from '../utils/formatters';

/**
 * cb_tickets.captured_info, rendered for a person.
 *
 * The chatbot does not store a flat bag. structure_captured() groups what it
 * collected into `contact`, `details` and `notes`, and the ticket writer adds
 * its own bookkeeping alongside. A flat key/value renderer prints "[object
 * Object]" for every one of those groups, so nested values are walked instead.
 *
 * Keys the drawer surfaces on their own — the topic the bot is blocked on, the
 * follow-ups since — are passed in `skipKeys` so they are not repeated here.
 */

/** Group order, so the agent reads who is asking before what they asked for. */
const GROUP_ORDER = ['contact', 'details', 'notes'];

const GROUP_LABELS = {
  contact: 'Contact',
  details: 'Captured Details',
  notes: 'Notes',
};

function isGroup(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function DefinitionRow({ label, value }) {
  return (
    <div className="grid grid-cols-3 gap-3 py-1.5">
      <dt className="col-span-1 break-words text-xs font-bold uppercase tracking-wide text-gray-500">
        {label}
      </dt>
      <dd className="col-span-2 whitespace-pre-wrap break-words text-sm text-gray-800">{value}</dd>
    </div>
  );
}

function Group({ title, entries }) {
  if (!entries.length) return null;
  return (
    <div className="border-t border-gray-200 pt-3 first:border-t-0 first:pt-0">
      {title ? (
        <h5 className="mb-1 text-xs font-bold uppercase tracking-wide text-[#003E60]">{title}</h5>
      ) : null}
      <dl className="divide-y divide-gray-200/70">
        {entries.map(([key, value]) => (
          <DefinitionRow key={key} label={formatKey(key)} value={formatValue(value)} />
        ))}
      </dl>
    </div>
  );
}

export default function CapturedInfoPanel({ capturedInfo, skipKeys = [] }) {
  const info = isGroup(capturedInfo) ? capturedInfo : null;

  if (!info || Object.keys(info).length === 0) {
    return (
      <div className="rounded-lg bg-gray-50 px-4 py-3 text-sm text-gray-500">
        No structured data captured
      </div>
    );
  }

  const skip = new Set(skipKeys);
  const entries = Object.entries(info).filter(
    ([key, value]) => !skip.has(key) && value !== null && value !== undefined && value !== '',
  );

  // A nested object becomes its own titled group; everything else stays at the
  // top of the panel under no heading.
  const groups = entries.filter(([, value]) => isGroup(value));
  const flat = entries.filter(([, value]) => !isGroup(value));

  groups.sort(([a], [b]) => {
    const indexA = GROUP_ORDER.indexOf(a);
    const indexB = GROUP_ORDER.indexOf(b);
    return (indexA === -1 ? GROUP_ORDER.length : indexA) - (indexB === -1 ? GROUP_ORDER.length : indexB);
  });

  if (!groups.length && !flat.length) {
    return (
      <div className="rounded-lg bg-gray-50 px-4 py-3 text-sm text-gray-500">
        No structured data captured
      </div>
    );
  }

  return (
    <div className="space-y-3 rounded-lg bg-gray-50 px-4 py-3">
      <Group title={null} entries={flat} />
      {groups.map(([key, value]) => (
        <Group
          key={key}
          title={GROUP_LABELS[key] || formatKey(key)}
          entries={Object.entries(value).filter(
            ([, inner]) => inner !== null && inner !== undefined && inner !== '',
          )}
        />
      ))}
    </div>
  );
}
