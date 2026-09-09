'use client';

/**
 * Clear one WhatsApp conversation, without the terminal.
 *
 *   password + number  ->  Clear chat  ->  confirm  ->  done
 *
 * One form, one button. Earlier versions had a separate "Look up" step, a
 * verification panel and a tick box per lead; the client asked for all three
 * gone, on the grounds that they already know whose number they typed.
 *
 * The lookup still happens -- pressing Clear chat reads the number first --
 * but it is invisible, and its only purpose now is to fill in the confirmation
 * dialog so that dialog can say what it actually found rather than asking
 * "are you sure?" about nothing.
 *
 * That confirmation is the one step deliberately kept. Every delete here is
 * irreversible and the only thing identifying the target is a phone number
 * typed by hand, so one click on a dialog naming the contact is what stands
 * between a mistyped digit and somebody else's transcript.
 */

import { useCallback, useState } from 'react';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { Banner, Card, PasswordInput, Spinner, TextInput } from '@/components/ui';
import { prettyPhone } from '@/lib/phone';
import type { LookupResult, ResetResult } from '@/lib/types';

export default function Page() {
  const [password, setPassword] = useState('');
  const [unlocked, setUnlocked] = useState(false);

  const [phone, setPhone] = useState('');
  const [checking, setChecking] = useState(false);
  const [pending, setPending] = useState<LookupResult | null>(null);
  // A title as well as a body: "Already cleared" and "Nothing found" are
  // different outcomes and the client should not have to read the sentence to
  // tell them apart.
  const [notFound, setNotFound] = useState<{ title: string; body: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [clearing, setClearing] = useState(false);
  const [result, setResult] = useState<ResetResult | null>(null);

  // Only the steps that did not complete are ever rendered.
  const failedSteps = (result?.steps ?? []).filter((step) => step.status === 'failed');

  const post = useCallback(
    async (path: string, body: unknown) => {
      const response = await fetch(path, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          // The password goes to our own serverless function, which holds the
          // Supabase service-role key. That key never reaches this browser.
          'x-reset-password': password,
        },
        body: JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      return { response, data } as const;
    },
    [password],
  );

  /**
   * Clear chat: read the number, then open the confirmation.
   *
   * Nothing is deleted here. This only gathers what the dialog needs to say.
   */
  async function handleClear(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setNotFound(null);
    setResult(null);
    setPending(null);
    setChecking(true);
    try {
      const { response, data } = await post('/api/lookup', { phone });
      if (!response.ok) {
        setError(data?.error ?? 'Could not read that number.');
        if (response.status === 401) setUnlocked(false);
        return;
      }

      const lookup = data as LookupResult;
      setUnlocked(true);

      if (!lookup.allowed) {
        setError(lookup.blockedReason ?? 'This number is not allowed.');
        return;
      }
      if (!lookup.conversation && lookup.leads.length === 0) {
        setNotFound({
          title: 'Nothing found',
          body:
            `No conversation and no lead on ${prettyPhone(lookup.phone)}. ` +
            'Check the digits and the country code.',
        });
        return;
      }

      // Already cleared. The conversation row survives a reset -- it is
      // updated, not deleted -- so a second press finds a real conversation
      // with nothing left in it. Without this the client would be walked
      // through a confirmation dialog reading "0 messages, 0 tickets" and
      // then told it had been cleared, which reads as though the first clear
      // had not worked.
      const counts = lookup.conversation?.counts;
      const empty =
        !counts || (counts.messages === 0 && counts.tickets === 0 && counts.handovers === 0);
      if (empty && lookup.leads.length === 0) {
        setNotFound({
          title: 'Already cleared',
          body:
            `${prettyPhone(lookup.phone)} has no messages, tickets, handovers or leads left. ` +
            'There is nothing further to clear — the bot already has this conversation.',
        });
        return;
      }

      setPending(lookup);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setChecking(false);
    }
  }

  async function handleConfirm() {
    if (!pending) return;
    setClearing(true);
    setError(null);
    try {
      // A phone number and nothing else. The conversation and every lead are
      // resolved from it on the server.
      const { data } = await post('/api/reset', { phone: pending.phone });
      setResult(data as ResetResult);
      setPending(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setPending(null);
    } finally {
      setClearing(false);
    }
  }

  return (
    <main className="flex min-h-screen flex-col items-center justify-center px-6 py-12">
      <div className="w-full max-w-2xl space-y-7">
        <header className="text-center">
          <p className="text-xs font-bold uppercase tracking-[0.2em] text-brand/60">
            Ming Hwee Employment Agency
          </p>
          <h1 className="mt-2 text-4xl font-black tracking-tight text-brand sm:text-5xl">
            Clear a conversation
          </h1>
          <p className="mx-auto mt-3 max-w-lg text-base leading-relaxed text-slate-600">
            Hands one WhatsApp conversation back to the bot and deletes its lead.
          </p>
        </header>

        <Card>
          <form onSubmit={handleClear} className="space-y-6">
            {!unlocked ? (
              <PasswordInput
                label="Password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder="Shared password"
                autoComplete="current-password"
              />
            ) : null}

            <TextInput
              label="Contact number"
              value={phone}
              onChange={(event) => {
                setPhone(event.target.value);
                setNotFound(null);
                setResult(null);
              }}
              placeholder="+65 9123 4567"
              inputMode="tel"
              hint="Any format. An 8-digit number is assumed Singaporean."
            />

            <div>
              <button
                type="submit"
                disabled={checking || clearing || !phone.trim() || (!unlocked && !password)}
                className="w-full rounded-xl bg-danger px-6 py-4 text-base font-black tracking-wide text-white shadow-lg shadow-danger/25 transition hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-40 disabled:shadow-none"
              >
                {checking ? <Spinner label="Checking…" /> : 'Clear chat'}
              </button>
              <p className="mt-3 text-center text-sm text-slate-500">
                You will be asked to confirm before anything is deleted.
              </p>
            </div>
          </form>
        </Card>

        {error ? (
          <Banner tone="error" title="Something went wrong">
            {error}
          </Banner>
        ) : null}

        {notFound ? (
          <Banner tone="info" title={notFound.title}>
            {notFound.body}
          </Banner>
        ) : null}

        {/* On a clean run this is the whole result: one line saying it worked.
            The per-step breakdown is gone at the client's request.

            Anything that did NOT complete is still named, because "Cleared"
            printed over a lead the database refused would simply be untrue --
            and a blocked employer lead is a real outcome, not an edge case. */}
        {result && failedSteps.length === 0 ? (
          <Banner tone="success" title={`Cleared for ${prettyPhone(result.phone)}`}>
            The bot has the conversation back. Send it a message to start a fresh enquiry.
          </Banner>
        ) : null}

        {result && failedSteps.length > 0 ? (
          <Card tone="danger">
            <Banner tone="warning" title={`Partly cleared for ${prettyPhone(result.phone)}`}>
              The conversation was cleared, but this could not be deleted and has been left in
              place.
            </Banner>
            <ul className="mt-5 space-y-3">
              {failedSteps.map((step, index) => (
                <li
                  key={index}
                  className="rounded-xl border border-danger/20 bg-red-50/60 p-4 text-sm"
                >
                  <span className="block font-bold text-slate-900">{step.label}</span>
                  <span className="mt-1 block break-words leading-relaxed text-slate-600">
                    {step.detail}
                  </span>
                </li>
              ))}
            </ul>
          </Card>
        ) : null}

        <footer className="text-center text-sm leading-relaxed text-slate-400">
          Deletes rows the WhatsApp portal also owns. Every delete is scoped to the one number
          entered above.
        </footer>
      </div>

      {pending ? (
        <ConfirmDialog
          lookup={pending}
          busy={clearing}
          onCancel={() => setPending(null)}
          onConfirm={handleConfirm}
        />
      ) : null}
    </main>
  );
}
