import React from 'react';
import { formatSGT } from '../utils/formatters';

/**
 * captured_info.follow_ups — what the client said about this topic AFTER the
 * ticket was raised.
 *
 * While a ticket is live the bot acknowledges rather than answers, so these
 * messages reached nobody except the transcript. They are the chases and
 * corrections an agent picking the ticket up has not seen. The chatbot keeps
 * the ten most recent.
 */
export default function FollowUpsList({ followUps = [] }) {
  const items = Array.isArray(followUps) ? followUps.filter(Boolean) : [];
  if (!items.length) return null;

  return (
    <ol className="space-y-2">
      {items.map((item, index) => {
        const at = typeof item === 'object' ? item.at : null;
        const message = typeof item === 'object' ? item.message : item;
        return (
          <li
            key={`${at || 'no-time'}-${index}`}
            className="rounded-lg border-l-2 border-amber-400 bg-amber-50/60 px-3 py-2"
          >
            <p className="whitespace-pre-wrap break-words text-sm text-gray-800">{message}</p>
            {at ? <p className="mt-1 text-xs text-gray-500">{formatSGT(at)}</p> : null}
          </li>
        );
      })}
    </ol>
  );
}
