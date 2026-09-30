'use client';

import Link from 'next/link';
import { useState } from 'react';
import { label } from '@/components/labels';

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

/** Who the chatbot should take the sender to be. Values are the ones /api/preview accepts. */
const AUDIENCES = [
  { value: 'unknown', label: 'New number' },
  { value: 'employer', label: 'Employer' },
  { value: 'candidate', label: 'Helper / candidate' },
];

const MAX_MESSAGES = 10;

function TurnResult({ turn }: { turn: Turn }) {
  const b = band(turn.rag_best_score);
  return (
    <div className="border-t pt-5 first:border-t-0 first:pt-0" style={{ borderColor: 'var(--divider)' }}>
      <p className="text-[12px] italic text-muted">“{turn.message}”</p>
      <p className="mt-3 whitespace-pre-wrap text-[15px] leading-[1.7]">
        {turn.reply || 'The chatbot did not reply to this message.'}
      </p>
      {turn.needs_handover ? (
        <p className="mt-3 text-[13px] text-muted">The chatbot would also pass this conversation to a person.</p>
      ) : null}

      <h3 className="field-label mb-2 mt-6">Sources the chatbot used</h3>
      {turn.sources.length ? (
        <>
          <div className={`strip pill-${b.tone}`}>
            <span className="mono">{turn.rag_best_score.toFixed(2)}</span> · {b.verdict}
          </div>
          <ul className="mt-2">
            {turn.sources.map((s) => {
              const sb = band(s.similarity);
              return (
                <li
                  key={s.id}
                  className="flex items-baseline gap-3 border-t py-2.5 text-[13px] first:border-t-0"
                  style={{ borderColor: 'var(--divider)' }}
                >
                  <span className="mono w-11 flex-none font-medium" style={{ color: TONE_COLOUR[sb.tone] }}>
                    {s.similarity.toFixed(2)}
                  </span>
                  <Link href={`/rows/${s.id}`} className="link min-w-0 flex-1 truncate" title={s.question || undefined}>
                    {s.question || 'Untitled passage'}
                  </Link>
                  <span className="flex-none text-[12px] text-muted">
                    {label('service', s.service_type)}
                    {s.nationality && s.nationality !== 'all' ? ` · ${label('nationality', s.nationality)}` : ''}
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
    </div>
  );
}

export function TestForm() {
  const [messages, setMessages] = useState<string[]>(['']);
  const [audience, setAudience] = useState('unknown');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<Result | null>(null);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError('');
    setResult(null);
    try {
      const res = await fetch('/api/preview', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ messages: messages.filter((m) => m.trim()), contact_type: audience }),
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
    setMessages(['']);
    setResult(null);
    setError('');
  }

  return (
    <>
      <form onSubmit={run} className="card card-pad">
        <div className="space-y-4">
          {messages.map((m, i) => (
            <div key={i}>
              <label htmlFor={`message-${i}`} className="field-label mb-1.5 block">
                {i === 0 ? 'Question — as a client would type it' : `Follow-up message ${i + 1}`}
              </label>
              <input
                id={`message-${i}`}
                value={m}
                maxLength={2000}
                onChange={(e) => setMessages(messages.map((x, j) => (j === i ? e.target.value : x)))}
                placeholder={
                  i === 0
                    ? 'e.g. How much does it cost to hire an Indonesian helper?'
                    : 'What the client sends next, after the chatbot replies'
                }
                className="input input-lg"
                autoComplete="off"
              />
            </div>
          ))}
        </div>

        <div className="mt-4 flex flex-wrap items-end gap-3">
          {messages.length < MAX_MESSAGES ? (
            <button type="button" onClick={() => setMessages([...messages, ''])} className="btn btn-secondary">
              + Add a follow-up message
            </button>
          ) : null}
          {messages.length > 1 ? (
            <button type="button" onClick={() => setMessages(messages.slice(0, -1))} className="btn btn-secondary">
              Remove last
            </button>
          ) : null}
          <div className="w-[190px] flex-none">
            <label htmlFor="audience" className="field-label mb-1.5 block">
              Asking as
            </label>
            <select id="audience" value={audience} onChange={(e) => setAudience(e.target.value)} className="select h-10">
              {AUDIENCES.map((a) => (
                <option key={a.value} value={a.value}>
                  {a.label}
                </option>
              ))}
            </select>
          </div>
          <div className="ml-auto flex gap-2">
            {(result || error) && !busy ? (
              <button type="button" onClick={clear} className="btn btn-secondary">
                Clear
              </button>
            ) : null}
            <button type="submit" disabled={busy || !messages.some((m) => m.trim())} className="btn btn-primary">
              {busy ? 'Asking the chatbot…' : 'Ask the chatbot'}
            </button>
          </div>
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

      {result && result.turns.length ? (
        <section className="card card-pad space-y-5" aria-label="The chatbot's answer">
          {result.turns.map((t, i) => (
            <TurnResult key={i} turn={t} />
          ))}
          <p className="text-[12px] text-muted">Test mode: nothing was saved and no message was sent.</p>
        </section>
      ) : null}
    </>
  );
}
