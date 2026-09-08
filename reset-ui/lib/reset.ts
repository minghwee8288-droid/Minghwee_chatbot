/**
 * The reset itself: the same work `scripts/reset_conversation.py` does,
 * driven from a button instead of a terminal.
 *
 * ORDER MATTERS, and it is the script's order:
 *
 *   1. cb_handovers   -- rows point at a ticket, so they go before tickets
 *   2. cb_tickets     -- rows reference the conversation
 *   3. wp_chat_messages -- last; nothing references it, and it is the one the
 *                        bot actually reads back
 *   4. the LangGraph checkpoint, by thread_id
 *   5. the selected leads (this tool's addition -- see below)
 *   6. the conversation row itself: bot_active, a fresh thread, identity cleared
 *
 * Deleting the messages is the part that actually matters. The bot does not
 * read its memory out of the checkpoint alone -- every turn rebuilds
 * history_text from the last `history_limit` messages of wp_chat_messages
 * (40 by default) and does not care which thread they belong to. A new thread
 * id on its own leaves the whole previous test sitting there and the bot
 * picks the old enquiry up as if nothing had happened.
 *
 * WHAT THIS DOES THAT THE SCRIPT DOES NOT: it can delete a row from `leads`,
 * the employer lead table. The script refuses to, deliberately -- that table
 * holds real sales pipeline. This tool clears EVERY lead on the number being
 * cleared, employer and candidate alike, at the client's explicit request:
 * clearing a number is meant to leave nothing behind, and asking them to tick
 * a box per lead was work they did not want. The protection that remains is
 * that a lead something else still references is refused with a reason rather
 * than attempted -- the foreign keys would reject it anyway. Deleting
 * a lead is also the documented way to test the first-contact flow twice:
 * section 1B makes a lead permanent, so a reset number is otherwise always a
 * returning client. The script's own note says to delete the lead row and the
 * thread's checkpoint TOGETHER -- deleting the lead alone is what broke
 * conversation 36, where the checkpoint kept pointing at the dead row and
 * every ticket insert failed the foreign key, silently, for twenty minutes.
 * Doing both in one operation is exactly what this does.
 *
 * Every delete below is filtered by a conversation id or a lead id, and BOTH
 * are resolved here from the phone number -- the browser sends a number and
 * nothing else, so no request can name a row belonging to somebody else.
 * There is no unscoped delete in this file and there must never be one:
 * wp_chat_messages is the portal's transcript table for every client, not
 * just the test numbers.
 */

import { randomBytes } from 'node:crypto';
import { employerLeadBlockers } from './blockers';
import { supabase, withPostgres, countForConversation } from './db';
import { env, mayReset } from './env';
import { findConversation, findLeads } from './lookup';
import { normalizePhone } from './phone';
import type { ResetRequest, ResetResult, ResetStep } from './types';

/**
 * Deleted child-first. Identical to WIPE_TABLES in the Python, comment and
 * all, because the ordering constraint is a property of the schema rather
 * than of either implementation.
 */
const WIPE_TABLES = ['cb_handovers', 'cb_tickets', 'wp_chat_messages'] as const;

/**
 * LangGraph's own tables, all keyed by thread_id. The cb_ prefix is ours:
 * the library hardcodes unprefixed names and app/graph/checkpointer.py
 * rewrites them, so these must stay in step with _NAMES over there.
 */
const CHECKPOINT_TABLES = [
  'cb_checkpoints',
  'cb_checkpoint_blobs',
  'cb_checkpoint_writes',
] as const;

/** app/services/conversation.py :: new_thread_id */
function newThreadId(conversationId: number | null): string {
  const suffix = randomBytes(6).toString('hex');
  return `conv-${conversationId ?? 'new'}-${suffix}`.slice(0, 100);
}

/**
 * Delete the abandoned thread's rows from LangGraph's checkpoint tables.
 *
 * Without this, resetting the conversation row still leaves collected_info,
 * asked_field_counts and everything else the checkpointer tracked sitting
 * under the abandoned thread id forever. Harmless to the next run, which
 * gets a fresh thread id either way, but it is real state from a "deleted"
 * conversation still in the database, accumulating one orphaned thread per
 * reset.
 *
 * A failure here does not fail the reset, matching the script: an
 * uninitialised checkpointer must not block handing a conversation back.
 */
