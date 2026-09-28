'use client';

import Link from 'next/link';
import { useState } from 'react';

type Source = { id: string; service_type: string; nationality: string; question: string; similarity: number };
type Turn = {
  message: string;
  reply: string;
  intent: string | null;
  service_type: string | null;
  needs_handover: boolean;
  rag_best_score: number;
  sources: Source[];
};
type Result = { turns: Turn[]; refused_writes: { method: string; target: string }[]; rules_source: string | null };

type Band = { tone: 'good' | 'warn' | 'bad'; verdict: string };

/** How much to trust an answer, from how closely the best entry matched. */
function band(score: number): Band {
  if (score >= 0.5) return { tone: 'good', verdict: 'Strong match — answer is reliable' };
  if (score >= 0.4) return { tone: 'warn', verdict: 'Moderate match — check the answer' };
  return { tone: 'bad', verdict: 'Weak match — the chatbot would normally hand this to a person' };
}

const TONE_COLOUR = { good: 'var(--good)', warn: 'var(--warn)', bad: 'var(--bad)' } as const;

export function TestForm() {
  const [question, setQuestion] = useState('');
  const [asked, setAsked] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<Result | null>(null);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    const text = question.trim();
    if (!text) return;
    setBusy(true);
    setError('');
    setResult(null);
    setAsked(text);
    try {
      const res = await fetch('/api/preview', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ messages: [text], contact_type: 'unknown' }),
      });
      if (res.status === 401) {
        window.location.href = '/login?e=bad-session';
        return;
      }
      const data = await res.json().catch(() => ({}));
      if (!res.ok) setError(data.error ?? `The chatbot could not answer (error ${res.status}). Please try again.`);
      else setResult(data as Result);
    } catch {
      setError('Could not reach the chatbot. Please check your connection and try again.');
    } finally {
      setBusy(false);
    }
  }

  function clear() {
    setQuestion('');
    setAsked('');
    setResult(null);
    setError('');
  }

  const turn = result?.turns[result.turns.length - 1];

  return (
    <>
      <form onSubmit={run} className="card card-pad">
        <label htmlFor="question" className="field-label mb-1.5 block">
          Question — as a client would type it
        </label>
        <div className="flex gap-3">
          <input
            id="question"
            value={question}
            maxLength={2000}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="e.g. How much does it cost to hire an Indonesian helper?"
            className="input input-lg flex-1"
            autoComplete="off"
          />
          <button type="submit" disabled={busy || !question.trim()} className="btn btn-primary h-12">
            {busy ? 'Asking the chatbot…' : 'Ask the chatbot'}
          </button>
          {(result || error) && !busy ? (
            <button type="button" onClick={clear} className="btn btn-secondary h-12">
              Clear
            </button>
          ) : null}
        </div>
      </form>

      {busy ? (
        <div className="card card-pad text-[14px] text-muted" role="status">
          Finding the answer… this usually takes a few seconds.
        </div>
      ) : null}

      {error ? (
        <div className="notice notice-red" role="alert">
          {error}
        </div>
      ) : null}

      {turn ? (
        <section className="card card-pad" aria-label="The chatbot's answer">
          <p className="text-[12px] italic text-muted">“{asked}”</p>
          <p className="mt-3 whitespace-pre-wrap text-[15px] leading-[1.7]">
            {turn.reply || 'The chatbot did not reply to this message.'}
          </p>
          {turn.needs_handover ? (
            <p className="mt-3 text-[13px] text-muted">The chatbot would also pass this conversation to a person.</p>
          ) : null}

          <h2 className="field-label mb-2 mt-6">Sources the chatbot used</h2>
          {turn.sources.length ? (
            <>
              {(() => {
                const b = band(turn.rag_best_score);
                return (
                  <div className={`strip pill-${b.tone}`}>
                    <span className="mono">{turn.rag_best_score.toFixed(2)}</span> · {b.verdict}
                  </div>
                );
              })()}
              <ul className="mt-2">
                {turn.sources.map((s) => {
                  const b = band(s.similarity);
                  return (
                    <li
                      key={s.id}
                      className="flex items-baseline gap-3 border-t py-2.5 text-[13px] first:border-t-0"
                      style={{ borderColor: 'var(--divider)' }}
                    >
                      <span className="mono w-11 flex-none font-medium" style={{ color: TONE_COLOUR[b.tone] }}>
                        {s.similarity.toFixed(2)}
                      </span>
                      <Link href={`/rows/${s.id}`} className="link min-w-0 flex-1 truncate" title={s.question || undefined}>
                        {s.question || 'Untitled passage'}
                      </Link>
                      <span className="mono flex-none text-[12px] text-muted">
                        {s.service_type}
                        {s.nationality && s.nationality !== 'all' ? ` · ${s.nationality}` : ''}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </>
          ) : (
            <p className="text-[13px] text-muted">
              No knowledge base entries were used for this answer
              {turn.rag_best_score < 0.4 ? ', so the chatbot would normally hand this to a person' : ''}.
            </p>
          )}
          <p className="mt-4 text-[12px] text-muted">Test mode: nothing was saved and no message was sent.</p>
        </section>
      ) : null}
    </>
  );
}
