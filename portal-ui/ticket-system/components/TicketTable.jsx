import React from 'react';
import { OPEN_STATUSES } from '../utils/constants';
import {
  DASH,
  agentName,
  asServiceArray,
  firstLine,
  formatPhone,
  formatSGT,
  truncate,
} from '../utils/formatters';
import EmptyState from './EmptyState';
import ErrorState from './ErrorState';
import LoadingSkeleton from './LoadingSkeleton';
import PriorityBadge from './PriorityBadge';
import ServiceTypePill from './ServiceTypePill';
import StatusBadge from './StatusBadge';

/**
 * The ticket list.
 *
 * Presentational: the list state (filters, page, sort) lives in the module
 * root, because the detail drawer has to be able to refetch it after a save
 * and the dashboard reads from the same connection.
 *
 * Customer and Assigned Agent are not sortable. Both come from a batched
 * lookup rather than an embedded join, so Postgres cannot order by them —
 * offering a header that silently sorted by something else would be worse
 * than not offering one.
 */

const SORTABLE = new Set(['ticket_number', 'status', 'priority', 'created_at', 'updated_at']);

const COLUMNS = [
  { key: 'ticket_number', label: 'Reference' },
  { key: 'service_type', label: 'Service Type' },
  { key: 'status', label: 'Status' },
  { key: 'priority', label: 'Priority' },
  { key: 'assigned_agent', label: 'Assigned Agent' },
  { key: 'customer', label: 'Customer' },
  { key: 'created_lead_id', label: 'Linked Case' },
  { key: 'created_at', label: 'Created' },
  { key: 'updated_at', label: 'Updated' },
];

function SortIcon({ active, direction }) {
  if (!active) return <span className="ml-1 text-gray-300">↕</span>;
  return <span className="ml-1 text-[#0D7AD2]">{direction === 'asc' ? '↑' : '↓'}</span>;
}

