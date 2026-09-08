/** Shapes shared between the API routes and the page. */

export type LeadKind = 'employer' | 'candidate';

export type LeadBlocker = {
  table: string;
  column: string;
  count: number;
  /** Plain-English reason this reference stops the lead being deleted. */
  reason: string;
};

export type LeadRow = {
  id: string;
  table: 'leads' | 'leads_candidate';
  kind: LeadKind;
  leadNumber: string | null;
  fullName: string | null;
  phone: string | null;
  email: string | null;
  status: string | null;
  source: string | null;
  createdAt: string | null;
  /** Employer leads only. */
  interestType?: string | null;
  requirement?: string | null;
  /** Candidate leads only. */
  nationality?: string | null;
  /** Empty when the lead can be deleted. */
  blockers: LeadBlocker[];
};

export type ContactIdentity = {
  type: 'employer' | 'candidate' | 'unknown';
  id: string | null;
  name: string | null;
  phone: string | null;
  /** Non-archived `placements` rows — the only record of "they hired through us". */
  priorHires?: number;
  nationality?: string | null;
};

export type MessagePreview = {
  id: number;
  direction: string | null;
  isBot: boolean | null;
  body: string | null;
  createdAt: string | null;
};

export type ConversationSummary = {
  id: number;
  customerNumber: string | null;
  customerName: string | null;
  botStatus: string | null;
  status: string | null;
  threadId: string | null;
  lastMessageAt: string | null;
  serviceType: string | null;
  intent: string | null;
  contactType: string | null;
  counts: {
    messages: number;
    tickets: number;
    handovers: number;
  };
};

export type LookupResult = {
  phone: string;
  allowed: boolean;
  /** Set when the safety gate refuses this number. */
  blockedReason?: string;
  conversation: ConversationSummary | null;
  contact: ContactIdentity;
  leads: LeadRow[];
  recentMessages: MessagePreview[];
  checkpointsReachable: boolean;
};

export type ResetRequest = {
  /**
   * The number to clear. Everything deleted is resolved from this, server
   * side -- the browser names a phone number and nothing else. There is no
   * request shape that can address a row belonging to anybody else.
   */
  phone: string;
  /**
   * `--keep-history`: reset the routing state but delete no transcript.
   * Not exposed in the UI; kept because the Python script has it and a
   * caller may want it.
   */
  keepHistory?: boolean;
};

export type ResetStep = {
  label: string;
  detail: string;
  status: 'done' | 'skipped' | 'failed';
};

export type ResetResult = {
  ok: boolean;
  phone: string;
  conversationId: number | null;
  steps: ResetStep[];
  newThreadId: string | null;
  error?: string;
};
