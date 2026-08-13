/**
 * Aggregates for the KPI cards and the two charts.
 *
 * Status and priority counts come back as head-only count queries — one round
 * trip each, no rows transferred. service_type cannot be counted that way: it
 * is a text[] and one ticket can sit in several buckets after a merge, so the
 * column is fetched on its own and unnested here.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  PRIORITY_OPTIONS,
  SERVICE_TYPE_OPTIONS,
  STATUS_OPTIONS,
  priorityValues,
} from '../utils/constants';
import { asServiceArray } from '../utils/formatters';

const TICKETS = 'cb_tickets';

/** Ceiling on rows pulled for the service_type breakdown. */
const MAX_SERVICE_ROWS = 5000;

function emptyCounts(keys) {
  return keys.reduce((acc, key) => ({ ...acc, [key]: 0 }), {});
}

async function countWhere(supabaseClient, column, value) {
  const { count, error } = await supabaseClient
    .from(TICKETS)
    .select('id', { count: 'exact', head: true })
    .eq(column, value);
  if (error) throw error;
  return count || 0;
}

/** One count over a level and the old values that fold into it. */
async function countPriority(supabaseClient, priority) {
  const { count, error } = await supabaseClient
    .from(TICKETS)
    .select('id', { count: 'exact', head: true })
    .in('priority', priorityValues(priority));
  if (error) throw error;
  return count || 0;
}

export default function useDashboardStats({ supabaseClient }) {
  const [total, setTotal] = useState(0);
  const [byStatus, setByStatus] = useState(() => emptyCounts(STATUS_OPTIONS));
  const [byPriority, setByPriority] = useState(() => emptyCounts(PRIORITY_OPTIONS));
  const [byServiceType, setByServiceType] = useState([]);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const requestRef = useRef(0);

  const fetchStats = useCallback(async () => {
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;

    setLoading(true);
    setError(null);

    try {
      const [totalResult, statusCounts, priorityCounts, serviceRows] = await Promise.all([
        supabaseClient.from(TICKETS).select('id', { count: 'exact', head: true }),

        Promise.all(STATUS_OPTIONS.map((status) => countWhere(supabaseClient, 'status', status))),

        Promise.all(
          PRIORITY_OPTIONS.map((priority) => countPriority(supabaseClient, priority)),
        ),

        supabaseClient.from(TICKETS).select('service_type').limit(MAX_SERVICE_ROWS),
      ]);

      if (totalResult.error) throw totalResult.error;
      if (serviceRows.error) throw serviceRows.error;

      const rows = serviceRows.data || [];
      const tally = new Map();
      for (const row of rows) {
        // One ticket covering two services counts once in each bucket: that is
        // what the array means, and what an agent scanning the chart expects.
        for (const service of asServiceArray(row.service_type)) {
          tally.set(service, (tally.get(service) || 0) + 1);
        }
      }

      // Known services in their canonical order, then anything unrecognised —
      // a value written before the current constraint still has to show up.
      const ordered = [
        ...SERVICE_TYPE_OPTIONS.filter((service) => tally.has(service)),
        ...[...tally.keys()].filter((service) => !SERVICE_TYPE_OPTIONS.includes(service)).sort(),
      ];

      if (requestRef.current !== requestId) return;

      setTotal(totalResult.count || 0);
      setByStatus(
        STATUS_OPTIONS.reduce((acc, status, i) => ({ ...acc, [status]: statusCounts[i] }), {}),
      );
      setByPriority(
        PRIORITY_OPTIONS.reduce((acc, p, i) => ({ ...acc, [p]: priorityCounts[i] }), {}),
      );
      setByServiceType(ordered.map((service) => ({ service, count: tally.get(service) })));
      setTruncated(rows.length >= MAX_SERVICE_ROWS);
    } catch (err) {
      if (requestRef.current !== requestId) return;
      console.error('Dashboard stats query failed', err);
      setError(err);
      setTotal(0);
      setByStatus(emptyCounts(STATUS_OPTIONS));
      setByPriority(emptyCounts(PRIORITY_OPTIONS));
      setByServiceType([]);
      setTruncated(false);
    } finally {
      if (requestRef.current === requestId) setLoading(false);
    }
  }, [supabaseClient]);

  useEffect(() => {
    fetchStats();
  }, [fetchStats]);

  return {
    total,
    byStatus,
    byServiceType,
    byPriority,
    truncated,
    loading,
    error,
    refetch: fetchStats,
  };
}
