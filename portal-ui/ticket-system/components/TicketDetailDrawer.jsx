import React, { useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import useTicketDetail from '../hooks/useTicketDetail';
import {
  PRIORITY_LABELS,
  PRIORITY_OPTIONS,
  STATUS_LABELS,
  STATUS_OPTIONS,
  priorityLevel,
} from '../utils/constants';
import {
  DASH,
  agentName,
  asServiceArray,
  contactTypeLabel,
  formatPhone,
  formatSGT,
} from '../utils/formatters';
import BotContextBanner from './BotContextBanner';
import CapturedInfoPanel from './CapturedInfoPanel';
import ConversationThread from './ConversationThread';
import ErrorState from './ErrorState';
import FollowUpsList from './FollowUpsList';
import HandoverSection from './HandoverSection';
import LoadingSkeleton from './LoadingSkeleton';
import ServiceTypePill from './ServiceTypePill';

/**
 * One ticket, in full, with the three fields the portal may change.
 *
 * captured_info keys the panel must not print: the first four this drawer
 * already surfaces in their own sections, and `notes` — the chatbot's internal
 * running commentary on its own state ("Client is answering the consultant's
 * question…"), which is not information about the enquiry.
 */
const HANDLED_INFO_KEYS = ['topic_key', 'enquiry_type', 'also_topics', 'follow_ups', 'notes'];

const FIELD_LABEL = 'text-xs font-bold uppercase tracking-wide text-gray-500';
const SELECT_CLASS =
  'mt-1 w-full rounded-md border border-gray-300 bg-white px-2 py-1.5 text-sm text-gray-800 focus:outline-none focus:ring-2 focus:ring-blue-500';

function Row({ label, className = '', children }) {
  return (
    <div className={`min-w-0 ${className}`}>
      <p className={FIELD_LABEL}>{label}</p>
      <div className="mt-1 text-sm text-gray-800">{children}</div>
    </div>
  );
}

function SectionHeading({ children, count }) {
  return (
    <h4 className="mb-2 text-sm font-bold text-[#003E60]">
      {children}
      {typeof count === 'number' ? (
        <span className="ml-1 font-normal text-gray-400">({count})</span>
      ) : null}
    </h4>
  );
}

/**
 * cb_tickets.updated_at is not written by the chatbot and may not exist on the
 * column list at all. Sending it unconditionally would make a missing column
 * reject the whole PATCH — the status change and the reassignment with it. So
 * it is sent optimistically and dropped on the one error that means "no such
 * column", rather than being left out of a schema that does have it.
 */
function isMissingColumn(error) {
  if (!error) return false;
  if (error.code === '42703' || error.code === 'PGRST204') return true;
  return /updated_at/i.test(error.message || '') && /column|schema cache/i.test(error.message || '');
}

export default function TicketDetailDrawer({
  supabaseClient,
  ticketId,
  agents = [],
  onClose,
  onSaveSuccess,
  onToast,
}) {
  const { ticket, conversation, messages, handovers, loading, error, refetch } = useTicketDetail({
    supabaseClient,
    ticketId,
  });

  const [selectedStatus, setSelectedStatus] = useState('');
  const [selectedPriority, setSelectedPriority] = useState('');
  const [selectedAgentId, setSelectedAgentId] = useState('');
  const [isSaving, setIsSaving] = useState(false);

  // Seeded from the row each time a different ticket loads. Keyed on the id as
  // well as the object so a refetch of the same ticket does not discard an
  // edit the agent has not saved yet.
  useEffect(() => {
    if (!ticket) return;
    setSelectedStatus(ticket.status || '');
    // A row with the column empty still has to land on a value the select can
    // show, or it would open blank and save that blank back.
    setSelectedPriority(priorityLevel(ticket.priority));
    setSelectedAgentId(ticket.assigned_agent_id || '');
  }, [ticket?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const onKeyDown = (event) => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKeyDown);
    // The page behind a full-height drawer must not scroll with it.
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [onClose]);

  const info = useMemo(
    () => (ticket?.captured_info && typeof ticket.captured_info === 'object' ? ticket.captured_info : {}),
    [ticket],
  );
  const followUps = Array.isArray(info.follow_ups) ? info.follow_ups : [];
  const services = asServiceArray(ticket?.service_type);

  const isDirty =
    Boolean(ticket) &&
    (selectedStatus !== (ticket.status || '') ||
      selectedPriority !== priorityLevel(ticket.priority) ||
      (selectedAgentId || '') !== (ticket.assigned_agent_id || ''));

  const handleSave = async () => {
    if (!ticket) return;
    setIsSaving(true);

    const base = {
      status: selectedStatus,
      priority: selectedPriority,
      assigned_agent_id: selectedAgentId || null,
    };

    let { data: savedRows, error: saveError } = await supabaseClient
      .from('cb_tickets')
      .update({ ...base, updated_at: new Date().toISOString() })
      .eq('id', ticket.id)
      .select('id');

    if (saveError && isMissingColumn(saveError)) {
      console.warn('cb_tickets has no updated_at column — saving without it', saveError);
      ({ data: savedRows, error: saveError } = await supabaseClient
        .from('cb_tickets')
        .update(base)
        .eq('id', ticket.id)
        .select('id'));
    }

    setIsSaving(false);

    if (saveError) {
      console.error('Ticket save failed', saveError);
      if (onToast) onToast('Save failed — please try again', 'error');
      return;
    }

    // RLS silently matches zero rows instead of erroring when the caller has
    // no write policy — without this check that reads as a normal save and
    // the ticket quietly keeps its old status.
    if (!savedRows || savedRows.length === 0) {
      console.error('Ticket save matched no rows — likely blocked by RLS', ticket.id);
      if (onToast) onToast('Save failed — you may not have permission to edit this ticket', 'error');
      return;
    }

    if (onToast) onToast('Ticket updated successfully', 'success');
    await refetch();
    if (onSaveSuccess) onSaveSuccess();
  };

  /**
   * Rendered into <body>, not in place.
   *
   * A fixed element is still a flow child of its parent, so a parent's
   * `space-y-*` (the module's own page wrapper has one) puts a margin on it and
   * pushes it off the viewport — the drawer's footer fell below the fold by
   * exactly that margin. A transformed ancestor would break it differently, by
   * making `fixed` behave like `absolute`. The portal is immune to both, which
   * matters most in the real portal, whose page chrome this module cannot see.
   */
  return createPortal(
    <>
      <div
        className="fixed inset-0 z-30 bg-black/40"
        onClick={onClose}
        aria-hidden="true"
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Ticket details"
        // Pinned top and bottom rather than sized with h-screen: 100vh can
        // exceed what is actually visible when a mobile browser's URL bar is
        // showing, which hides the footer's buttons.
        className="fixed inset-y-0 right-0 z-40 flex w-full flex-col bg-white shadow-2xl sm:w-[560px] lg:w-[640px]"
      >
        <header className="flex items-start justify-between gap-3 border-b border-gray-200 px-5 py-4">
          <div className="min-w-0">
            <h3 className="truncate text-lg font-black text-[#003E60]">
              {ticket?.ticket_number || (loading ? 'Loading…' : 'Ticket')}
            </h3>
            <p className="text-xs text-gray-500">
              {ticket ? `Raised ${formatSGT(ticket.created_at)} SGT` : ''}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="flex-shrink-0 rounded-md px-2 py-1 text-xl leading-none text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            ×
          </button>
        </header>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          {error ? (
            <ErrorState message={error} onRetry={refetch} />
          ) : loading || !ticket ? (
            <div className="space-y-4">
              <LoadingSkeleton variant="block" height="h-24" />
              <LoadingSkeleton variant="block" height="h-32" />
              <LoadingSkeleton variant="block" height="h-48" />
            </div>
          ) : (
            <div className="space-y-5">
              <BotContextBanner ticket={ticket} conversation={conversation} />

              {/* The writable fields, boxed away from the read-only facts
                  below — otherwise the selects sit in a stack of plain rows and
                  nothing says which parts of this drawer do anything. */}
              <div className="rounded-lg border border-gray-200 bg-gray-50 p-4">
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <label className="min-w-0">
                    <span className={FIELD_LABEL}>Status</span>
                    <select
                      value={selectedStatus}
                      onChange={(event) => setSelectedStatus(event.target.value)}
                      className={SELECT_CLASS}
                    >
                      {STATUS_OPTIONS.map((status) => (
                        <option key={status} value={status}>
                          {STATUS_LABELS[status]}
                        </option>
                      ))}
                    </select>
                  </label>
                  {/* The chatbot sets this once at insert — High for an abuse
                      report, Medium for everything else — and never revisits
                      it. This select is how a ticket gets re-triaged
                      afterwards. */}
                  <label className="min-w-0">
                    <span className={FIELD_LABEL}>Priority</span>
                    <select
                      value={selectedPriority}
                      onChange={(event) => setSelectedPriority(event.target.value)}
                      className={SELECT_CLASS}
                    >
                      {PRIORITY_OPTIONS.map((priority) => (
                        <option key={priority} value={priority}>
                          {PRIORITY_LABELS[priority]}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="min-w-0 sm:col-span-2">
                    <span className={FIELD_LABEL}>Assigned Agent</span>
                    <select
                      value={selectedAgentId}
                      onChange={(event) => setSelectedAgentId(event.target.value)}
                      className={SELECT_CLASS}
                    >
                      <option value="">Unassigned</option>
                      {agents.map((agent) => (
                        <option key={agent.id} value={agent.id}>
                          {agentName(agent) || agent.id}
                        </option>
                      ))}
                      {/* The assigned profile may sit outside the list the
                          dropdown loaded; without this the select would fall
                          back to "Unassigned" and quietly clear it on save. */}
                      {ticket.assigned_agent_id &&
                      !agents.some((a) => String(a.id) === String(ticket.assigned_agent_id)) ? (
                        <option value={ticket.assigned_agent_id}>
                          {agentName(ticket.agent) || ticket.assigned_agent_id}
                        </option>
                      ) : null}
                    </select>
                  </label>
                </div>
              </div>

              {/* One grid rather than four stacked pairs, so the labels keep a
                  single rhythm down the drawer instead of drifting apart. */}
              <div className="grid grid-cols-2 gap-x-4 gap-y-4">
                {/* Spans the row: a merged ticket can carry several pills, and
                    priority no longer sits beside it now that it is editable. */}
                <Row label="Service Type" className="col-span-2">
                  <div className="flex flex-wrap gap-1">
                    {services.length ? (
                      services.map((service) => <ServiceTypePill key={service} value={service} />)
                    ) : (
                      <span className="text-gray-400">{DASH}</span>
                    )}
                  </div>
                </Row>
                <Row label="Customer">
                  <div>{formatPhone(conversation?.customer_number)}</div>
                  {conversation?.customer_name ? (
                    <div className="text-xs text-gray-500">{conversation.customer_name}</div>
                  ) : null}
                </Row>
                <Row label="Contact Type">{contactTypeLabel(conversation?.contact_type)}</Row>
                <Row label="Linked Case / Lead">
                  {ticket.created_lead_id ? (
                    <span className="break-all font-mono text-xs">{ticket.created_lead_id}</span>
                  ) : (
                    <span className="text-gray-400">{DASH}</span>
                  )}
                </Row>
                <Row label="Assignment Rule">
                  {ticket.assignment_rule ? (
                    ticket.assignment_rule.replace(/_/g, ' ')
                  ) : (
                    <span className="text-gray-400">{DASH}</span>
                  )}
                </Row>
              </div>

              {/* cb_tickets carries a resolution trio the chatbot never
                  writes — it is the portal's own. Shown when present, never
                  written from here: this module only owns status and the
                  assignment. */}
              {ticket.resolved_at || ticket.resolution_note ? (
                <div className="rounded-lg border border-green-200 bg-green-50 px-4 py-3">
                  <p className={FIELD_LABEL}>Resolution</p>
                  {ticket.resolution_note ? (
                    <p className="mt-1 whitespace-pre-wrap break-words text-sm text-gray-800">
                      {ticket.resolution_note}
                    </p>
                  ) : null}
                  {ticket.resolved_at ? (
                    <p className="mt-1 text-xs text-gray-500">
                      Resolved {formatSGT(ticket.resolved_at)} SGT
                    </p>
                  ) : null}
                </div>
              ) : null}

              <div>
                <SectionHeading>Description</SectionHeading>
                <div className="whitespace-pre-wrap break-words rounded-lg bg-gray-50 px-4 py-3 text-sm text-gray-800">
                  {ticket.description?.trim() || 'No description recorded.'}
                </div>
              </div>

              <div>
                <SectionHeading>Captured Info</SectionHeading>
                <CapturedInfoPanel capturedInfo={ticket.captured_info} skipKeys={HANDLED_INFO_KEYS} />
              </div>

              {followUps.length ? (
                <div>
                  <SectionHeading count={followUps.length}>Since this ticket was raised</SectionHeading>
                  <p className="mb-2 text-xs text-gray-500">
                    Messages the client sent about this topic while the bot was holding it. Nobody
                    has replied to these.
                  </p>
                  <FollowUpsList followUps={followUps} />
                </div>
              ) : null}

              <hr className="border-gray-200" />

              <div>
                <SectionHeading count={messages.length}>Conversation</SectionHeading>
                <ConversationThread messages={messages} />
              </div>

              {handovers.length ? (
                <>
                  <hr className="border-gray-200" />
                  <div>
                    <SectionHeading count={handovers.length}>Handover Records</SectionHeading>
                    <HandoverSection
                      handovers={handovers}
                      agents={agents}
                      ticketId={ticket.id}
                    />
                  </div>
                </>
              ) : null}
            </div>
          )}
        </div>

        <footer className="flex items-center justify-end gap-2 border-t border-gray-200 px-5 py-3">
          {isDirty ? (
            <p className="mr-auto text-xs text-amber-700">Unsaved changes</p>
          ) : null}
          <button
            type="button"
            onClick={onClose}
            disabled={isSaving}
            className="rounded-md border border-gray-300 px-4 py-2 text-sm font-bold text-gray-700 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={isSaving || loading || !ticket || !isDirty}
            className="flex items-center gap-2 rounded-md bg-[#0D7AD2] px-4 py-2 text-sm font-bold text-white hover:bg-[#0b68b3] focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {isSaving ? (
              <>
                <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white" />
                Saving…
              </>
            ) : (
              'Save Changes'
            )}
          </button>
        </footer>
      </aside>
    </>,
    document.body,
  );
}
