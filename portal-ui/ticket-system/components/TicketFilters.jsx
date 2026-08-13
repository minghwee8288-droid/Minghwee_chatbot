import React from 'react';
import {
  EMPTY_FILTERS,
  PRIORITY_LABELS,
  PRIORITY_OPTIONS,
  SERVICE_TYPE_OPTIONS,
  STATUS_LABELS,
  STATUS_OPTIONS,
} from '../utils/constants';
import { agentName, serviceTypeLabel } from '../utils/formatters';

/**
 * The filter bar.
 *
 * Laid out on a grid rather than as a wrapping flex row. Wrapping put the
 * controls at whatever widths their content happened to want and left "Clear
 * Filters" stranded on a line of its own; on the grid every control is the same
 * height and lines up in a column, and the clear action lives in the bar's
 * header where it reads as a control for the whole panel.
 */

const CONTROL_CLASS =
  'h-9 w-full rounded-md border border-gray-300 bg-white px-2.5 text-sm text-gray-800 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500';

function Field({ label, className = '', children }) {
  return (
    <label className={`flex min-w-0 flex-col gap-1.5 ${className}`}>
      <span className="text-xs font-bold uppercase tracking-wide text-gray-500">{label}</span>
      {children}
    </label>
  );
}

export default function TicketFilters({ filters, onFilterChange, agents = [], agentsLoading }) {
  const set = (patch) => onFilterChange({ ...filters, ...patch });

  const activeCount = Object.keys(EMPTY_FILTERS).filter((key) => filters[key]).length;

  return (
    <div className="overflow-hidden rounded-lg border border-gray-200 bg-white">
      <div className="flex items-center justify-between gap-3 border-b border-gray-200 bg-gray-50 px-5 py-2.5">
        <h2 className="text-sm font-bold text-[#003E60]">Filters</h2>
        <div className="flex items-center gap-3">
          {activeCount ? (
            <span className="rounded-full bg-[#0D7AD2] px-2 py-0.5 text-xs font-bold text-white">
              {activeCount} active
            </span>
          ) : null}
          <button
            type="button"
            onClick={() => onFilterChange({ ...EMPTY_FILTERS })}
            disabled={!activeCount}
            className="rounded-md px-2 py-1 text-sm font-bold text-[#0D7AD2] hover:bg-blue-50 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:cursor-not-allowed disabled:text-gray-400 disabled:hover:bg-transparent"
          >
            Clear all
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-x-4 gap-y-4 p-5 sm:grid-cols-2 lg:grid-cols-4">
        <Field label="Search" className="sm:col-span-2">
          <div className="relative">
            <svg
              className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth="2"
              aria-hidden="true"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="m21 21-4.3-4.3M17 11a6 6 0 1 1-12 0 6 6 0 0 1 12 0Z"
              />
            </svg>
            <input
              type="search"
              value={filters.search}
              onChange={(event) => set({ search: event.target.value })}
              placeholder="Reference, description, phone number or name"
              className={`${CONTROL_CLASS} pl-9`}
            />
          </div>
        </Field>

        <Field label="Status">
          <select
            value={filters.status}
            onChange={(event) => set({ status: event.target.value })}
            className={CONTROL_CLASS}
          >
            <option value="">All statuses</option>
            {STATUS_OPTIONS.map((status) => (
              <option key={status} value={status}>
                {STATUS_LABELS[status]}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Service Type">
          <select
            value={filters.service_type}
            onChange={(event) => set({ service_type: event.target.value })}
            className={CONTROL_CLASS}
          >
            <option value="">All services</option>
            {SERVICE_TYPE_OPTIONS.map((service) => (
              <option key={service} value={service}>
                {serviceTypeLabel(service)}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Priority">
          <select
            value={filters.priority}
            onChange={(event) => set({ priority: event.target.value })}
            className={CONTROL_CLASS}
          >
            <option value="">All priorities</option>
            {PRIORITY_OPTIONS.map((priority) => (
              <option key={priority} value={priority}>
                {PRIORITY_LABELS[priority]}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Assigned Agent">
          <select
            value={filters.assigned_agent_id}
            onChange={(event) => set({ assigned_agent_id: event.target.value })}
            className={CONTROL_CLASS}
            disabled={agentsLoading}
          >
            <option value="">{agentsLoading ? 'Loading agents…' : 'All agents'}</option>
            {agents.map((agent) => (
              <option key={agent.id} value={agent.id}>
                {agentName(agent) || agent.id}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Created From">
          <input
            type="date"
            value={filters.date_from}
            onChange={(event) => set({ date_from: event.target.value })}
            className={CONTROL_CLASS}
          />
        </Field>

        <Field label="Created To">
          <input
            type="date"
            value={filters.date_to}
            onChange={(event) => set({ date_to: event.target.value })}
            className={CONTROL_CLASS}
          />
        </Field>
      </div>
    </div>
  );
}
