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
      const data = await res.json();
      if (res.status === 401) {
        window.location.href = '/login?e=bad-session';
        return;
      }
      if (!res.ok) setError(data.error ?? `Failed (${res.status})`);
      else setResult(data as Result);
    } catch {
      setError('The request failed.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <form onSubmit={run} className="space-y-3 rounded-lg border border-slate-200 bg-white p-4">
        {messages.map((m, i) => (
          <label key={i} className="block text-sm">
            <span className="text-xs text-slate-500">
              {messages.length > 1 ? `Client message ${i + 1}` : 'Client message'}
            </span>
            <textarea
              value={m}
              maxLength={2000}
              rows={2}
              onChange={(e) => setMessages(messages.map((x, j) => (j === i ? e.target.value : x)))}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
            />
          </label>
        ))}
        <div className="flex flex-wrap items-center gap-3 text-sm">
          {messages.length < 10 ? (
            <button type="button" onClick={() => setMessages([...messages, ''])} className="rounded-md border border-slate-300 px-3 py-1.5">
              + Add a follow-up message
            </button>
          ) : null}
          {messages.length > 1 ? (
            <button type="button" onClick={() => setMessages(messages.slice(0, -1))} className="rounded-md border border-slate-300 px-3 py-1.5">
              Remove last
            </button>
          ) : null}
          <label className="flex items-center gap-2">
            <span className="text-xs text-slate-500">Asking as</span>
            <select value={audience} onChange={(e) => setAudience(e.target.value)} className="rounded-md border border-slate-300 px-2 py-1.5">
              <option value="unknown">new number</option>
              <option value="employer">employer</option>
              <option value="candidate">helper / candidate</option>
            </select>
          </label>
          <button
            type="submit"
            disabled={busy || !messages.some((m) => m.trim())}
            className="ml-auto rounded-md bg-brand px-4 py-1.5 text-white disabled:opacity-60"
          >
            {busy ? 'Asking the bot…' : 'Ask the bot'}
          </button>
        </div>
      </form>

      {error ? <p className="rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-800">{error}</p> : null}

      {result ? (
        <div className="space-y-4">
          {result.turns.map((t, i) => (
            <div key={i} className="rounded-lg border border-slate-200 bg-white p-4 text-sm">
              <p className="text-xs text-slate-500">Client</p>
              <p className="mb-3 whitespace-pre-wrap">{t.message}</p>
              <p className="text-xs text-slate-500">Bot</p>
              <p className="mb-3 whitespace-pre-wrap rounded-md bg-emerald-50 p-2">{t.reply || '(no reply)'}</p>
              <p className="mb-2 text-xs text-slate-500">
                intent {t.intent ?? '—'} · service {t.service_type ?? '—'} · best score {t.rag_best_score}
                {t.needs_handover ? ' · hands over to an agent' : ''}
              </p>
              {t.sources.length ? (
                <table className="w-full text-xs">
                  <thead className="text-left text-slate-500">
                    <tr>
                      <th className="py-1 pr-2">Score</th>
                      <th className="py-1 pr-2">Row</th>
                      <th className="py-1 pr-2">Service</th>
                      <th className="py-1">Nationality</th>
                    </tr>
                  </thead>
                  <tbody>
                    {t.sources.map((s) => (
                      <tr key={s.id} className="border-t border-slate-100">
                        <td className="py-1 pr-2 tabular-nums">{s.similarity.toFixed(3)}</td>
                        <td className="py-1 pr-2">
                          <Link href={`/rows/${s.id}`} className="text-brand hover:underline">
                            {s.question || s.id}
                          </Link>
                        </td>
                        <td className="py-1 pr-2">{s.service_type}</td>
                        <td className="py-1">{s.nationality}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="text-xs text-slate-500">No knowledge-base rows were used.</p>
              )}
            </div>
          ))}
          <p className="text-xs text-slate-500">
            Rules read from: {result.rules_source ?? '—'} · Writes the bot would have made and were refused:{' '}
            {result.refused_writes.length
              ? result.refused_writes.map((w) => `${w.method} ${w.target}`).join(', ')
              : 'none'}
          </p>
        </div>
      ) : null}
    </div>
  );
}