async function wipeCheckpoints(threadId: string | null): Promise<ResetStep> {
  const label = 'LangGraph checkpoint';
  if (!threadId) {
    return { label, status: 'skipped', detail: 'the conversation had no thread id' };
  }
  if (!env.dbUrl) {
    return {
      label,
      status: 'skipped',
      detail:
        'SUPABASE_DB_URL is not set on the server, so the checkpoint tables cannot be reached. ' +
        'The reset is otherwise complete; set that variable to clear them too.',
    };
  }

  try {
    const total = await withPostgres(async (sql) => {
      let deleted = 0;
      for (const table of CHECKPOINT_TABLES) {
        // The table name is from the constant list above, never from input.
        const result = await sql`
          DELETE FROM ${sql(table)} WHERE thread_id = ${threadId}
        `;
        deleted += result.count ?? 0;
      }
      return deleted;
    });
    return {
      label,
      status: 'done',
      detail: `cleared ${total} row(s) for thread ${threadId}`,
    };
  } catch (error) {
    return {
      label,
      status: 'failed',
      detail: `could not clear checkpoint rows for thread ${threadId}: ${
        error instanceof Error ? error.message : String(error)
      }`,
    };
  }
}

/**
 * Delete one lead the client explicitly selected.
 *
 * The blockers are re-checked here rather than trusted from the lookup: the
 * page may have been open for a while, and a ticket raised in between would
 * turn a permitted delete into a foreign-key error. Re-reading is cheap and
 * the alternative is a Postgres error message shown to a non-technical user.
 */
async function deleteLead(
  table: 'leads' | 'leads_candidate',
  id: string,
  conversationId: number | null,
): Promise<ResetStep> {
  const label = table === 'leads' ? 'Employer lead' : 'Candidate lead';

  const existing = await supabase()
    .from(table)
    .select('id, lead_number, full_name')
    .eq('id', id)
    .eq('tenant_id', env.tenantId)
    .limit(1);

  if (existing.error) {
    return { label, status: 'failed', detail: `could not read ${table}: ${existing.error.message}` };
  }
  const row = (existing.data ?? [])[0];
  if (!row) {
    return {
      label,
      status: 'skipped',
      detail: 'the lead no longer exists -- somebody else may have deleted it already',
    };
  }

  const name = `${row.lead_number ?? id} -- ${row.full_name ?? 'no name'}`;

  if (table === 'leads') {
    // Re-checked at the moment of deletion, not trusted from the lookup:
    // the page may have been open a while, and a ticket raised in between
    // would turn a permitted delete into a foreign-key error.
    const blockers = await employerLeadBlockers(id, conversationId);
    if (blockers.length) {
      return {
        label,
        status: 'failed',
        detail: `${name} was NOT deleted. ${blockers.map((b) => b.reason).join(' ')}`,
      };
    }
  }

  const { error } = await supabase()
    .from(table)
    .delete()
    .eq('id', id)
    .eq('tenant_id', env.tenantId);
  if (error) {
    return { label, status: 'failed', detail: `${name} could not be deleted: ${error.message}` };
  }
  return { label, status: 'done', detail: `deleted ${name} from ${table}` };
}

