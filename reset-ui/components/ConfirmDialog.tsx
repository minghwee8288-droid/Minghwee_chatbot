'use client';

/**
 * The confirmation step -- the only thing standing between a mistyped digit
 * and somebody else's transcript.
 *
 * It does not ask "are you sure?" and leave it there. It names the contact and
 * itemises what was found on that number a moment ago, so the operator can see
 * at a glance whether this is the person they meant. A confirmation nobody
 * reads is not a safeguard, and the terminal version this replaces gave no
 * warning at all -- which is why the contact's name is the largest thing on it.
 */

import type { LookupResult } from '@/lib/types';
import { prettyPhone } from '@/lib/phone';
import { Pill } from './ui';

export function ConfirmDialog({
  lookup,
  busy,
  onCancel,
  onConfirm,
}: {
  lookup: LookupResult;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const conversation = lookup.conversation;
  const leads = lookup.leads;
  const who = lookup.contact.name || conversation?.customerName || 'Unknown contact';
  const blocked = leads.filter((lead) => lead.blockers.length > 0);
  const deletable = leads.filter((lead) => lead.blockers.length === 0);

  const counts: [string, number][] = conversation
    ? [
        ['Messages', conversation.counts.messages],
        ['Tickets', conversation.counts.tickets],
        ['Handovers', conversation.counts.handovers],
      ]
    : [];

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center overflow-y-auto bg-slate-900/60 p-4 backdrop-blur-sm sm:p-8"
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-title"
    >
      <div className="my-auto w-full max-w-2xl overflow-hidden rounded-2xl bg-white shadow-2xl">
        {/* Who, first and largest. The single most useful thing on this dialog
            is whether the name matches the person they meant to clear. */}
        <div className="border-b border-slate-200 bg-slate-50 px-8 py-6">
          <p className="text-xs font-bold uppercase tracking-widest text-danger">
            Confirm deletion
          </p>
          <h2 id="confirm-title" className="mt-2 text-2xl font-black leading-snug text-slate-900">
            {who}
          </h2>
          <p className="mt-1 text-lg font-semibold text-slate-600">
            {prettyPhone(lookup.phone)}
          </p>
        </div>

        <div className="space-y-5 px-8 py-6">
          <p className="text-base leading-relaxed text-slate-700">
            Are you sure you want to clear the conversation for this contact and lead?{' '}
            <span className="font-bold text-danger">This cannot be undone.</span>
          </p>

          {counts.length ? (
            <div className="grid grid-cols-3 gap-3">
              {counts.map(([label, count]) => (
                <div key={label} className="rounded-xl bg-slate-50 p-4 text-center">
                  <p className="text-3xl font-black text-brand">{count}</p>
                  <p className="mt-1 text-xs font-bold uppercase tracking-wide text-slate-500">
                    {label}
                  </p>
                </div>
              ))}
            </div>
          ) : (
            <div className="rounded-xl bg-slate-50 p-4 text-sm text-slate-600">
              <span className="font-bold">No chat thread</span> on this number — only the lead
              below will be deleted.
            </div>
          )}

          <div>
            <p className="text-xs font-bold uppercase tracking-widest text-slate-500">
              Also deleted
            </p>
            <ul className="mt-3 space-y-2">
              {conversation ? (
                <li className="flex items-start gap-3 text-sm text-slate-700">
                  <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-400" />
                  <span>
                    The bot&rsquo;s saved memory for this thread
                    {lookup.checkpointsReachable
                      ? ''
                      : ' (skipped — not configured on the server)'}
                  </span>
                </li>
              ) : null}

              {deletable.map((lead) => (
                <li key={lead.id} className="flex items-start gap-3 text-sm text-slate-800">
                  <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-danger" />
                  <span>
                    <span className="font-bold">{lead.leadNumber ?? lead.id.slice(0, 8)}</span> —{' '}
                    {lead.fullName ?? 'no name'}{' '}
                    <Pill tone={lead.kind === 'employer' ? 'red' : 'amber'}>
                      {lead.kind === 'employer' ? 'employer lead' : 'candidate lead'}
                    </Pill>
                  </span>
                </li>
              ))}

              {leads.length === 0 ? (
                <li className="flex items-start gap-3 text-sm text-slate-500">
                  <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-300" />
                  <span>No lead on this number.</span>
                </li>
              ) : null}
            </ul>
          </div>

          {blocked.length ? (
            <div className="rounded-xl border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">
              <p className="font-bold">
                {blocked.length === 1
                  ? 'One lead cannot be deleted'
                  : `${blocked.length} leads cannot be deleted`}{' '}
                and will be left in place.
              </p>
              <ul className="mt-2 space-y-1 text-xs leading-relaxed">
                {blocked.map((lead) => (
                  <li key={lead.id}>
                    <span className="font-semibold">
                      {lead.leadNumber ?? lead.id.slice(0, 8)}
                    </span>{' '}
                    — {lead.blockers[0].reason}
                  </li>
                ))}
              </ul>
              <p className="mt-2 text-xs font-semibold">The conversation is still cleared.</p>
            </div>
          ) : null}

          {deletable.some((lead) => lead.kind === 'employer') ? (
            <div className="rounded-xl border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">
              <span className="font-bold">This includes an employer lead.</span> That table holds
              the sales pipeline the team works from.
            </div>
          ) : null}

          {conversation ? (
            <p className="text-sm leading-relaxed text-slate-500">
              The conversation itself is kept and handed back to the bot: it goes back to{' '}
              <code className="rounded bg-slate-100 px-1.5 py-0.5 text-xs">bot_active</code> with a
              fresh thread, so the next message starts a new enquiry.
            </p>
          ) : null}
        </div>

        <div className="flex flex-col-reverse gap-3 border-t border-slate-200 bg-slate-50 px-8 py-5 sm:flex-row sm:justify-end">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="rounded-xl border border-slate-300 bg-white px-6 py-3 text-sm font-bold text-slate-700 transition hover:bg-slate-100 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className="rounded-xl bg-danger px-6 py-3 text-sm font-black text-white shadow-lg shadow-danger/25 transition hover:bg-red-700 disabled:opacity-60"
          >
            {busy ? 'Clearing…' : 'Yes, clear it'}
          </button>
        </div>
      </div>
    </div>
  );
}
