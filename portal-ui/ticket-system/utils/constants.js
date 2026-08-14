/**
 * Values used across the module.
 *
 * Every list here mirrors what the chatbot actually writes. Where the original
 * spec disagreed with the database, the database won — a filter built from a
 * value the column cannot hold silently returns nothing, which reads as "no
 * tickets" rather than as a bug.
 */

// 'in_progress' and 'closed' are valid on the column but the chatbot never
// writes them and the portal never has either — only 'open' and 'resolved'
// are ever set, so those are the only two offered as choices.
export const STATUS_OPTIONS = ['open', 'resolved'];

/**
 * The three values cb_tkt_priority_check permits on cb_tickets.priority.
 *
 * It was 'urgent'/'normal'; `migrations/001_priority_three_levels.sql` records
 * the change, all three phases of which have been applied. The column can no
 * longer hold an old value, so nothing here translates one.
 */
export const PRIORITY_OPTIONS = ['high', 'medium', 'low'];

/**
 * Every column value that should count as `priority`. A single-element list
 * now; it exists because the migration's middle phase needed the old value
 * folded in too, and a row could still be written by something older.
 */
export function priorityValues(priority) {
  return [priority];
}

/** The level a stored value belongs to. Medium is the chatbot's default. */
export function priorityLevel(value) {
  return value || 'medium';
}

/**
 * The eleven values cb_tkt_service_check permits on cb_tickets.service_type.
 * Stored snake_case, not title case.
 */
export const SERVICE_TYPE_OPTIONS = [
  'new_hiring',
  'direct_hiring',
  'replacement',
  'transfer',
  'renewal',
  'home_leave',
  'passport_renewal',
  'dispute_salary',
  'dispute_assault',
  'fee_enquiry',
  'salary_enquiry',
];

export const STATUS_LABELS = {
  open: 'Open',
  in_progress: 'In Progress',
  resolved: 'Resolved',
  closed: 'Closed',
};

export const PRIORITY_LABELS = {
  high: 'High',
  medium: 'Medium',
  low: 'Low',
};

export const SERVICE_TYPE_LABELS = {
  new_hiring: 'New Hiring',
  direct_hiring: 'Direct Hiring',
  replacement: 'Replacement',
  transfer: 'Transfer',
  renewal: 'Renewal',
  home_leave: 'Home Leave',
  passport_renewal: 'Passport Renewal',
  dispute_salary: 'Salary Dispute',
  dispute_assault: 'Safety Report',
  fee_enquiry: 'Fee Enquiry',
  salary_enquiry: 'Salary Enquiry',
};

/**
 * Topic keys the chatbot records in captured_info that cb_tickets.service_type
 * cannot hold. A ticket filed under a substitute still carries its real topic
 * here, so the agent opening it sees what the client actually asked for.
 */
export const TOPIC_KEY_LABELS = {
  ...SERVICE_TYPE_LABELS,
  candidate_new_hiring: 'Job Seeker (Helper)',
  candidate_registration: 'Helper Registration',
  media_received: 'Attachment Received',
  general_question: 'General Question',
  process_question: 'Process Question',
  document_question: 'Document Question',
  case_enquiry: 'Case Enquiry',
};

/** wp_chat_conversations.contact_type */
export const CONTACT_TYPE_LABELS = {
  employer: 'Employer',
  candidate: 'Helper',
  supplier: 'Agent',
  partner: 'Partner',
  unknown: 'Unknown',
};

/** wp_chat_conversations.bot_status */
export const BOT_STATUS_LABELS = {
  bot_active: 'Bot replying',
  human_active: 'Human handling',
  none: 'Bot not engaged',
};

/** cb_handovers.direction */
export const HANDOVER_DIRECTION_LABELS = {
  bot_to_human: 'Bot → Human',
  human_to_bot: 'Human → Bot',
};

/** cb_handovers.reason */
export const HANDOVER_REASON_LABELS = {
  ticket_raised: 'Ticket raised',
  dispute_escalation: 'Dispute escalated',
  bot_confused: 'Bot could not answer',
  client_requested: 'Client asked for a human',
  agent_resolved: 'Agent resolved the thread',
  agent_takeover: 'Agent replied directly',
  agent_idle: 'Agent went idle',
  agent_pause_expired: 'Agent pause expired',
  fee_enquiry: 'Fee enquiry',
  salary_enquiry: 'Salary enquiry',
  media_received: 'Attachment received',
  candidate_registration: 'Helper registration',
};

/**
 * Statuses that keep a ticket live. While a ticket sits in one of these the
 * chatbot will not answer its topic on that conversation — it acknowledges and
 * waits for the human. Resolving or closing hands the topic back to the bot.
 */
export const OPEN_STATUSES = ['open', 'in_progress'];

/** Brand palette. */
export const COLORS = {
  darkBlue: '#003E60',
  blue: '#0D7AD2',
  red: '#DF0000',
  amber: '#D97706',
  green: '#059669',
  grey: '#9CA3AF',
};

export const STATUS_CHART_COLORS = {
  open: COLORS.blue,
  in_progress: COLORS.amber,
  resolved: COLORS.green,
  closed: COLORS.grey,
};

export const PRIORITY_CHART_COLORS = {
  high: COLORS.red,
  medium: COLORS.blue,
  low: COLORS.grey,
};

/**
 * Donut slices, assigned in this fixed order and never cycled.
 *
 * Checked for colourblind separation against a light and a dark surface: worst
 * adjacent pair ΔE 9.4 (deuteranopia), normal-vision floor 31.1, every slot
 * above the 3:1 contrast floor. Reordering these, or adding an eighth hue,
 * invalidates that — hence OTHER_SERVICE_COLOR below.
 */
export const SERVICE_CHART_COLORS = [
  '#0D7AD2',
  '#D97706',
  '#8B5CF6',
  '#059669',
  '#DF0000',
  '#0891B2',
  '#DB2777',
];

/**
 * Eleven service types will not survive as eleven distinct hues. Anything past
 * the seventh is folded into one "Other" slice, which the drawer and the table
 * can still break down exactly.
 */
export const MAX_SERVICE_SLICES = SERVICE_CHART_COLORS.length;
export const OTHER_SERVICE_COLOR = '#9CA3AF';
export const OTHER_SERVICE_KEY = '__other__';

/**
 * Who can hold a ticket.
 *
 * `profiles` is the platform-wide people table — employers, suppliers,
 * partners, transporters and runners share it with staff. Offering all of them
 * put "Manila Source Agency" and "J3P Transport" in the assignment dropdown,
 * one keystroke away from a mis-assignment nobody would notice.
 *
 * These are the archetypes the chatbot itself routes to: sales via the
 * round-robin, admin for safety escalations and partner enquiries. manager and
 * super_admin are included because a human reassigning upward is normal.
 * Widen this list if operations want runners on tickets too.
 */
export const ASSIGNABLE_ARCHETYPES = ['sales', 'admin', 'manager', 'super_admin'];

/** profiles.status — a suspended account is still in the table. */
export const ACTIVE_PROFILE_STATUS = 'active';

export const DEFAULT_PAGE_SIZE = 20;

export const EMPTY_FILTERS = {
  search: '',
  status: '',
  service_type: '',
  priority: '',
  assigned_agent_id: '',
  date_from: '',
  date_to: '',
};
