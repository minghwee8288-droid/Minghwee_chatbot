/**
 * Phone normalisation, ported line for line from `app/utils.py`.
 *
 * This must stay in step with the Python. The chatbot writes
 * `wp_chat_conversations.customer_number` as BARE DIGITS via
 * `conversation.storage_key()`, while the platform tables (`employers`,
 * `leads`) hold whatever a human typed years ago — '+65 9188 4442',
 * '6591234567', '91234567'. Look a number up with the wrong rules and this
 * tool reports "no conversation" for a client who has one, or worse, offers
 * up somebody else's row to delete.
 *
 * Divergence risk is real and is why each function names its Python original.
 */

/** app/utils.py :: digits_only */
export function digitsOnly(phone: string | null | undefined): string {
  return (phone ?? '').replace(/\D+/g, '');
}

/**
 * app/utils.py :: normalize_phone
 *
 * Accepts '6591234567@s.whatsapp.net', '65 9123 4567', '+6591234567'.
 * A bare 8-digit number starting 3/6/8/9 is assumed Singaporean.
 */
export function normalizePhone(raw: string | null | undefined): string {
  if (!raw) return '';
  const value = raw.split('@', 1)[0];
  let digits = digitsOnly(value);
  if (!digits) return '';
  if (digits.length === 8 && '3689'.includes(digits[0])) {
    digits = `65${digits}`;
  }
  return `+${digits}`;
}

/**
 * app/utils.py :: phone_variants
 *
 * Every spelling of a number we might find in the platform tables.
 */
export function phoneVariants(phone: string | null | undefined): string[] {
  const digits = digitsOnly(phone);
  if (!digits) return [];
  const variants = new Set<string>([digits, `+${digits}`]);
  if (digits.startsWith('65') && digits.length > 8) {
    const local = digits.slice(2);
    variants.add(local);
    variants.add(`+${local}`);
  }
  return [...variants].filter(Boolean);
}

/**
 * app/services/conversation.py :: storage_key
 *
 * What the portal stores in `customer_number`. Bare digits, and UNIQUE —
 * writing E.164 there is what split a conversation in two once already.
 */
export function storageKey(phone: string): string {
  return digitsOnly(phone);
}

/** A number is only usable once it has enough digits to identify anyone. */
export function isUsablePhone(phone: string | null | undefined): boolean {
  return digitsOnly(phone).length >= 8;
}

/** Display helper: '+6591234567' -> '+65 9123 4567' for Singapore numbers. */
export function prettyPhone(phone: string): string {
  const digits = digitsOnly(phone);
  if (digits.startsWith('65') && digits.length === 10) {
    return `+65 ${digits.slice(2, 6)} ${digits.slice(6)}`;
  }
  return phone;
}
