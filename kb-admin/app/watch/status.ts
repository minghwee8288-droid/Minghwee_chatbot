import type { Tone } from '@/components/ui';

/** How each cb_kb_drafts.status reads in the review queue. */
export const DRAFT_STATUS: Record<string, { text: string; tone: Tone }> = {
  pending: { text: 'Waiting for review', tone: 'warn' },
  blocked: { text: 'Needs agency confirmation', tone: 'lock' },
  approved: { text: 'Approved, not yet live', tone: 'warn' },
  published: { text: 'Live', tone: 'good' },
  rejected: { text: 'Rejected', tone: 'off' },
  superseded: { text: 'Replaced by a newer proposal', tone: 'off' },
};
