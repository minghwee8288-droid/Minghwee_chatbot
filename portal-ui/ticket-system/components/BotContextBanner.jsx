import React from 'react';
import { BOT_STATUS_LABELS, OPEN_STATUSES } from '../utils/constants';
import { topicLabel } from '../utils/formatters';
import ServiceTypePill from './ServiceTypePill';

/**
 * Why the bot is quiet on this topic, and what changes that.
 *
 * The chatbot re-reads its open tickets every turn. While one sits in `open` or
 * `in_progress` it will acknowledge a message on that ticket's topic rather
 * than answer it — the client is waiting on a human. Setting the ticket to
 * `resolved` or `closed` is what hands the topic back. Nothing else in the
 * portal says so, and an agent who does not know it leaves the client parked.
 *
 * The topics are read from captured_info, never from the service_type column:
 * a topic the column cannot hold (a job seeker, an attachment, a question the
 * bot could not answer) is filed under a substitute, and only `topic_key` and
 * `also_topics` carry what the client actually asked about.
 */
export default function BotContextBanner({ ticket, conversation }) {
  if (!ticket) return null;

  const info = ticket.captured_info && typeof ticket.captured_info === 'object' ? ticket.captured_info : {};
  const primaryTopic = info.topic_key || null;
  const alsoTopics = Array.isArray(info.also_topics) ? info.also_topics.filter(Boolean) : [];
  const trueService = info.enquiry_type || null;
  const topics = [primaryTopic, ...alsoTopics].filter(Boolean);

  const isLive = OPEN_STATUSES.includes(ticket.status);
  const botStatus = conversation?.bot_status || null;

  return (
    <div
      className={`rounded-lg border px-4 py-3 ${
        isLive ? 'border-amber-200 bg-amber-50' : 'border-gray-200 bg-gray-50'
      }`}
    >
      <div className="flex items-start gap-2">
        <svg
          className={`mt-0.5 h-4 w-4 flex-shrink-0 ${isLive ? 'text-amber-600' : 'text-gray-400'}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth="2"
          aria-hidden="true"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d={
              isLive
                ? 'M12 9v4m0 4h.01M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18Z'
                : 'm5 13 4 4L19 7'
            }
          />
        </svg>
        <div className="min-w-0 flex-1">
          <p className={`text-sm font-bold ${isLive ? 'text-amber-900' : 'text-gray-700'}`}>
            {isLive
              ? 'The bot is not answering this topic'
              : 'This topic is back with the bot'}
          </p>
          <p className={`mt-0.5 text-sm ${isLive ? 'text-amber-800' : 'text-gray-600'}`}>
            {isLive
              ? 'While this ticket is Open or In Progress the bot acknowledges messages on it instead of replying. Set the status to Resolved or Closed to hand the topic back.'
              : 'The bot will answer messages on this topic again.'}
          </p>

          {topics.length ? (
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <span className="text-xs font-bold uppercase tracking-wide text-gray-500">
                {isLive ? 'Blocked topics' : 'Topics'}
              </span>
              {topics.map((topic) => (
                <ServiceTypePill key={topic} label={topicLabel(topic)} tone="accent" />
              ))}
            </div>
          ) : null}

          {trueService ? (
            <p className="mt-2 text-xs text-gray-600">
              <span className="font-bold">Filed as a substitute.</span> The client actually asked
              about <span className="font-bold">{topicLabel(trueService)}</span>, which the ticket&apos;s
              service type column cannot store.
            </p>
          ) : null}

          {botStatus ? (
            <p className="mt-2 text-xs text-gray-600">
              Conversation:{' '}
              <span className="font-bold">{BOT_STATUS_LABELS[botStatus] || botStatus}</span>
              {botStatus === 'human_active'
                ? ' — the bot is stood down on this whole thread, not just this topic.'
                : ''}
            </p>
          ) : null}
        </div>
      </div>
    </div>
  );
}
