/**
 * Facts the live rows state that a new version of the document would drop.
 *
 *   findLostFacts(liveRowsText, newChunksText) -> LostFact[]
 *
 * Pure: no database, no network. kb-admin runs it before an approver
 * publishes a re-uploaded document: the live rows may have been corrected
 * after the original import (the agency's phone number, the salary floor, the
 * transfer timeline), and a source file that predates those corrections would
 * silently put the old figures back.
 *
 * A fact is one of:
 *   phone     a phone number (+65 6534 2277, 6534 2277, 1800-255-0000);
 *   money     an amount with a currency mark ($1,428, S$650, $5,000);
 *   span      a duration ("1 to 2 weeks", "2-3 weeks", "3 working days");
 *   percent   a percentage (50%);
 *   number    any other number.
 * Each is compared in a normal form, so "1 to 2 weeks" and "1-2 weeks", or
 * "S$650" and "$650", are the same fact. A fact counts as kept if the new
 * chunks state it anywhere; a bare number counts as kept if it appears in the
 * new chunks in any form (inside an amount, a span, or alone).
 */

export type FactKind = 'phone' | 'money' | 'span' | 'percent' | 'number';

export type LostFact = {
  kind: FactKind;
  /** Normal form: digits for a phone, "$650", "1-2 weeks", "50%", "2026". */
  fact: string;
  /** How many live rows state it. */
  liveRows: number;
  /** The fact as first written in the live rows, with a little context. */
  example: string;
};

const PHONE = /(?:\+\d{1,3}[\s-]?)?\b(?:1800[\s-]?\d{3}[\s-]?\d{4}|[3689]\d{3}[\s-]?\d{4})\b/g;
const UNIT = '(working days?|business days?|days?|weeks?|months?|years?|hours?)';
const SPAN = new RegExp(`\\b(\\d+(?:\\.\\d+)?)\\s*(?:-|–|—|to)\\s*(\\d+(?:\\.\\d+)?)\\s*${UNIT}\\b|\\b(\\d+(?:\\.\\d+)?)\\s*${UNIT}\\b`, 'gi');
const MONEY = /(?:S\$|SGD\s?|\$)\s?\d[\d,]*(?:\.\d+)?/gi;
const PERCENT = /\b\d+(?:\.\d+)?\s?%/g;
const NUMBER = /\d[\d,]*(?:\.\d+)?/g;

const plural = (u: string) => {
  const unit = u.toLowerCase().replace(/\s+/g, ' ');
  return unit.endsWith('s') ? unit : `${unit}s`;
};
const digits = (s: string) => s.replace(/[^\d.]/g, '').replace(/\.$/, '');

type Found = { kind: FactKind; fact: string; raw: string; at: number };

/** Every fact in one text, each span of text claimed by one kind only. */
export function extractFacts(text: string): Found[] {
  const out: Found[] = [];
  // [cite:N] is a research-tool marker, not a fact (the chunker removes it).
  let rest = text.replace(/\[cite:\d+\]/g, (s) => ' '.repeat(s.length));
  const take = (re: RegExp, kind: FactKind, normal: (m: RegExpMatchArray) => string | null) => {
    for (const m of rest.matchAll(re)) {
      const fact = normal(m);
      if (fact) out.push({ kind, fact, raw: m[0], at: m.index ?? 0 });
    }
    // Blank what was claimed (same length, so positions stay true).
    rest = rest.replace(re, (s) => ' '.repeat(s.length));
  };
  take(PHONE, 'phone', (m) => {
    let d = m[0].replace(/\D/g, '');
    if (d.length === 10 && d.startsWith('65')) d = d.slice(2);
    return d.length >= 8 ? d : null;
  });
  take(SPAN, 'span', (m) => (m[1] ? `${m[1]}-${m[2]} ${plural(m[3])}` : `${m[4]} ${plural(m[5])}`));
  take(MONEY, 'money', (m) => `$${digits(m[0])}`);
  take(PERCENT, 'percent', (m) => `${digits(m[0])}%`);
  take(NUMBER, 'number', (m) => digits(m[0]) || null);
  return out;
}

/** Every number written anywhere in a text, in any form - what a bare number is checked against. */
function allNumbers(text: string): Set<string> {
  return new Set([...text.matchAll(NUMBER)].map((m) => digits(m[0])).filter(Boolean));
}

export function findLostFacts(liveRowsText: string[], newChunksText: string[]): LostFact[] {
  const kept = new Set<string>();
  const keptNumbers = new Set<string>();
  for (const t of newChunksText) {
    for (const f of extractFacts(t)) kept.add(`${f.kind}|${f.fact}`);
    for (const n of allNumbers(t)) keptNumbers.add(n);
  }
  const lost = new Map<string, LostFact>();
  for (const row of liveRowsText) {
    const seen = new Set<string>();
    for (const f of extractFacts(row)) {
      const key = `${f.kind}|${f.fact}`;
      const isKept = f.kind === 'number' ? keptNumbers.has(f.fact) : kept.has(key);
      if (isKept || seen.has(key)) continue;
      seen.add(key);
      const prev = lost.get(key);
      if (prev) prev.liveRows += 1;
      else {
        const from = Math.max(0, f.at - 40);
        const example = row.slice(from, f.at + f.raw.length + 40).replace(/\s+/g, ' ').trim();
        lost.set(key, { kind: f.kind, fact: f.fact, liveRows: 1, example });
      }
    }
  }
  const order: FactKind[] = ['phone', 'money', 'span', 'percent', 'number'];
  return [...lost.values()].sort((a, b) => order.indexOf(a.kind) - order.indexOf(b.kind) || b.liveRows - a.liveRows);
}
