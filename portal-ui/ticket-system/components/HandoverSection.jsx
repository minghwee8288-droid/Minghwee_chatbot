import React from 'react';
import {
  agentName,
  formatSGT,
  handoverDirectionLabel,
  handoverReasonLabel,
} from '../utils/formatters';

/**
 * cb_handovers for this ticket and its conversation — every time the thread
 * moved between the bot and a person, and why. Read-only: only the chatbot
 * writes this table.
 */

const BOT_TO_HUMAN = 'bot_to_human';

export default function HandoverSection({ handovers = [], agents = [], ticketId }) {
  if (!handovers.length) return null;

  const agentById = new Map((agents || []).map((agent) => [String(agent.id), agent]));

  return (
    <ol className="space-y-2">
      {handovers.map((handover) => {
        // The hook attaches the profile it fetched; the agents list passed from
        // the drawer covers a row it could not resolve.
        const profile =
          handover.agent ||
          (handover.agent_profile_id ? agentById.get(String(handover.agent_profile_id)) : null);
        const name = agentName(profile);
        const toHuman = handover.direction === BOT_TO_HUMAN;
        const onThisTicket = ticketId && handover.ticket_id === ticketId;

        return (
          <li
            key={handover.id}
            className={`rounded-lg border px-3 py-2 ${
              onThisTicket ? 'border-blue-200 bg-blue-50/50' : 'border-gray-200 bg-white'
            }`}
          >
            <div className="flex flex-wrap items-center gap-2">
              <span
                className={`rounded-full px-2 py-0.5 text-xs font-bold ${
                  toHuman ? 'bg-amber-100 text-amber-800' : 'bg-green-100 text-green-800'
                }`}
              >
                {handoverDirectionLabel(handover.direction)}
              </span>
              <span className="text-sm text-gray-800">
                {handoverReasonLabel(handover.reason)}
              </span>
              {onThisTicket ? (
                <span className="rounded-full bg-blue-100 px-2 py-0.5 text-xs font-bold text-blue-800">
                  This ticket
                </span>
              ) : null}
            </div>
            <div className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-gray-500">
              <span>{formatSGT(handover.created_at)}</span>
              {handover.agent_profile_id ? (
                <span>Agent: {name || handover.agent_profile_id}</span>
              ) : null}
              {handover.ticket_id && !onThisTicket ? (
                <span>Other ticket on this conversation</span>
              ) : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
