import { Pill } from '@/components/ui';

/** The red badge on a page read without a certificate check (017), and on every proposal from it. */
export function UnverifiedBadge({ reason }: { reason?: string | null }) {
  return (
    <span title={reason ?? undefined} data-unverified-badge>
      <Pill tone="bad">Unverified source</Pill>
    </span>
  );
}
