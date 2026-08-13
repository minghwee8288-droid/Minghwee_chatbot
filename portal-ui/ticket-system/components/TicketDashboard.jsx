import React, { useMemo } from 'react';
import {
  Bar,
  BarChart,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {
  COLORS,
  MAX_SERVICE_SLICES,
  OTHER_SERVICE_COLOR,
  OTHER_SERVICE_KEY,
  PRIORITY_CHART_COLORS,
  PRIORITY_LABELS,
  PRIORITY_OPTIONS,
  SERVICE_CHART_COLORS,
} from '../utils/constants';
import { serviceTypeLabel } from '../utils/formatters';
import ErrorState from './ErrorState';
import LoadingSkeleton from './LoadingSkeleton';

/**
 * Five counts and two charts.
 *
 * The donut caps at seven hues plus one "Other". Eleven service types cannot be
 * told apart by colour at donut-slice size — past the seventh the tail folds
 * into a single grey slice whose tooltip still names what is in it.
 */

const CARD_COLORS = {
  total: COLORS.darkBlue,
  open: COLORS.blue,
  in_progress: COLORS.amber,
  resolved: COLORS.green,
  closed: COLORS.grey,
};

function KpiCard({ label, value, color, loading, hint }) {
  return (
    <div className="flex overflow-hidden rounded-lg border border-gray-200 bg-white">
      <div className="w-1 flex-shrink-0" style={{ backgroundColor: color }} aria-hidden="true" />
      <div className="min-w-0 flex-1 px-4 py-4">
        <p className="text-xs font-bold uppercase tracking-wide text-gray-500">{label}</p>
        {loading ? (
          <div className="mt-2 h-8 w-14 animate-pulse rounded bg-gray-200" />
        ) : (
          <p className="mt-1.5 text-3xl font-black leading-none text-[#003E60]">{value}</p>
        )}
        {/* Reserved whether or not this card has a hint, so the five cards keep
            one baseline instead of stepping up and down across the row. */}
        <p className="mt-1.5 h-4 text-xs leading-4 text-gray-400">{hint || ''}</p>
      </div>
    </div>
  );
}

function ChartCard({ title, subtitle, children }) {
  return (
    <div className="flex h-full flex-col rounded-lg border border-gray-200 bg-white p-5">
      <h3 className="text-sm font-bold text-[#003E60]">{title}</h3>
      <p className="mt-1 text-xs leading-5 text-gray-500">{subtitle || ' '}</p>
      <div className="mt-5 flex-1">{children}</div>
    </div>
  );
}

/**
 * The donut's legend, as a list beside the ring rather than a strip under it.
 *
 * Recharts' own bottom legend left the card two-thirds empty and squeezed the
 * ring into the middle third. A vertical list uses the width, and it has room
 * for the count and share that a horizontal strip has to drop.
 */
function DonutLegend({ data, total }) {
  return (
    <ul className="min-w-0 flex-1 space-y-1.5">
      {data.map((entry) => (
        <li key={entry.key} className="flex items-baseline gap-2 text-sm">
          <span
            className="mt-1 h-2.5 w-2.5 flex-shrink-0 rounded-full"
            style={{ backgroundColor: entry.color }}
            aria-hidden="true"
          />
          <span className="min-w-0 flex-1 truncate text-gray-700" title={entry.label}>
            {entry.label}
          </span>
          <span className="flex-shrink-0 font-bold tabular-nums text-[#003E60]">{entry.count}</span>
          <span className="w-10 flex-shrink-0 text-right text-xs tabular-nums text-gray-400">
            {total ? `${Math.round((entry.count / total) * 100)}%` : ''}
          </span>
        </li>
      ))}
    </ul>
  );
}

function ChartTooltip({ active, payload }) {
  if (!active || !payload || !payload.length) return null;
  const entry = payload[0];
  const datum = entry.payload || {};
  return (
    <div className="rounded-md border border-gray-200 bg-white px-3 py-2 text-sm shadow-lg">
      <p className="font-bold text-gray-800">{datum.label}</p>
      <p className="text-gray-600">
        {entry.value} {entry.value === 1 ? 'ticket' : 'tickets'}
        {datum.share ? ` · ${datum.share}%` : ''}
      </p>
      {datum.members ? <p className="mt-1 text-xs text-gray-500">{datum.members}</p> : null}
    </div>
  );
}

export default function TicketDashboard({
  total,
  byStatus,
  byServiceType,
  byPriority,
  truncated,
  loading,
  error,
  onRetry,
}) {
  const serviceData = useMemo(() => {
    const rows = byServiceType || [];
    const grandTotal = rows.reduce((sum, row) => sum + row.count, 0);
    const share = (count) => (grandTotal ? Math.round((count / grandTotal) * 100) : 0);

    const head = rows.slice(0, MAX_SERVICE_SLICES).map((row, index) => ({
      key: row.service,
      label: serviceTypeLabel(row.service),
      count: row.count,
      share: share(row.count),
      color: SERVICE_CHART_COLORS[index],
    }));

    const tail = rows.slice(MAX_SERVICE_SLICES);
    if (!tail.length) return head;

    const tailCount = tail.reduce((sum, row) => sum + row.count, 0);
    return [
      ...head,
      {
        key: OTHER_SERVICE_KEY,
        label: `Other (${tail.length})`,
        count: tailCount,
        share: share(tailCount),
        color: OTHER_SERVICE_COLOR,
        members: tail.map((row) => `${serviceTypeLabel(row.service)} ${row.count}`).join(' · '),
      },
    ];
  }, [byServiceType]);

  const priorityData = useMemo(
    () =>
      PRIORITY_OPTIONS.map((priority) => ({
        key: priority,
        label: PRIORITY_LABELS[priority],
        count: (byPriority || {})[priority] || 0,
        color: PRIORITY_CHART_COLORS[priority],
      })),
    [byPriority],
  );

  if (error) {
    return (
      <div className="space-y-4">
        <ErrorState message={error} onRetry={onRetry} />
      </div>
    );
  }

  const status = byStatus || {};
  const serviceTotal = serviceData.reduce((sum, row) => sum + row.count, 0);
  // A floor of 1 keeps the axis from collapsing when every count is zero.
  const maxPriority = Math.max(1, ...priorityData.map((row) => row.count));

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5">
        <KpiCard label="Total" value={total} color={CARD_COLORS.total} loading={loading} />
        <KpiCard
          label="Open"
          value={status.open || 0}
          color={CARD_COLORS.open}
          loading={loading}
          hint="Bot paused on topic"
        />
        <KpiCard
          label="In Progress"
          value={status.in_progress || 0}
          color={CARD_COLORS.in_progress}
          loading={loading}
          hint="Bot paused on topic"
        />
        <KpiCard
          label="Resolved"
          value={status.resolved || 0}
          color={CARD_COLORS.resolved}
          loading={loading}
        />
        <KpiCard
          label="Closed"
          value={status.closed || 0}
          color={CARD_COLORS.closed}
          loading={loading}
        />
      </div>

      <div className="grid grid-cols-1 items-stretch gap-5 lg:grid-cols-2">
        <ChartCard
          title="Tickets by Service Type"
          subtitle={
            truncated
              ? 'Based on the most recent 5,000 tickets. A merged ticket counts once per service.'
              : 'A merged ticket counts once per service it covers.'
          }
        >
          {loading ? (
            <LoadingSkeleton variant="block" height="h-56" />
          ) : !serviceTotal ? (
            <p className="py-16 text-center text-sm text-gray-500">No tickets yet</p>
          ) : (
            <div className="flex flex-col items-center gap-4 sm:flex-row">
              <div className="relative h-[224px] w-[224px] flex-shrink-0">
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie
                      data={serviceData}
                      dataKey="count"
                      nameKey="label"
                      cx="50%"
                      cy="50%"
                      innerRadius={62}
                      outerRadius={104}
                      // A 2px surface gap between slices keeps adjacent fills
                      // apart without relying on the hue difference alone.
                      paddingAngle={2}
                      stroke="#ffffff"
                      strokeWidth={2}
                      // Recharts grows marks from zero on mount. A
                      // ResponsiveContainer that settles its width mid-animation
                      // can leave them there, painting an empty plot. Nothing
                      // here benefits from the motion.
                      isAnimationActive={false}
                    >
                      {serviceData.map((entry) => (
                        <Cell key={entry.key} fill={entry.color} />
                      ))}
                    </Pie>
                    <Tooltip content={<ChartTooltip />} />
                  </PieChart>
                </ResponsiveContainer>
                {/* Centred over the hole rather than drawn into the SVG, so it
                    cannot be clipped by the plot area and inherits page type. */}
                <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
                  <span className="text-3xl font-black leading-none text-[#003E60]">
                    {serviceTotal}
                  </span>
                  <span className="mt-0.5 text-xs uppercase tracking-wide text-gray-400">
                    services
                  </span>
                </div>
              </div>
              <DonutLegend data={serviceData} total={serviceTotal} />
            </div>
          )}
        </ChartCard>

        <ChartCard
          title="Tickets by Priority"
          subtitle="The chatbot sets High for safety reports and Medium otherwise; agents re-triage from the ticket."
        >
          {loading ? (
            <LoadingSkeleton variant="block" height="h-56" />
          ) : (
            /* Columns. Three of the four priorities are almost always zero, so
               each column sits in a full-height grey track — a zero reads as an
               empty column under its label rather than as a missing bar. */
            <ResponsiveContainer width="100%" height={224}>
              <BarChart
                data={priorityData}
                margin={{ top: 22, right: 4, bottom: 0, left: 4 }}
                barCategoryGap="26%"
              >
                <XAxis
                  dataKey="label"
                  tickLine={false}
                  axisLine={{ stroke: '#E5E7EB' }}
                  tickMargin={8}
                  tick={{ fill: '#374151', fontSize: 13 }}
                />
                <YAxis type="number" hide allowDecimals={false} domain={[0, maxPriority]} />
                <Tooltip content={<ChartTooltip />} cursor={{ fill: '#F9FAFB' }} />
                {/* See the note on the donut: mount animation is off so the
                    bars cannot be left at zero height by a resize. */}
                <Bar
                  dataKey="count"
                  radius={[4, 4, 0, 0]}
                  maxBarSize={64}
                  isAnimationActive={false}
                  label={{
                    position: 'top',
                    fill: '#374151',
                    fontSize: 13,
                    fontWeight: 700,
                  }}
                  // A zero column still needs a visible track, otherwise the
                  // category label floats under nothing.
                  background={{ fill: '#F3F4F6', radius: 4 }}
                >
                  {priorityData.map((entry) => (
                    <Cell key={entry.key} fill={entry.color} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          )}
        </ChartCard>
      </div>
    </div>
  );
}