export async function reset(request: ResetRequest): Promise<ResetResult> {
  const phone = normalizePhone(request.phone);
  const steps: ResetStep[] = [];

  if (!mayReset(phone)) {
    return {
      ok: false,
      phone,
      conversationId: null,
      steps,
      newThreadId: null,
      error:
        'This number is not on RESET_ALLOWED_NUMBERS, the safety list configured on the server.',
    };
  }

  const conversation = await findConversation(phone);

  // Every lead on this number, from both tables. Resolved here rather than
  // taken from the request: the browser sends a phone number and nothing
  // else, so there is no shape of request that can name somebody else's row.
  const leads = await findLeads(phone, conversation ? conversation.id : null);

  // No conversation is not necessarily an error. A lead can outlive its
  // conversation -- the thread was cleared before, or the lead was created by
  // the portal rather than by the bot -- and it still has to be clearable.
  // Refuse only when there is genuinely nothing on this number.
  if (!conversation && leads.length === 0) {
    return {
      ok: false,
      phone,
      conversationId: null,
      steps,
      newThreadId: null,
      error: `Nothing found for ${phone} -- no conversation and no lead. Check the number.`,
    };
  }

  const cid: number | null = conversation ? conversation.id : null;
  const oldThreadId: string | null = conversation?.langgraph_thread_id ?? null;

  // --- The conversation, when there is one ---------------------------------
  if (cid !== null) {
    if (request.keepHistory) {
      for (const table of WIPE_TABLES) {
        const count = await countForConversation(table, cid);
        steps.push({
          label: table,
          status: 'skipped',
          detail: `${count} row(s) left in place -- the bot will still see the previous enquiry`,
        });
      }
    } else {
      for (const table of WIPE_TABLES) {
        const count = await countForConversation(table, cid);
        const { error } = await supabase().from(table).delete().eq('conversation_id', cid);
        if (error) {
          steps.push({ label: table, status: 'failed', detail: error.message });
          return {
            ok: false,
            phone,
            conversationId: cid,
            steps,
            newThreadId: null,
            error: `Clearing ${table} failed, so nothing further was attempted: ${error.message}`,
          };
        }
        steps.push({
          label: table,
          status: 'done',
          detail: `cleared ${count} row(s) for conversation ${cid} only`,
        });
      }
    }

    // A fresh thread id is minted below regardless of keepHistory, so the old
    // thread's checkpoint is abandoned either way -- always clear it.
    steps.push(await wipeCheckpoints(oldThreadId));
  }

  // --- Every lead on this number -------------------------------------------
  //
  // Runs after the conversation's tickets are gone, so a lead referenced only
  // by a ticket on THIS conversation is now deletable. That ordering is the
  // whole reason the two happen in one operation.
  //
  // A lead that something else still references is refused rather than
  // attempted: the foreign keys on `leads` are ON DELETE NO ACTION, so the
  // attempt would fail anyway, and a checked refusal carries a reason the
  // client can act on where a raw Postgres error does not.
  if (leads.length === 0) {
    steps.push({
      label: 'Leads',
      status: 'skipped',
      detail: 'no lead on this number',
    });
  }
  for (const lead of leads) {
    steps.push(await deleteLead(lead.table, lead.id, cid));
  }

  // --- Hand the conversation back to the bot -------------------------------
  let threadId: string | null = null;
  if (cid !== null) {
    threadId = newThreadId(cid);
    const { error: updateError } = await supabase()
      .from('wp_chat_conversations')
      .update({
        bot_status: 'bot_active',
        status: 'open',
        langgraph_thread_id: threadId,
        intent: null,
        service_type: null,
        assignment_rule: null,
        last_message_body: null,
        last_bot_reply_at: null,
        // Identity too, or the next run inherits it. contact_type is only
        // re-derived while it is unknown, so a number left branded 'candidate'
        // by an earlier test had every later enquiry -- including "I am
        // looking for a maid" -- routed down the candidate flow.
        contact_type: null,
        matched_employer_id: null,
        matched_candidate_id: null,
        matched_supplier_id: null,
        matched_case_id: null,
        updated_at: new Date().toISOString(),
      })
      .eq('id', cid);

    if (updateError) {
      steps.push({ label: 'Conversation state', status: 'failed', detail: updateError.message });
      return {
        ok: false,
        phone,
        conversationId: cid,
        steps,
        newThreadId: null,
        error:
          'The rows were cleared but the conversation could not be handed back to the bot: ' +
          updateError.message,
      };
    }

    steps.push({
      label: 'Conversation state',
      status: 'done',
      detail: `bot_status='bot_active', fresh thread ${threadId}, identity cleared`,
    });
  } else {
    steps.push({
      label: 'Conversation state',
      status: 'skipped',
      detail:
        'this number has no conversation, so there was no thread to hand back to the bot',
    });
  }

  const failed = steps.filter((s) => s.status === 'failed');
  return {
    ok: failed.length === 0,
    phone,
    conversationId: cid,
    steps,
    newThreadId: threadId,
    error: failed.length
      ? 'Some steps did not complete -- see below. Anything marked done did happen.'
      : undefined,
  };
}
