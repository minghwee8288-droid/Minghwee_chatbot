/**
 * The ticket list: filters, sorting, pagination.
 *
 * Conversation and agent details are fetched as separate batched reads rather
 * than as PostgREST embeds. An embed depends on the foreign key being declared
 * and unambiguous, and cb_tickets carries four id columns pointing at three
 * different tables — a resolution failure there takes out the whole list. Two
 * extra reads keyed by id cannot fail that way, and they are what makes a
 * search across the joined customer_number work with a correct total.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { DEFAULT_PAGE_SIZE, EMPTY_FILTERS, priorityValues } from '../utils/constants';
import { looksLikePhone } from '../utils/formatters';
import { fetchProfilesByIds } from '../utils/profiles';

const TICKETS = 'cb_tickets';
const CONVERSATIONS = 'wp_chat_conversations';

/** Ceiling on ids folded into a search, to keep the request URL sane. */
const MAX_SEARCH_CONVERSATIONS = 200;

/** PostgREST reads `,` and `.` inside an or() filter as syntax. */
function escapeForOr(term) {
  return String(term).replace(/[,.()]/g, ' ');
}

function escapeLike(term) {
  return String(term).replace(/[%_]/g, (c) => `\\${c}`);
}

/**
 * Conversation ids whose customer number or name matches the search term.
 * Returns null when the term is not worth a lookup, which the caller reads as
 * "do not add a conversation clause" rather than "matched nothing".
 */
async function conversationIdsMatching(supabaseClient, term) {
  const trimmed = String(term || '').trim();
  if (trimmed.length < 2) return null;

  const like = `%${escapeLike(trimmed)}%`;
  const digits = trimmed.replace(/\D/g, '');
  // A number typed as +65 9123 4567 has to be matched against bare digits.
  const numberLike = digits.length >= 3 ? `%${digits}%` : like;

  const { data, error } = await supabaseClient
    .from(CONVERSATIONS)
    .select('id')
    .or(`customer_number.ilike.${numberLike},customer_name.ilike.${like}`)
    .limit(MAX_SEARCH_CONVERSATIONS);

  if (error) throw error;
  return (data || []).map((row) => row.id);
}

export default function useTickets({
  supabaseClient,
  filters = EMPTY_FILTERS,
  page = 1,
  pageSize = DEFAULT_PAGE_SIZE,
  sortColumn = 'created_at',
  sortDirection = 'desc',
}) {
  const [data, setData] = useState([]);
  const [count, setCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Cheap stable key: the filter object is rebuilt on every keystroke, so
  // depending on the object identity would refetch even when nothing changed.
  const filterKey = JSON.stringify(filters);

  // Guards against a slow early request overwriting a newer one's results.
  const requestRef = useRef(0);

  const fetchTickets = useCallback(async () => {
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;

    setLoading(true);
    setError(null);

    try {
      const active = JSON.parse(filterKey);
      let query = supabaseClient.from(TICKETS).select('*', { count: 'exact' });

      const search = String(active.search || '').trim();
      if (search) {
        const clauses = [
          `ticket_number.ilike.%${escapeForOr(escapeLike(search))}%`,
          `description.ilike.%${escapeForOr(escapeLike(search))}%`,
        ];
        // Customer number and name live on the conversation, so they are
        // resolved to ids first and folded into the same OR — filtering the
        // page in memory afterwards would report a wrong total and drop rows.
        const conversationIds = await conversationIdsMatching(supabaseClient, search);
        if (conversationIds && conversationIds.length) {
          clauses.push(`conversation_id.in.(${conversationIds.join(',')})`);
        } else if (conversationIds && looksLikePhone(search)) {
          // A phone-shaped search that matched no conversation cannot match a
          // ticket_number either; say so rather than showing description hits.
          clauses.length = 0;
          clauses.push('conversation_id.is.null');
        }
        query = query.or(clauses.join(','));
      }

      if (active.status) query = query.eq('status', active.status);
      // .in() rather than .eq() so a level can match more than one column
      // value — see priorityValues(), which the migration once needed.
      if (active.priority) query = query.in('priority', priorityValues(active.priority));
      if (active.assigned_agent_id) {
        query = query.eq('assigned_agent_id', active.assigned_agent_id);
      }
      // service_type is text[]; membership is a containment test.
      if (active.service_type) query = query.contains('service_type', [active.service_type]);
      if (active.date_from) query = query.gte('created_at', active.date_from);
      if (active.date_to) {
        // A date input gives midnight; the intent is "up to the end of that day".
        const end = active.date_to.length === 10 ? `${active.date_to}T23:59:59.999Z` : active.date_to;
        query = query.lte('created_at', end);
      }

      query = query.order(sortColumn || 'created_at', {
        ascending: sortDirection === 'asc',
        nullsFirst: false,
      });

      const from = Math.max(0, (page - 1) * pageSize);
      query = query.range(from, from + pageSize - 1);

      const { data: rows, count: total, error: queryError } = await query;
      if (queryError) throw queryError;

      const tickets = rows || [];

      // Batched lookups for the two joined labels the table shows.
      const conversationIds = [
        ...new Set(tickets.map((t) => t.conversation_id).filter((id) => id !== null && id !== undefined)),
      ];
      const agentIds = [...new Set(tickets.map((t) => t.assigned_agent_id).filter(Boolean))];

      const [conversations, agentById] = await Promise.all([
        conversationIds.length
          ? supabaseClient
              .from(CONVERSATIONS)
              .select('id, customer_number, customer_name, contact_type, bot_status, status')
              .in('id', conversationIds)
          : Promise.resolve({ data: [], error: null }),
        fetchProfilesByIds(supabaseClient, agentIds),
      ]);

      // A failed lookup costs the labels, not the list: the tickets themselves
      // already loaded, and showing them without a customer name beats an
      // error page that hides every one of them.
      if (conversations.error) {
        console.warn('Could not load conversations for the ticket list', conversations.error);
      }

      const conversationById = new Map((conversations.data || []).map((c) => [String(c.id), c]));

      const merged = tickets.map((ticket) => ({
        ...ticket,
        conversation: conversationById.get(String(ticket.conversation_id)) || null,
        agent: ticket.assigned_agent_id
          ? agentById.get(String(ticket.assigned_agent_id)) || null
          : null,
      }));

      if (requestRef.current !== requestId) return;
      setData(merged);
      setCount(total || 0);
    } catch (err) {
      if (requestRef.current !== requestId) return;
      console.error('Ticket list query failed', err);
      setError(err);
      setData([]);
      setCount(0);
    } finally {
      if (requestRef.current === requestId) setLoading(false);
    }
  }, [supabaseClient, filterKey, page, pageSize, sortColumn, sortDirection]);

  useEffect(() => {
    fetchTickets();
  }, [fetchTickets]);

  /** True once a row has been seen carrying the column — see the note in TicketTable. */
  const hasUpdatedAt = useMemo(
    () => data.some((row) => Object.prototype.hasOwnProperty.call(row, 'updated_at')),
    [data],
  );

  return { data, count, loading, error, refetch: fetchTickets, hasUpdatedAt };
}
