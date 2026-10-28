/**
 * The service / audience / nationality a watcher proposal is filed under
 * (2026-10-28). PURE: no database, network or environment code, so
 * scripts/selfcheck.mjs runs every rule offline.
 *
 * Why: until now every NEW proposal landed as general / all / all (the
 * pipeline passed nothing), so the MWO Singapore pages (contract verification,
 * the OEC, Balik-Manggagawa) went live as general entries, and one of them
 * answered a Filipino NEW-HIRE documents question with contract-verification
 * documents. The bot filters its search on these three tags, so a wrong tag is
 * a wrong answer to somebody else's question.
 *
 * HOW A TAG IS CHOSEN, per tag:
 *   1. the start: an EDIT keeps the live entry's tag; a NEW entry starts at
 *      general / all / all;
 *   2. the drafting model's tag, when it is one of the allowed values - for a
 *      NEW entry always; for an EDIT only where the live tag is the generic
 *      one (general / all), so the model may narrow a generic entry but never
 *      re-file a specific one on its own say-so;
 *   3. the evidence rules below, which OVERRIDE both - but only where the
 *      evidence is unambiguous: one answer, never two. The draft's own text
 *      (question, answer, source passage) is read first; the page's label and
 *      address only when the draft's text says nothing.
 * Every step leaves a "why" line, and the review page shows them, so a
 * reviewer can see where each tag came from and change it before approving.
 */

export const TAG_SERVICES = [
  'general', 'new_hiring', 'direct_hiring', 'transfer', 'replacement', 'renewal',
  'home_leave', 'passport_renewal', 'fee_enquiry', 'salary_enquiry',
] as const;
export const TAG_AUDIENCES = ['all', 'employer', 'candidate'] as const;
export const TAG_NATIONALITIES = ['all', 'PH', 'ID', 'MM'] as const;

export type Tags = { service: string; audience: string; nationality: string };
export type TagKey = keyof Tags;
export type TagProposal = Tags & { why: string[] };

export const DEFAULT_TAGS: Tags = { service: 'general', audience: 'all', nationality: 'all' };

const ALLOWED: Record<TagKey, readonly string[]> = {
  service: TAG_SERVICES,
  audience: TAG_AUDIENCES,
  nationality: TAG_NATIONALITIES,
};

const NAMES: Record<TagKey, string> = { service: 'Service', audience: 'Audience', nationality: 'Nationality' };

/** Is this one of the allowed values for the tag (and, for a service, one the knowledge base uses)? */
export function validTag(key: TagKey, value: unknown, services?: readonly string[]): value is string {
  if (typeof value !== 'string' || !ALLOWED[key].includes(value)) return false;
  return key !== 'service' || !services || services.includes(value) || value === 'general';
}

/** One rule: a value, the words that show it, and what to say when it fires. */
type Rule = { value: string; re: RegExp; says: string };

const PH_WORDS = /\b(MWO|OFWs?|POEA|DMW|OEC|Balik[- ]?Manggagawa|Philippines?|Philippine|Filipinos?|Filipina)\b|mwosingapore|philippine-embassy|dmw\.gov\.ph|dfa\.gov\.ph/i;

const NATIONALITY_RULES: Rule[] = [
  { value: 'PH', re: PH_WORDS, says: 'it names the Philippine labour office, OFWs, the OEC or the Philippines' },
  { value: 'ID', re: /\b(KBRI|Indonesians?|Indonesia|WNI)\b|kemlu\.go\.id/i, says: 'it names the Indonesian embassy (KBRI) or Indonesia' },
  { value: 'MM', re: /\b(Myanmar|Burmese)\b|myanmarembassy/i, says: 'it names Myanmar' },
];

const SERVICE_RULES: Rule[] = [
  {
    value: 'home_leave',
    re: /\b(Balik[- ]?Manggagawa|OEC|Overseas Employment Certificate|returning to the Philippines|returning workers?|home leave|on vacation)\b/i,
    says: 'Balik-Manggagawa / the OEC / returning home is a home-leave matter',
  },
  {
    value: 'transfer',
    re: /\bcontract verification\b(?=[\s\S]*\b(new employer|chang\w* (of )?employers?|transfer\w*)\b)|\b(new employer|chang\w* (of )?employers?)\b(?=[\s\S]*\bcontract verification\b)/i,
    says: 'contract verification for a new or changed employer is a transfer step',
  },
];