export default function TicketTable({
  tickets = [],
  loading,
  error,
  count = 0,
  page = 1,
  pageSize = 20,
  sortColumn,
  sortDirection,
  hasUpdatedAt = false,
  filtersActive = false,
  onSort,
  onPageChange,
  onRefresh,
  onSelectTicket,
}) {
  const columns = COLUMNS.filter((column) => column.key !== 'updated_at' || hasUpdatedAt);

  const from = count === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = Math.min(page * pageSize, count);
  const lastPage = Math.max(1, Math.ceil(count / pageSize));

  const handleSort = (key) => {
    if (!SORTABLE.has(key) || !onSort) return;
    const nextDirection = sortColumn === key && sortDirection === 'desc' ? 'asc' : 'desc';
    onSort(key, nextDirection);
  };

  /**
   * Anywhere in the row opens the ticket.
   *
   * Skipped while the user has text selected: the row carries a phone number
   * and a case id, and dragging across either to copy it ends in a click. The
   * drawer opening on top of that reads as the table swallowing the gesture.
   */
  const handleRowClick = (ticket) => {
    if (!onSelectTicket) return;
    const selection = typeof window !== 'undefined' ? window.getSelection() : null;
    if (selection && String(selection).length > 0) return;
    onSelectTicket(ticket);
  };

  return (
    <section className="rounded-lg border border-gray-200 bg-white">
      <header className="flex items-center justify-between border-b border-gray-200 px-4 py-3">
        <h2 className="text-sm font-bold text-[#003E60]">
          Tickets{' '}
          {!loading && !error ? (
            <span className="font-normal text-gray-500">({count})</span>
          ) : null}
        </h2>
        <button
          type="button"
          onClick={onRefresh}
          disabled={loading}
          title="Refresh"
          aria-label="Refresh tickets"
          className="rounded-md border border-gray-300 px-2 py-1 text-sm text-gray-600 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
        >
          <span className={loading ? 'inline-block animate-spin' : 'inline-block'}>↻</span>
        </button>
      </header>

      {error ? (
        <div className="p-4">
          <ErrorState message={error} onRetry={onRefresh} />
        </div>
      ) : loading ? (
        <LoadingSkeleton rows={5} columns={columns.length} />
      ) : tickets.length === 0 ? (
        <EmptyState
          message={
            filtersActive
              ? 'No tickets match these filters. Try clearing them.'
              : 'Tickets appear here as the chatbot raises them.'
          }
        />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[1000px] border-collapse text-left">
            <thead>
              <tr className="border-b border-gray-200 bg-gray-50">
                {columns.map((column) => {
                  const sortable = SORTABLE.has(column.key);
                  const active = sortColumn === column.key;
                  return (
                    <th
                      key={column.key}
                      scope="col"
                      className="px-4 py-2 text-xs font-bold uppercase tracking-wide text-gray-500"
                    >
                      {sortable ? (
                        <button
                          type="button"
                          onClick={() => handleSort(column.key)}
                          className="flex items-center rounded focus:outline-none focus:ring-2 focus:ring-blue-500"
                        >
                          {column.label}
                          <SortIcon active={active} direction={sortDirection} />
                        </button>
                      ) : (
                        column.label
                      )}
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {tickets.map((ticket) => {
                const services = asServiceArray(ticket.service_type);
                const shown = services.slice(0, 2);
                const extra = services.length - shown.length;
                const conversation = ticket.conversation;
                const isLive = OPEN_STATUSES.includes(ticket.status);

                return (
                  <tr
                    key={ticket.id}
                    onClick={() => handleRowClick(ticket)}
                    className="align-top hover:bg-gray-50 cursor-pointer"
                  >
                    <td className="px-4 py-3">
                      {/* Kept a real button, and it is the row's only tab stop:
                          the row itself is a mouse affordance, so the keyboard
                          path stays here rather than on a focusable <tr>.
                          stopPropagation so a click on it does not also reach
                          the row and open the drawer twice. */}
                      <button
                        type="button"
                        onClick={(event) => {
                          event.stopPropagation();
                          if (onSelectTicket) onSelectTicket(ticket);
                        }}
                        className="rounded text-left text-sm font-bold text-[#0D7AD2] hover:underline focus:outline-none focus:ring-2 focus:ring-blue-500"
                      >
                        {ticket.ticket_number || DASH}
                      </button>
                      {ticket.description ? (
                        <p className="mt-0.5 max-w-[220px] text-xs text-gray-500">
                          {truncate(firstLine(ticket.description), 60)}
                        </p>
                      ) : null}
                    </td>

                    <td className="px-4 py-3">
                      <div className="flex flex-wrap gap-1">
                        {shown.length ? (
                          shown.map((service) => <ServiceTypePill key={service} value={service} />)
                        ) : (
                          <span className="text-sm text-gray-400">{DASH}</span>
                        )}
                        {extra > 0 ? (
                          <span
                            title={services.join(', ')}
                            className="inline-block rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-500"
                          >
                            +{extra} more
                          </span>
                        ) : null}
                      </div>
                    </td>

                    <td className="px-4 py-3">
                      <StatusBadge value={ticket.status} />
                      {isLive ? (
                        <p className="mt-1 whitespace-nowrap text-xs text-amber-700">Bot paused</p>
                      ) : null}
                    </td>

                    <td className="px-4 py-3">
                      <PriorityBadge value={ticket.priority} />
                    </td>

                    <td className="px-4 py-3 text-sm text-gray-700">
                      {agentName(ticket.agent) || (
                        <span className="text-gray-400">Unassigned</span>
                      )}
                    </td>

                    <td className="px-4 py-3 text-sm text-gray-700">
                      {conversation ? (
                        <>
                          <div className="whitespace-nowrap">
                            {formatPhone(conversation.customer_number)}
                          </div>
                          {conversation.customer_name ? (
                            <div className="text-xs text-gray-500">
                              {truncate(conversation.customer_name, 24)}
                            </div>
                          ) : null}
                        </>
                      ) : (
                        <span className="text-gray-400">{DASH}</span>
                      )}
                    </td>

                    <td className="px-4 py-3 text-sm text-gray-700">
                      {ticket.created_lead_id ? (
                        <span title={ticket.created_lead_id} className="font-mono text-xs">
                          {String(ticket.created_lead_id).slice(0, 8)}…
                        </span>
                      ) : (
                        <span className="text-gray-400">{DASH}</span>
                      )}
                    </td>

                    <td className="whitespace-nowrap px-4 py-3 text-sm text-gray-600">
                      {formatSGT(ticket.created_at)}
                    </td>

                    {hasUpdatedAt ? (
                      <td className="whitespace-nowrap px-4 py-3 text-sm text-gray-600">
                        {formatSGT(ticket.updated_at)}
                      </td>
                    ) : null}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {!error && !loading && count > 0 ? (
        <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-gray-200 px-4 py-3">
          <p className="text-sm text-gray-500">
            Showing {from}–{to} of {count} tickets
          </p>
          <div className="ml-auto flex items-center gap-2">
            <button
              type="button"
              onClick={() => onPageChange && onPageChange(page - 1)}
              disabled={page <= 1}
              className="rounded-md border border-gray-300 px-3 py-1 text-sm text-gray-700 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Previous
            </button>
            <button
              type="button"
              onClick={() => onPageChange && onPageChange(page + 1)}
              disabled={page >= lastPage}
              className="rounded-md border border-gray-300 px-3 py-1 text-sm text-gray-700 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Next
            </button>
          </div>
        </footer>
      ) : null}
    </section>
  );
}
