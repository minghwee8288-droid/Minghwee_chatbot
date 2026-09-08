/**
 * Everything the confirmation screen needs before anything is deleted.
 *
 * The point of this file is that the client can SEE they have the right
 * person: the conversation, who we think they are, the last few messages,
 * and every lead on that number -- before a single row is touched.
 */

import { employerLeadBlockers } from './blockers';
import { supabase, countForConversation } from './db';
import { env, mayReset, gateEnabled } from './env';
import { digitsOnly, normalizePhone, phoneVariants, storageKey } from './phone';
import type {
  ContactIdentity,
  ConversationSummary,
  LeadRow,
  LookupResult,
  MessagePreview,
} from './types';

/**
 * app/services/conversation.py :: get_by_phone
 *
 * Finds the conversation whatever format the number is stored in, and
 * prefers the portal's own bare-digits row over any duplicate the bot may
 * have created (see scripts/fix_split_conversations.py -- that bug has
 * happened, and a tool that picked the wrong row would clear the wrong half
 * of a split thread).
 */
export async function findConversation(phone: string) {
  const variants = phoneVariants(phone);
  if (!variants.length) return null;

  const { data, error } = await supabase()
    .from('wp_chat_conversations')
    .select('*')
    .in('customer_number', variants)
    .limit(5);
  if (error) throw new Error(`Conversation lookup failed: ${error.message}`);

  const rows = data ?? [];
  if (!rows.length) return null;

  const key = storageKey(phone);
  return rows.find((r) => r.customer_number === key) ?? rows[0];
}

/**
 * app/services/contact.py :: _first_match
 *
 * Exact variants first, then a narrow last-four-digits search compared on
 * digits alone -- employers.phone is stored spaced ('+65 9188 4442'), so an
 * exact match alone misses most employers.
 */
async function firstMatch(
  table: string,
  columns: string,
  phoneColumns: string[],
  phone: string,
): Promise<Record<string, any> | null> {
  const variants = phoneVariants(phone);
  const digits = digitsOnly(phone);
  if (!variants.length || digits.length < 8) return null;
  const tail = digits.slice(-8);
  const last4 = digits.slice(-4);

  for (const column of phoneColumns) {
    const { data, error } = await supabase()
      .from(table)
      .select(columns)
      .in(column, variants)
      .limit(1);
    // A column the platform schema does not have is not worth failing the
    // whole lookup over -- contact.py tolerates exactly the same thing.
    if (error) continue;
    if (data && data.length) return data[0] as Record<string, any>;
  }

  for (const column of phoneColumns) {
    const { data, error } = await supabase()
      .from(table)
      .select(columns)
      .ilike(column, `%${last4}%`)
      .limit(25);
    if (error) continue;
    for (const row of (data ?? []) as Record<string, any>[]) {
      if (digitsOnly(row[column]).endsWith(tail)) return row;
    }
  }
  return null;
}

/**
 * Who is this number? Employer first, then candidate -- the order
 * contact.py checks in.
 */
export async function findContact(phone: string): Promise<ContactIdentity> {
  const employer = await firstMatch(
    'employers',
    'id, display_name, phone, archived_at',
    ['phone'],
    phone,
  );
  if (employer && !employer.archived_at) {
    // `placements` is the only record of "they hired through us". An
    // `employers` row only means the portal holds their details.
    const { count } = await supabase()
      .from('placements')
      .select('*', { count: 'exact', head: true })
      .eq('employer_id', employer.id)
      .is('archived_at', null);
    return {
      type: 'employer',
      id: employer.id,
      name: employer.display_name ?? null,
      phone: employer.phone ?? null,
      priorHires: count ?? 0,
    };
  }

  const candidate = await firstMatch(
    'candidates',
    'id, full_name, phone, phone_e164, nationality, archived_at',
    ['phone', 'phone_e164'],
    phone,
  );
  if (candidate && !candidate.archived_at) {
    return {
      type: 'candidate',
      id: candidate.id,
      name: candidate.full_name ?? null,
      phone: candidate.phone ?? candidate.phone_e164 ?? null,
      nationality: candidate.nationality ?? null,
    };
  }

  return { type: 'unknown', id: null, name: null, phone: null };
}

/**
 * app/services/lead.py :: find_by_phone, widened to return EVERY match.
 *
 * The Python returns the first lead it finds and stops, because section 1B
 * says one phone has one lead, ever. This returns all of them from both
 * tables: section 9.3 records that the rule is enforced per-table today, so
 * one number really can hold both an employer and a candidate lead, and a
 * tool for cleaning up has to show what is actually there rather than what
 * the rule says should be.
 */
