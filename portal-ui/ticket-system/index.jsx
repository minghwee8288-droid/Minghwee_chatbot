import React, { useCallback, useEffect, useMemo, useState } from 'react';
import useAgents from './hooks/useAgents';
import useDashboardStats from './hooks/useDashboardStats';
import useTickets from './hooks/useTickets';
import ErrorState from './components/ErrorState';
import TicketDashboard from './components/TicketDashboard';
import TicketDetailDrawer from './components/TicketDetailDrawer';
import TicketFilters from './components/TicketFilters';
import TicketTable from './components/TicketTable';
import Toast from './components/Toast';
import { DEFAULT_PAGE_SIZE, EMPTY_FILTERS } from './utils/constants';

/**
 * Ticket Management for the Ming Hwee OS portal.
 *
 *     import TicketSystemModule from './ticket-system';
 *     <TicketSystemModule supabaseClient={supabase} />
 *
 * The module creates no Supabase client of its own and holds no global state:
 * every hook takes the client it is given. It reads cb_tickets, cb_handovers,
 * wp_chat_conversations, wp_chat_messages and profiles, and writes only
 * cb_tickets.status, cb_tickets.priority and cb_tickets.assigned_agent_id.
 */

const SEARCH_DEBOUNCE_MS = 300;

export default function TicketSystemModule({ supabaseClient, pageSize = DEFAULT_PAGE_SIZE }) {
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  // Typing is applied to the query on a delay; every other filter applies at
  // once. Debouncing the whole object would make the selects feel broken.
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [page, setPage] = useState(1);
  const [sortColumn, setSortColumn] = useState('created_at');
  const [sortDirection, setSortDirection] = useState('desc');
  const [selectedTicketId, setSelectedTicketId] = useState(null);
  const [toast, setToast] = useState(null);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(filters.search), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [filters.search]);

  const appliedFilters = useMemo(
    () => ({ ...filters, search: debouncedSearch }),
    [filters, debouncedSearch],
  );

  const { agents, loading: agentsLoading, error: agentsError } = useAgents({ supabaseClient });

  const stats = useDashboardStats({ supabaseClient });

  const {
    data: tickets,
    count,
    loading,
    error,
    refetch,
    hasUpdatedAt,
  } = useTickets({
    supabaseClient,
    filters: appliedFilters,
    page,
    pageSize,
    sortColumn,
    sortDirection,
  });

  const handleFilterChange = useCallback((next) => {
    setFilters(next);
    // A filter change with the old page number still set can land on a page
    // that no longer exists, which reads as an empty result.
    setPage(1);
  }, []);

  const handleSort = useCallback((column, direction) => {
    setSortColumn(column);
    setSortDirection(direction);
    setPage(1);
  }, []);

  const handleRefresh = useCallback(() => {
    refetch();
    stats.refetch();
  }, [refetch, stats]);

  const handleSaveSuccess = useCallback(() => {
    // A status change moves the ticket between buckets, so the cards and the
    // charts are as stale as the list is.
    refetch();
    stats.refetch();
  }, [refetch, stats]);

  const showToast = useCallback((message, type) => setToast({ message, type }), []);
  const dismissToast = useCallback(() => setToast(null), []);

  const filtersActive = Object.keys(EMPTY_FILTERS).some((key) => appliedFilters[key]);

  return (
    <div className="space-y-6">
      <div className="border-b border-gray-200 pb-4">
        <h1 className="text-xl font-black text-[#003E60]">Ticket Management</h1>
        <p className="mt-1 max-w-3xl text-sm leading-6 text-gray-500">
          Enquiries the WhatsApp chatbot handed over. A ticket left Open or In Progress keeps the
          bot from answering that topic.
        </p>
      </div>

      <TicketDashboard
        total={stats.total}
        byStatus={stats.byStatus}
        byServiceType={stats.byServiceType}
        byPriority={stats.byPriority}
        truncated={stats.truncated}
        loading={stats.loading}
        error={stats.error}
        onRetry={stats.refetch}
      />

      {agentsError ? (
        <ErrorState message="Agent names and the assignment dropdown are unavailable. Tickets still load." />
      ) : null}

      <TicketFilters
        filters={filters}
        onFilterChange={handleFilterChange}
        agents={agents}
        agentsLoading={agentsLoading}
      />

      <TicketTable
        tickets={tickets}
        loading={loading}
        error={error}
        count={count}
        page={page}
        pageSize={pageSize}
        sortColumn={sortColumn}
        sortDirection={sortDirection}
        hasUpdatedAt={hasUpdatedAt}
        filtersActive={filtersActive}
        onSort={handleSort}
        onPageChange={setPage}
        onRefresh={handleRefresh}
        onSelectTicket={(ticket) => setSelectedTicketId(ticket.id)}
      />

      {selectedTicketId ? (
        <TicketDetailDrawer
          supabaseClient={supabaseClient}
          ticketId={selectedTicketId}
          agents={agents}
          onClose={() => setSelectedTicketId(null)}
          onSaveSuccess={handleSaveSuccess}
          onToast={showToast}
        />
      ) : null}

      <Toast message={toast?.message} type={toast?.type} onDismiss={dismissToast} />
    </div>
  );
}
