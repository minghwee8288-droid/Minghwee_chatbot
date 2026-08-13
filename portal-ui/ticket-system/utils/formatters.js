/**
 * Display helpers. Every timestamp in this module is rendered in Singapore
 * time; the database stores UTC.
 */

import {
  CONTACT_TYPE_LABELS,
  HANDOVER_DIRECTION_LABELS,
  HANDOVER_REASON_LABELS,
  SERVICE_TYPE_LABELS,
  TOPIC_KEY_LABELS,
} from './constants';

const DASH = '—';

/** Format any UTC timestamp to Singapore time (UTC+8). */
export function formatSGT(isoString, includeTime = true) {
  if (!isoString) return DASH;
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return DASH;
  return new Intl.DateTimeFormat('en-SG', {
    timeZone: 'Asia/Singapore',
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    ...(includeTime ? { hour: '2-digit', minute: '2-digit', hour12: false } : {}),
  }).format(date);
}

/** Time only (HH:mm), for chat bubbles. */
export function formatTimeSGT(isoString) {
  if (!isoString) return '';
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat('en-SG', {
    timeZone: 'Asia/Singapore',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date);
}

/** Date only, used as the day divider in a conversation thread. */
export function formatDateSGT(isoString) {
  return formatSGT(isoString, false);
}

/** captured_info keys are snake_case; render them as words. */
export function formatKey(key) {
  return String(key || '')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * Render a captured_info value as text. Values arrive as strings, numbers,
 * booleans or arrays of any of those; objects are handled a level up by
 * CapturedInfoPanel, which walks them rather than stringifying them.
 */
export function formatValue(value) {
  if (value === null || value === undefined || value === '') return DASH;
  if (Array.isArray(value)) {
    const parts = value.map((item) =>
      item !== null && typeof item === 'object' ? JSON.stringify(item) : String(item),
    );
    return parts.length ? parts.join(', ') : DASH;
  }
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

/** A cb_tickets.service_type entry, in the words an agent uses. */
export function serviceTypeLabel(value) {
  if (!value) return DASH;
  return SERVICE_TYPE_LABELS[value] || formatKey(value);
}

/**
 * A captured_info topic key. Wider than serviceTypeLabel: topics the portal's
 * column cannot hold (a job seeker, an attachment, an unanswerable question)
 * only ever appear here.
 */
export function topicLabel(value) {
  if (!value) return DASH;
  return TOPIC_KEY_LABELS[value] || formatKey(value);
}

export function contactTypeLabel(value) {
  if (!value) return DASH;
  return CONTACT_TYPE_LABELS[value] || formatKey(value);
}

export function handoverDirectionLabel(value) {
  if (!value) return DASH;
  return HANDOVER_DIRECTION_LABELS[value] || formatKey(value);
}

export function handoverReasonLabel(value) {
  if (!value) return DASH;
  return HANDOVER_REASON_LABELS[value] || formatKey(value);
}

/**
 * cb_tickets.service_type is text[]. A null column, or a row written before the
 * array migration, must still render as a list rather than throwing.
 */
export function asServiceArray(value) {
  if (Array.isArray(value)) return value.filter(Boolean);
  if (typeof value === 'string' && value) return [value];
  return [];
}

/**
 * The agent's name. profiles uses display_name; full_name is selected too and
 * used as a fallback, so the column that exists is the one that shows.
 */
export function agentName(profile) {
  if (!profile) return null;
  return profile.display_name || profile.full_name || profile.name || null;
}

/** '+65 9123 4567' from the bare digits the portal stores. */
export function formatPhone(number) {
  const digits = String(number || '').replace(/\D/g, '');
  if (!digits) return DASH;
  if (digits.length === 10 && digits.startsWith('65')) {
    return `+65 ${digits.slice(2, 6)} ${digits.slice(6)}`;
  }
  return `+${digits}`;
}

/** Does this search term look like someone typing a phone number? */
export function looksLikePhone(term) {
  const trimmed = String(term || '').trim();
  if (trimmed.length < 3) return false;
  return /^[+\d][\d\s()+-]*$/.test(trimmed);
}

/** Trim a long single-line value for a table cell. */
export function truncate(text, max = 80) {
  const body = String(text || '').replace(/\s+/g, ' ').trim();
  if (body.length <= max) return body;
  return `${body.slice(0, max).trimEnd()}…`;
}

/** First line of a ticket description — the chatbot writes the summary there. */
export function firstLine(text) {
  const [line] = String(text || '').split('\n');
  return line.trim();
}

export { DASH };