export async function findLeads(
  phone: string,
  conversationId: number | null,
): Promise<LeadRow[]> {
  const variants = phoneVariants(phone);
  const digits = digitsOnly(phone);
  if (!variants.length || digits.length < 8) return [];
  const tail = digits.slice(-8);

  const found: LeadRow[] = [];

  for (const table of ['leads', 'leads_candidate'] as const) {
    const rows: Record<string, any>[] = [];

    const exact = await supabase()
      .from(table)
      .select('*')
      .eq('tenant_id', env.tenantId)
      .in('phone', variants)
      .limit(25);
    if (!exact.error) rows.push(...((exact.data ?? []) as Record<string, any>[]));

    // The same loose fallback the Python uses: stored numbers carry spaces,
    // dashes and brackets, and an exact-match-only check reports "no lead"
    // for a lead that is plainly there.
    const loose = await supabase()
      .from(table)
      .select('*')
      .eq('tenant_id', env.tenantId)
      .ilike('phone', `%${digits.slice(-4)}%`)
      .limit(25);
    if (!loose.error) {
      for (const row of (loose.data ?? []) as Record<string, any>[]) {
        if (!digitsOnly(row.phone).endsWith(tail)) continue;
        if (rows.some((r) => r.id === row.id)) continue;
        rows.push(row);
      }
    }

    for (const row of rows) {
      const isEmployer = table === 'leads';
      found.push({
        id: row.id,
        table,
        kind: isEmployer ? 'employer' : 'candidate',
        leadNumber: row.lead_number ?? null,
        fullName: row.full_name ?? null,
        phone: row.phone ?? null,
        email: row.email ?? null,
        status: row.status ?? null,
        source: row.source ?? null,
        createdAt: row.created_at ?? null,
        interestType: isEmployer ? row.interest_type ?? null : undefined,
        requirement: isEmployer ? row.requirement ?? null : undefined,
        nationality: isEmployer ? undefined : row.nationality ?? null,
        blockers: isEmployer ? await employerLeadBlockers(row.id, conversationId) : [],
      });
    }
  }

  return found;
}

/** The last few messages, so the client can confirm this is the right thread. */
export async function recentMessages(
  conversationId: number,
  limit = 6,
): Promise<MessagePreview[]> {
  const { data, error } = await supabase()
    .from('wp_chat_messages')
    .select('id, direction, is_bot, body, created_at')
    .eq('conversation_id', conversationId)
    .order('created_at', { ascending: false })
    .limit(limit);
  if (error) return [];
  return ((data ?? []) as Record<string, any>[])
    .map((row) => ({
      id: row.id as number,
      direction: (row.direction as string) ?? null,
      isBot: (row.is_bot as boolean) ?? null,
      body: (row.body as string) ?? null,
      createdAt: (row.created_at as string) ?? null,
    }))
    .reverse();
}

/** The whole confirmation payload for one phone number. */
export async function lookup(rawPhone: string): Promise<LookupResult> {
  const phone = normalizePhone(rawPhone);

  if (!mayReset(phone)) {
    return {
      phone,
      allowed: false,
      blockedReason:
        'This number is not on RESET_ALLOWED_NUMBERS, the safety list configured on the server. ' +
        'Nothing can be cleared for it.',
      conversation: null,
      contact: { type: 'unknown', id: null, name: null, phone: null },
      leads: [],
      recentMessages: [],
      checkpointsReachable: Boolean(env.dbUrl),
    };
  }

  const row = await findConversation(phone);

  let conversation: ConversationSummary | null = null;
  let messages: MessagePreview[] = [];

  if (row) {
    const [messageCount, ticketCount, handoverCount] = await Promise.all([
      countForConversation('wp_chat_messages', row.id),
      countForConversation('cb_tickets', row.id),
      countForConversation('cb_handovers', row.id),
    ]);
    conversation = {
      id: row.id,
      customerNumber: row.customer_number ?? null,
      customerName: row.customer_name ?? null,
      botStatus: row.bot_status ?? null,
      status: row.status ?? null,
      threadId: row.langgraph_thread_id ?? null,
      lastMessageAt: row.last_message_at ?? null,
      serviceType: row.service_type ?? null,
      intent: row.intent ?? null,
      contactType: row.contact_type ?? null,
      counts: { messages: messageCount, tickets: ticketCount, handovers: handoverCount },
    };
    messages = await recentMessages(row.id);
  }

  const [contact, leads] = await Promise.all([
    findContact(phone),
    findLeads(phone, row ? row.id : null),
  ]);

  return {
    phone,
    allowed: true,
    conversation,
    contact,
    leads,
    recentMessages: messages,
    checkpointsReachable: Boolean(env.dbUrl),
  };
}

export { gateEnabled };