/** An embassy page about passports: passport renewal. */
const PASSPORT = /\bpass(port|ports|por)\b|\bpaspor\b/i;

const CANDIDATE_WORDS = /\b(OFWs?|the worker|returning workers?|migrant workers?|Balik[- ]?Manggagawa|your (new |current |former )?employer|your employment contract)\b/i;
// Plural "employers must": "her new employer must sign" is about the worker's employer, not to one.
const EMPLOYER_WORDS = /\b(your (helper|maid|FDW|MDW|domestic (helper|worker))|employers (must|need|should|have to|pay|are required)|(you )?as (an|the) employer)\b/i;

/** The single value the rules point to in this text, or null (no evidence, or two answers). */
function oneOf(rules: Rule[], text: string): { value: string; says: string } | 'ambiguous' | null {
  const hits = rules.filter((r) => r.re.test(text));
  const values = [...new Set(hits.map((h) => h.value))];
  if (values.length === 1) return { value: values[0], says: hits[0].says };
  return values.length > 1 ? 'ambiguous' : null;
}

export type TagInput = {
  sourceLabel: string;
  sourceUrl: string;
  /** The watched page's category (015): government_rule | process | embassy | other. */
  sourceCategory?: string | null;
  question: string;
  answer: string;
  sourcePassage: string;
};

/** What the rules alone say, per tag: a value with its reason, or nothing. The review page shows these too. */
export function tagEvidence(p: TagInput): Partial<Record<TagKey, { value: string; why: string }>> {
  const draft = `${p.question}\n${p.answer}\n${p.sourcePassage}`;
  const qa = `${p.question}\n${p.answer}`;
  const page = `${p.sourceLabel}\n${p.sourceUrl}`;
  const out: Partial<Record<TagKey, { value: string; why: string }>> = {};
  // Read the draft first, then only its question and answer if the passage muddies it, then the page's own name.
  const decide = (rules: Rule[]): { value: string; says: string; where: string } | null => {
    const d = oneOf(rules, draft);
    if (d && d !== 'ambiguous') return { ...d, where: 'the proposal' };
    if (d === 'ambiguous') {
      const q = oneOf(rules, qa);
      return q && q !== 'ambiguous' ? { ...q, where: 'the question and answer' } : null;
    }
    const s = oneOf(rules, page);
    return s && s !== 'ambiguous' ? { ...s, where: "the page's name and address" } : null;
  };

  const nat = decide(NATIONALITY_RULES);
  if (nat) out.nationality = { value: nat.value, why: `${nat.where}: ${nat.says}` };

  // An embassy page ABOUT passports (its name, or the question asks about one) - not every embassy page that mentions one.
  const embassy = p.sourceCategory === 'embassy' || /embass|kbri|kemlu/i.test(page);
  const passportPage = embassy && PASSPORT.test(page);
  const embassyPassport = embassy && (passportPage || PASSPORT.test(p.question));
  const svc = decide(SERVICE_RULES);
  if (svc && passportPage && svc.value !== 'passport_renewal') {
    // Two answers (a passport page that also talks about going home or a new employer): no override.
  } else if (svc) {
    out.service = { value: svc.value, why: `${svc.where}: ${svc.says}` };
  } else if (embassyPassport) {
    out.service = { value: 'passport_renewal', why: 'an embassy page about passports: passport renewal' };
  }

  const cand = CANDIDATE_WORDS.test(draft) || (embassyPassport && /\byour passport\b/i.test(qa));
  const emp = EMPLOYER_WORDS.test(draft);
  if (cand && !emp) {
    out.audience = { value: 'candidate', why: 'the proposal is addressed to the worker (an OFW, the worker, "your employer", "your passport" on an embassy page)' };
  } else if (emp && !cand) {
    out.audience = { value: 'employer', why: 'the proposal is addressed to the employer ("your helper", "employers must")' };
  } else if (!cand && !emp && /\b(MWO|OFWs?|POEA|DMW|Balik[- ]?Manggagawa)\b|mwosingapore/i.test(page)) {
    out.audience = { value: 'candidate', why: 'the page\'s name and address: the Philippine labour office serves the worker' };
  }
  return out;
}

