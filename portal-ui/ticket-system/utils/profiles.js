/**
 * One place that knows which name column `profiles` actually has.
 *
 * The chatbot reads display_name; the original UI spec named full_name. Asking
 * for both fails outright — PostgREST rejects the whole select with 42703 when
 * either is missing — and the failure is quiet: the list still renders, just
 * with every agent name blank. So the column is negotiated once per client on
 * first use and remembered, and every caller goes through here.
 */

const NAME_COLUMN_CANDIDATES = ['display_name', 'full_name', 'name'];

/** Resolved column per supabase client, so the probe runs at most once. */
const resolved = new WeakMap();

function isMissingColumn(error) {
  if (!error) return false;
  return error.code === '42703' || /does not exist/i.test(error.message || '');
}

/**
 * The name column this database has, or null when none of the candidates do.
 * Concurrent callers share the same in-flight probe rather than racing it.
 */
export function resolveNameColumn(supabaseClient) {
  const cached = resolved.get(supabaseClient);
  if (cached) return cached;

  const probe = (async () => {
    for (const column of NAME_COLUMN_CANDIDATES) {
      const { error } = await supabaseClient.from('profiles').select(`id, ${column}`).limit(1);
      if (!error) return column;
      if (!isMissingColumn(error)) {
        // Permissions, network, anything else — it will fail identically for
        // the next candidate, so stop and let the caller surface it.
        return null;
      }
    }
    return null;
  })();

  resolved.set(supabaseClient, probe);
  return probe;
}

/** `id, display_name` — the select string to use for a profiles query. */
export async function profileSelect(supabaseClient, extraColumns = []) {
  const nameColumn = await resolveNameColumn(supabaseClient);
  return ['id', nameColumn, ...extraColumns].filter(Boolean).join(', ');
}

/**
 * Fetch profiles by id, already keyed for lookup. Never throws: a missing name
 * costs a label, and the ticket it labels has already loaded.
 */
export async function fetchProfilesByIds(supabaseClient, ids) {
  const unique = [...new Set((ids || []).filter(Boolean))];
  if (!unique.length) return new Map();

  try {
    const select = await profileSelect(supabaseClient);
    const { data, error } = await supabaseClient.from('profiles').select(select).in('id', unique);
    if (error) throw error;
    return new Map((data || []).map((row) => [String(row.id), row]));
  } catch (error) {
    console.warn('Could not load agent profiles', error);
    return new Map();
  }
}
