/**
 * Profiles offered in the assignment dropdown.
 *
 * Narrowed to staff. `profiles` holds everybody on the platform — employers,
 * suppliers, partners, transporters — and an unfiltered list put agencies and
 * client families in a dropdown that assigns work to them. Suspended accounts
 * are dropped too: they are duplicated by an active row of the same name, so
 * showing both offers a choice between two identical-looking options where one
 * silently goes nowhere.
 *
 * The name column is negotiated by utils/profiles — the chatbot reads
 * display_name, the original UI spec named full_name, and selecting both fails
 * the whole query on whichever database lacks one of them.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ACTIVE_PROFILE_STATUS, ASSIGNABLE_ARCHETYPES } from '../utils/constants';
import { agentName } from '../utils/formatters';
import { profileSelect } from '../utils/profiles';

const PROFILES = 'profiles';

function isMissingColumn(error) {
  if (!error) return false;
  return error.code === '42703' || /does not exist/i.test(error.message || '');
}

export default function useAgents({ supabaseClient }) {
  const [agents, setAgents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const requestRef = useRef(0);

  const fetchAgents = useCallback(async () => {
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;

    setLoading(true);
    setError(null);

    try {
      const select = await profileSelect(supabaseClient, ['archetype_key', 'status']);

      let { data, error: queryError } = await supabaseClient
        .from(PROFILES)
        .select(select)
        .in('archetype_key', ASSIGNABLE_ARCHETYPES)
        .eq('status', ACTIVE_PROFILE_STATUS);

      // A portal whose profiles table has neither column still gets a usable
      // dropdown — a wide list beats an empty one.
      if (queryError && isMissingColumn(queryError)) {
        console.warn('profiles has no archetype_key/status — listing all profiles', queryError);
        const fallbackSelect = await profileSelect(supabaseClient);
        ({ data, error: queryError } = await supabaseClient.from(PROFILES).select(fallbackSelect));
      }
      if (queryError) throw queryError;

      if (requestRef.current !== requestId) return;

      // Sorted here rather than in the query: the column to order by is
      // whichever name column this database turned out to have.
      const rows = [...(data || [])].sort((a, b) =>
        (agentName(a) || '').localeCompare(agentName(b) || ''),
      );
      setAgents(rows);
    } catch (err) {
      if (requestRef.current !== requestId) return;
      console.error('Agent list query failed', err);
      setError(err);
      setAgents([]);
    } finally {
      if (requestRef.current === requestId) setLoading(false);
    }
  }, [supabaseClient]);

  useEffect(() => {
    fetchAgents();
  }, [fetchAgents]);

  return { agents, loading, error, refetch: fetchAgents };
}
