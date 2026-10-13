"""Is this message mainly in a language other than English? Cheap and
deterministic: no model call and no paid API.

Phase 3 Step 3 (2026-10-13). The deterministic safety nets - ASSAULT_PATTERNS
in intent_classifier, and through it the out-of-hours emergency_override in the
webhook - are English-only (CLAUDE.md section 9.2), while Claire answers in any
language. A helper writing "Majikan saya memukul saya" (Bahasa: my employer hit
me) matched nothing, so whether she was escalated rested on the classifier
alone. This test decides when the harm check runs anyway.

Two signals:

* script - letters in a non-Latin script (Burmese, Chinese, Tamil, Devanagari,
  Thai...). A message that is mostly those is not English;
* function words - in Latin script, the small words every sentence carries.
  A message whose Bahasa / Malay / Tagalog / romanised-Hindi function words
  outnumber its English ones is not mainly English. A message with no marker
  of either kind ("renewal fee", "ok") is treated as English: it costs nothing,
  and the classifier still reads it.

It errs towards "not English" only where a marker says so, so an English
message pays for a harm check only when it carries a foreign function word.
"""

from __future__ import annotations

import re

# Letters in a script that is not Latin. Ranges, not a library: Burmese,
# Thai, Lao, Khmer, the Indic scripts, Sinhala, Arabic, Hebrew, CJK, Hangul,
# Japanese kana, Cyrillic and Greek.
_NON_LATIN = re.compile(
    "[Ͱ-ϿЀ-ӿ֐-׿؀-ۿऀ-෿"
    "฀-໿က-႟ក-៿぀-ヿ㐀-䶿"
    "一-鿿가-힯]"
)
_LATIN_LETTER = re.compile(r"[A-Za-zÀ-ɏ]")
_WORD = re.compile(r"[A-Za-zÀ-ɏ']+")

# Above this share of letters in another script, the message is not English.
_NON_LATIN_SHARE = 0.3

# English function words: a real English sentence almost never has none.
_ENGLISH = frozenset("""
i me my mine we us our you your he him his she her it its they them their
the a an is am are was were be been being do does did have has had will would
can could should shall must not no yes and or but if so to of in on at for
with from by about this that these those what when where why how who which
please thank thanks hi hello okay ok any there here than then also just very
""".split())

# Function words of the languages our clients and helpers write in. Only words
# that are NOT also English words ("may", "at", "a", "main", "to" are left out).
_FOREIGN = frozenset("""
saya aku kamu anda dia kami kita mereka tidak tak bukan belum sudah sedang
akan yang dan atau dengan untuk dari ke di pada ini itu ada apa siapa kenapa
mengapa bagaimana berapa bisa mau ingin tolong terima kasih selamat pagi siang
sore malam juga lagi sangat saja kalau jika karena tapi tetapi sama nya
ako ikaw siya kami tayo sila ko mo niya namin natin ng ang mga sa hindi oo
po opo ba naman lang din rin kasi pero kung paano bakit ano sino saan kailan
ilan magkano salamat maganda kumusta
mera meri mere mujhe mujhko hum hamara aap tum tera hai hain tha thi nahi
nahin kya kaise kyun kyon kab kahan aur lekin bhi
""".split())


def mainly_non_english(text: str) -> bool:
    """Whether the message is mainly in a language other than English."""
    body = text or ""
    non_latin = len(_NON_LATIN.findall(body))
    latin = len(_LATIN_LETTER.findall(body))
    if non_latin and non_latin >= _NON_LATIN_SHARE * (non_latin + latin):
        return True
    words = [w.lower().strip("'") for w in _WORD.findall(body)]
    foreign = sum(w in _FOREIGN for w in words)
    english = sum(w in _ENGLISH for w in words)
    return foreign >= 1 and foreign > english
