/**
 * Is a drafted entry worth a reviewer's time? PURE.
 *
 * Production, 2026-10-15: the MOM levy index page drafted four "new entry"
 * proposals that only pointed elsewhere - "The MOM page provides information
 * on paying levy, levy concession and levy waiver." A client cannot act on
 * that, and the assistant would only repeat it. A draft is now dropped when:
 *   - its question asks where to find information ("Where can I find
 *     information about...", "What does the MOM page say..."), or
 *   - once the sentences that only point elsewhere ("The MOM page provides
 *     information on...", "visit this page", "for more information...") are
 *     set aside, nothing is left that a client could act on: an amount, a
 *     rule, a deadline, an eligibility condition or a step.
 * The same words are in the drafting prompt (lib/watcher/drafting.ts); this is
 * the check that does not depend on the model listening.
 */

const POINTER_SENTENCE: RegExp[] = [
  /\bwhere (?:can|do|should|could) (?:i|you|we|employers?|clients?) (?:find|get|look|read|learn)\b/i,
  /\b(?:page|website|site|link|portal|webpage|web page|section)\b[^.?!]{0,80}\b(?:provides?|has|contains?|lists?|explains?|gives?|covers?|offers?|includes?|describes?|sets out|outlines?)\b/i,
  /\b(?:visit|see|refer to|check|go to|read|consult|look at|open)\s+(?:the\s+|this\s+|that\s+|mom'?s?\s+|our\s+|their\s+)?(?:official\s+)?(?:mom\s+)?(?:page|website|site|link|portal|webpage)\b/i,
  /\b(?:for|find) (?:more|further|full|detailed) (?:information|details|guidance)\b/i,
  /\bmore (?:information|details) (?:is|are|can be) (?:available|found)\b/i,
  /\bfind out (?:more|about|how|if|whether|what)\b/i,
];

const META_QUESTION = /\bwhere (?:can|do|should|could) (?:i|you|we|employers?)\b[^?]*\b(?:find|get|read|learn|look)\b|\bwhat (?:does|is on) (?:the|this|that) (?:mom |official )?(?:page|website|site)\b|\b(?:page|website|site) (?:about|on|for)\b/i;

/** The five kinds of fact a client can act on. */
export const FACT_KINDS: Record<'amount' | 'deadline' | 'rule' | 'eligibility' | 'step', RegExp> = {
  amount: /(?:S\$|SGD\s?|\$)\s?\d|\b\d[\d,.]*\s?(?:%|per ?cent)|\b\d+(?:\.\d+)?\s*(?:working days?|days?|weeks?|months?|years?|hours?)\b/i,
  deadline: /\b(?:by|on) the \d{1,2}(?:st|nd|rd|th)\b|\bwithin \d|\bdeadline\b|\bdue (?:on|by|date)\b|\b(?:before|after|from) (?:the )?\d|\b\d{1,2} ?(?:st|nd|rd|th) (?:day|of)\b/i,
  rule: /\b(?:must(?: not)?|required|requires?|have to|has to|needs? to|not allowed|cannot|can't|may not|may only|only if|entitled|mandatory|prohibited|compulsory|will be (?:revoked|charged|cancell?ed|waived|adjusted)|is waived|penalt(?:y|ies)|is (?:not )?(?:allowed|permitted))\b/i,
  // A condition, not just the word: "you may qualify for a concession" says nothing a client can check.
  eligibility: /\b(?:households? with|families with|persons? with|employers? (?:who|with)|only (?:for|if|when)|applies (?:only )?to|(?:eligible|qualif(?:y|ies)) (?:if|when|only)|if (?:you|your|she|he|the|a) )/i,
  step: /\b(?:apply (?:for|through|online|via|at)|log ?in|submit|pay (?:by|using|via|through|with)|register|book an?|bring|fill in|sign (?:the|a)|send (?:us|the)|giro|paynow)\b/i,
};

const sentences = (text: string) => text.split(/(?<=[.!?])\s+/).map((s) => s.trim()).filter(Boolean);

export function isPointerSentence(s: string): boolean {
  return POINTER_SENTENCE.some((re) => re.test(s));
}

/** The kinds of actionable fact in a text (after any pointer sentences are set aside). */
export function actionableKinds(text: string): string[] {
  const kept = sentences(text).filter((s) => !isPointerSentence(s)).join(' ');
  return Object.entries(FACT_KINDS).filter(([, re]) => re.test(kept)).map(([k]) => k);
}

/** Why a draft is not worth proposing, or null when it carries a fact a client can act on. */
export function lowValueReason(question: string, answer: string): string | null {
  if (META_QUESTION.test(question)) return 'a draft that only points elsewhere (its question asks where to find information)';
  const all = sentences(answer);
  const kept = all.filter((s) => !isPointerSentence(s));
  if (!kept.length) return 'a draft that only points elsewhere';
  if (!actionableKinds(kept.join(' ')).length) {
    return kept.length < all.length ? 'a draft that only points elsewhere' : 'a draft with no fact a client could act on (an amount, a rule, a deadline, an eligibility condition or a step)';
  }
  return null;
}
