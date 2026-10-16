/**
 * The language a watched page is written in (2026-10-16). PURE, no library:
 * the same rules as the bot's own detector (app/graph/language.py
 * reply_language) - the dominant script first (Burmese, Tamil, Hindi,
 * Chinese), then the function words of Bahasa Indonesia, Malay and Tagalog
 * against English ones.
 *
 * Used twice: the drafting prompt is told the page's language (keep the quote
 * in that language, write the proposal in English), and the review page shows
 * "Translated from <language>" on a proposal whose source passage is not in
 * English. Nothing about a page's language is stored; it is read off the text.
 * See NON_ENGLISH_PAGES.
 */

/** Switch: false = every page is treated as English, as before 2026-10-16. */
export const NON_ENGLISH_PAGES = true;

export type PageLanguage = { code: 'en' | 'id' | 'ms' | 'tl' | 'my' | 'ta' | 'hi' | 'zh'; name: string; english: boolean };

const NAMES: Record<PageLanguage['code'], string> = {
  en: 'English', id: 'Indonesian', ms: 'Malay', tl: 'Tagalog', my: 'Burmese', ta: 'Tamil', hi: 'Hindi', zh: 'Chinese',
};

const LATIN_LETTER = /[A-Za-zÀ-ɏ]/g;
const WORD = /[A-Za-zÀ-ɏ']+/g;
const NON_LATIN_SHARE = 0.3;

const set = (s: string) => new Set(s.split(/\s+/).filter(Boolean));
const ENGLISH = set(`i me my mine we us our you your he him his she her it its they them their
the a an is am are was were be been being do does did have has had will would
can could should shall must not no yes and or but if so to of in on at for
with from by about this that these those what when where why how who which
please thank thanks hi hello okay ok any there here than then also just very`);
const BAHASA = set(`saya aku kamu anda dia kami kita mereka tidak tak bukan belum sudah sedang
akan yang dan atau dengan untuk dari ke di pada ini itu ada apa siapa kenapa
mengapa bagaimana berapa bisa mau ingin tolong terima kasih selamat pagi siang
sore malam juga lagi sangat saja kalau jika karena tapi tetapi sama nya
dalam oleh bagi serta harus wajib dapat telah tersebut melalui sesuai setiap`);
const TAGALOG = set(`ako ikaw siya kami tayo sila ko mo niya namin natin ng ang mga sa hindi oo
po opo ba naman lang din rin kasi pero kung paano bakit ano sino saan kailan
ilan magkano salamat maganda kumusta`);
const MALAY_ONLY = set('sila polis nombor kerana ialah sahaja awak mahu pasukan ejen khabar sebarang');
const INDONESIAN_ONLY = set('silakan polisi nomor karena adalah saja kamu mau tim agen kabar mohon');

const SCRIPTS: [PageLanguage['code'], RegExp][] = [
  ['my', /[က-႟]/g],
  ['ta', /[஀-௿]/g],
  ['hi', /[ऀ-ॿ]/g],
  ['zh', /[㐀-䶿一-鿿]/g],
];

const lang = (code: PageLanguage['code']): PageLanguage => ({ code, name: NAMES[code], english: code === 'en' });

/** The language of a page's (or a passage's) text. Anything it cannot place is English. */
export function detectLanguage(text: string): PageLanguage {
  const body = text ?? '';
  if (!NON_ENGLISH_PAGES) return lang('en');
  const latin = (body.match(LATIN_LETTER) ?? []).length;
  let best: [number, PageLanguage['code']] = [0, 'en'];
  for (const [code, re] of SCRIPTS) {
    const n = (body.match(re) ?? []).length;
    if (n > best[0]) best = [n, code];
  }
  if (best[0] && best[0] >= NON_LATIN_SHARE * (best[0] + latin)) return lang(best[1]);
  const words = (body.match(WORD) ?? []).map((w) => w.toLowerCase().replace(/^'+|'+$/g, ''));
  const count = (s: Set<string>) => words.reduce((n, w) => n + (s.has(w) ? 1 : 0), 0);
  const english = count(ENGLISH);
  const bahasa = count(BAHASA);
  const tagalog = count(TAGALOG);
  const foreign = Math.max(bahasa, tagalog);
  if (!foreign || foreign <= english) return lang('en');
  if (tagalog > bahasa) return lang('tl');
  return count(MALAY_ONLY) > count(INDONESIAN_ONLY) ? lang('ms') : lang('id');
}

/** The prompt line for a page not in English, or '' for an English page. */
export function languageInstruction(language: PageLanguage): string {
  if (language.english) return '';
  return [
    `- The page is in ${language.name}. Copy every source_passage EXACTLY in ${language.name}, as it appears on the page - never translate a quote.`,
    `- Write every question, answer, added sentence and replacement in plain English, translated faithfully from the ${language.name} passages: keep every figure, date and condition exactly as the page states it.`,
  ].join('\n');
}