/**
 * The tags to propose, with why. `live` = the entry an EDIT changes (null for a
 * new entry); `modelTags` = what the drafting model said, unchecked; `services`
 * = the services the knowledge base uses now (the database refuses any other).
 */
export function proposeTags(p: TagInput & {
  modelTags?: Partial<Record<TagKey, unknown>> | null;
  live?: Tags | null;
  services?: readonly string[];
}): TagProposal {
  const evidence = tagEvidence(p);
  const out: Tags = { ...(p.live ?? DEFAULT_TAGS) };
  const why: string[] = [];
  for (const key of ['service', 'audience', 'nationality'] as TagKey[]) {
    const name = NAMES[key];
    const generic = DEFAULT_TAGS[key];
    const model = p.modelTags?.[key];
    if (model !== undefined && model !== null && model !== '' && !validTag(key, model, p.services)) {
      why.push(`${name}: the drafting model's "${String(model).slice(0, 40)}" is not a value we use; ignored`);
    } else if (validTag(key, model, p.services) && model !== out[key]) {
      if (!p.live) {
        out[key] = model;
        why.push(`${name} ${model}: proposed by the drafting model`);
      } else if (p.live[key] === generic) {
        out[key] = model;
        why.push(`${name} ${model}: proposed by the drafting model (the live entry is ${generic})`);
      } else {
        why.push(`${name}: the drafting model said ${model}; the live entry's ${p.live[key]} is kept`);
      }
    }
    const e = evidence[key];
    if (e && validTag(key, e.value, p.services)) {
      if (e.value !== out[key]) why.push(`${name} ${e.value}: ${e.why}${p.live && p.live[key] !== e.value ? ` (the live entry is ${p.live[key]})` : ''}`);
      else why.push(`${name} ${e.value}: ${e.why}`);
      out[key] = e.value;
    }
    if (key === 'service' && !validTag('service', out.service, p.services) && out.service !== p.live?.service) {
      why.push(`Service: ${out.service} is not used in the knowledge base yet; ${p.live?.service ?? generic} instead`);
      out.service = p.live?.service ?? generic;
    }
    if (!why.some((w) => w.startsWith(name))) {
      why.push(p.live ? `${name} ${out[key]}: kept from the live entry` : `${name} ${out[key]}: no evidence for anything narrower`);
    }
  }
  return { ...out, why };
}

/** The tags that differ between the live entry and a proposal (an edit), in the order shown. */
export function changedTags(live: Tags | null, proposed: Tags): TagKey[] {
  if (!live) return [];
  return (['service', 'audience', 'nationality'] as TagKey[]).filter((k) => live[k] !== proposed[k]);
}

/** The prompt line asking the drafting model for the three tags (both drafting calls). */
export const TAG_PROMPT = [
  '- For every draft give "service", "audience" and "nationality" - who the entry is for, from the page itself:',
  `  service one of ${TAG_SERVICES.join(', ')} (general when it is not about one service; a returning worker's OEC or Balik-Manggagawa is home_leave; contract verification for a new employer is transfer; an embassy passport page is passport_renewal);`,
  `  audience one of ${TAG_AUDIENCES.join(', ')} (candidate = the helper / worker herself, employer = the household employing her);`,
  `  nationality one of ${TAG_NATIONALITIES.join(', ')} (PH for a Philippine office or OFW page, ID for the Indonesian embassy, MM for Myanmar; all when it applies to every helper).`,
].join('\n');

/** The model's three tags on one draft, as given (checked later by proposeTags). */
export function modelTagsOf(d: Record<string, unknown>): Partial<Record<TagKey, unknown>> | undefined {
  const t: Partial<Record<TagKey, unknown>> = {};
  for (const k of ['service', 'audience', 'nationality'] as TagKey[]) if (d[k] !== undefined) t[k] = typeof d[k] === 'string' ? (d[k] as string).trim() : d[k];
  return Object.keys(t).length ? t : undefined;
}
