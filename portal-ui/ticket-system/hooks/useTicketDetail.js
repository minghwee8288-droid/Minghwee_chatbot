/**
 * Everything the drawer shows for one ticket.
 *
 * The ticket has to land before the messages can be read — they are keyed by
 * its conversation_id, not by the ticket — so the fetch is two waves rather
 * than one Promise.all: the ticket first, then the conversation, transcript,
 * handovers and handover agents together.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchProfilesByIds } from '../utils/profiles';

const TICKETS = 'cb_tickets';
const CONVERSATIONS = 'wp_chat_conversations';
const MESSAGES = 'wp_chat_messages';
const HANDOVERS = 'cb_handovers';

/** A thread longer than this is trimmed to its most recent messages. */
const MAX_MESSAGES = 400;

export default function useTicketDetail({ supabaseClient, ticketId }) {
  const [ticket, setTicket] = useState(null);
  const [conversation, setConversation] = useState(null);
  const [messages, setMessages] = useState([]);
  const [handovers, setHandovers] = useState([]);
  const [loading, setLoading] = useState(Boolean(ticketId));
  const [error, setError] = useState(null);

  const requestRef = useRef(0);

  const fetchDetail = useCallback(async () => {
    if (!ticketId) {
      setTicket(null);
      setConversation(null);
      setMessages([]);
      setHandovers([]);
      setLoading(false);
      setError(null);
      return;
    }

    const requestId = requestRef.current + 1;
    requestRef.current = requestId;

    setLoading(true);
    setError(null);

    try {
      const { data: ticketRow, error: ticketError } = await supabaseClient
        .from(TICKETS)
        .select('*')
        .eq('id', ticketId)
        .maybeSingle();

      if (ticketError) throw ticketError;
      if (!ticketRow) throw new Error('This ticket no longer exists.');

      const conversationId = ticketRow.conversation_id;

      const [conversationResult, messageResult, handoverResult, assignedAgentById] = await Promise.all([
        conversationId !== null && conversationId !== undefined
          ? supabaseClient.from(CONVERSATIONS).select('*').eq('id', conversationId).maybeSingle()
          : Promise.resolve({ data: null, error: null }),

        conversationId !== null && conversationId !== undefined
          ? supabaseClient
              .from(MESSAGES)
              .select('*')
              .eq('conversation_id', conversationId)
              .order('created_at', { ascending: false })
              .limit(MAX_MESSAGES)
          : Promise.resolve({ data: [], error: null }),

        // Handovers logged against this ticket, plus everything else on the
        // same conversation: the ones that silenced the bot before the ticket
        // existed are exactly the history an agent needs.
        conversationId !== null && conversationId !== undefined
          ? supabaseClient
              .from(HANDOVERS)
              .select('*')
              .or(`ticket_id.eq.${ticketId},conversation_id.eq.${conversationId}`)
              .order('created_at', { ascending: true })
          : supabaseClient
              .from(HANDOVERS)
              .select('*')
              .eq('ticket_id', ticketId)
              .order('created_at', { ascending: true }),

        fetchProfilesByIds(supabaseClient, [ticketRow.assigned_agent_id]),
      ]);

      if (messageResult.error) throw messageResult.error;
      if (handoverResult.error) throw handoverResult.error;
      // The conversation is a label. Losing it must not hide the ticket
      // itself, which is the thing the agent opened.
      if (conversationResult.error) {
        console.warn('Could not load the conversation for this ticket', conversationResult.error);
      }

      const handoverRows = handoverResult.data || [];
      const agentById = await fetchProfilesByIds(
        supabaseClient,
        handoverRows.map((row) => row.agent_profile_id),
      );

      if (requestRef.current !== requestId) return;

      const assignedAgent = ticketRow.assigned_agent_id
        ? assignedAgentById.get(String(ticketRow.assigned_agent_id)) || null
        : null;

      setTicket({ ...ticketRow, agent: assignedAgent });
      setConversation(conversationResult.data || null);
      // Read newest-first so the limit keeps the RECENT end of a long thread,
      // then reversed for display: a transcript reads oldest to newest.
      setMessages([...(messageResult.data || [])].reverse());
      setHandovers(
        handoverRows.map((row) => ({
          ...row,
          agent: row.agent_profile_id ? agentById.get(String(row.agent_profile_id)) || null : null,
        })),
      );
    } catch (err) {
      if (requestRef.current !== requestId) return;
      console.error('Ticket detail query failed', err);
      setError(err);
      setTicket(null);
      setConversation(null);
      setMessages([]);
      setHandovers([]);
    } finally {
      if (requestRef.current === requestId) setLoading(false);
    }
  }, [supabaseClient, ticketId]);

  useEffect(() => {
    fetchDetail();
  }, [fetchDetail]);

  return { ticket, conversation, messages, handovers, loading, error, refetch: fetchDetail };
}
