/**
 * What stops an employer lead being deleted.
 *
 * `leads` is referenced by three tables and every one of those foreign keys
 * is ON DELETE NO ACTION -- confirmed against the live database, not assumed:
 *
 *   cb_tickets.created_lead_id                  (CLAUDE.md section 9.5)
 *   lead_activities.lead_id
 *   employer_service_requests.converted_lead_id
 *
 * So a DELETE colliding with any of them fails outright. Finding that out at
 * the moment of deletion means a raw Postgres error in front of a
 * non-technical user; finding it out here means a sentence they can act on.
 *
 * `leads_candidate` has no inbound foreign keys at all, so it is never
 * blocked and this is never called for it.
 *
 * ONE COPY, TWO CALLERS, deliberately. The lookup calls it to decide whether
 * to offer a lead as deletable, and the reset calls it again at the moment of
 * deletion because the page may have been open for a while and a ticket
 * raised in between would turn a permitted delete into a foreign-key error.
 * Two copies of a rule like this are how they drift apart -- which is a live
 * problem in this codebase already (CLAUDE.md section 9.8).
 */

import { supabase } from './db';
import type { LeadBlocker } from './types';

/**
 * @param conversationId Tickets on THIS conversation are not blockers -- they
 *   are deleted first, in the same operation. Pass null to count every ticket.
 */
export async function employerLeadBlockers(
  leadId: string,
  conversationId: number | null,
): Promise<LeadBlocker[]> {
  const blockers: LeadBlocker[] = [];

  let ticketQuery = supabase()
    .from('cb_tickets')
    .select('*', { count: 'exact', head: true })
    .eq('created_lead_id', leadId);
  if (conversationId !== null) {
    ticketQuery = ticketQuery.neq('conversation_id', conversationId);
  }
  const tickets = await ticketQuery;
  if ((tickets.count ?? 0) > 0) {
    blockers.push({
      table: 'cb_tickets',
      column: 'created_lead_id',
      count: tickets.count ?? 0,
      reason:
        `${tickets.count} ticket(s) on OTHER conversations were raised from this lead. ` +
        'Deleting it would leave those tickets pointing at nothing, so the database refuses.',
    });
  }

  // A table the platform may not have in every environment: an error here is
  // treated as "nothing to report" rather than failing the whole lookup.
  const activities = await supabase()
    .from('lead_activities')
    .select('*', { count: 'exact', head: true })
    .eq('lead_id', leadId);
  if (!activities.error && (activities.count ?? 0) > 0) {
    blockers.push({
      table: 'lead_activities',
      column: 'lead_id',
      count: activities.count ?? 0,
      reason:
        `${activities.count} logged sales activity/activities belong to this lead. ` +
        'That is history the team worked from -- clear it in the portal first if the lead really must go.',
    });
  }

  const requests = await supabase()
    .from('employer_service_requests')
    .select('*', { count: 'exact', head: true })
    .eq('converted_lead_id', leadId);
  if (!requests.error && (requests.count ?? 0) > 0) {
    blockers.push({
      table: 'employer_service_requests',
      column: 'converted_lead_id',
      count: requests.count ?? 0,
      reason: `${requests.count} service request(s) were converted from this lead.`,
    });
  }

  return blockers;
}
