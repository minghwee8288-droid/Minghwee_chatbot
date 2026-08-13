import React, { useEffect, useRef } from 'react';
import { formatDateSGT, formatTimeSGT } from '../utils/formatters';

/**
 * The WhatsApp transcript, as the client and the agents wrote it.
 *
 * Direction is stored as 'inbound'/'outbound' — the chatbot's own constant, and
 * what the portal writes. Who sent an outbound message comes from `sent_by`
 * before `is_bot`: a WhatsApp Business away message is recorded with
 * is_bot = true so it cannot trip the agent grace window, but it is not the
 * chatbot and labelling it "Bot" makes the thread misleading.
 */

const INBOUND = 'inbound';

function speakerFor(message) {
  // Fills sit on the thread's grey ground, so the customer's own bubble is
  // white-on-grey rather than grey-on-grey.
  if (message.direction === INBOUND) {
    return { label: 'Customer', side: 'left', bubble: 'border border-gray-200 bg-white text-gray-800' };
  }
  if (message.sent_by === 'agent') {
    return { label: 'Agent', side: 'right', bubble: 'bg-green-50 text-gray-800' };
  }
  if (message.sent_by === 'whatsapp_auto') {
    return { label: 'WhatsApp auto-reply', side: 'right', bubble: 'border border-gray-200 bg-white text-gray-600' };
  }
  if (message.sent_by === 'bot' || message.is_bot) {
    return { label: 'Bot', side: 'right', bubble: 'bg-blue-50 text-gray-800' };
  }
  return { label: 'Agent', side: 'right', bubble: 'bg-green-50 text-gray-800' };
}

function MediaTag({ message }) {
  const name = message.media_filename || message.media_type || 'attachment';
  return (
    <span className="mt-1 inline-block rounded bg-gray-200 px-2 py-0.5 text-xs text-gray-600">
      [Media: {name}]
    </span>
  );
}

export default function ConversationThread({ messages = [] }) {
  const endRef = useRef(null);
  const containerRef = useRef(null);

  useEffect(() => {
    // Scroll the thread's own container, not the page: scrollIntoView on the
    // last bubble would drag the whole drawer past its sticky header.
    const container = containerRef.current;
    if (container) container.scrollTop = container.scrollHeight;
  }, [messages]);

  if (!messages.length) {
    return (
      <div className="rounded-lg bg-gray-50 px-4 py-6 text-center text-sm text-gray-500">
        No messages in this conversation
      </div>
    );
  }

  let lastDay = null;

  return (
    <div
      ref={containerRef}
      // Scrolls inside the drawer rather than lengthening it: a long transcript
      // would otherwise bury the handover records under hundreds of bubbles.
      // The grey ground is what marks it as its own region — on white the inner
      // scrollbar reads as the drawer's own.
      className="max-h-[420px] space-y-2 overflow-y-auto rounded-lg border border-gray-200 bg-gray-50 p-3"
    >
      {messages.map((message) => {
        const speaker = speakerFor(message);
        const isLeft = speaker.side === 'left';
        const body = (message.body || '').trim();
        const hasMedia = Boolean(message.media_filename || message.media_path || message.media_type);

        const day = message.created_at ? formatDateSGT(message.created_at) : null;
        const showDay = day && day !== lastDay;
        if (showDay) lastDay = day;

        return (
          <React.Fragment key={message.id || message.whapi_message_id}>
            {showDay ? (
              <div className="flex justify-center py-1">
                <span className="rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-500">
                  {day}
                </span>
              </div>
            ) : null}
            <div className={`flex ${isLeft ? 'justify-start' : 'justify-end'}`}>
              <div className={`max-w-[80%] rounded-lg px-3 py-2 ${speaker.bubble}`}>
                <p className="text-xs font-bold uppercase tracking-wide text-gray-500">
                  {speaker.label}
                </p>
                {body ? (
                  <p className="mt-0.5 whitespace-pre-wrap break-words text-sm">{body}</p>
                ) : null}
                {hasMedia && !body ? <MediaTag message={message} /> : null}
                {hasMedia && body ? (
                  <div>
                    <MediaTag message={message} />
                  </div>
                ) : null}
                <p className="mt-1 text-right text-xs text-gray-400">
                  {formatTimeSGT(message.created_at)}
                </p>
              </div>
            </div>
          </React.Fragment>
        );
      })}
      <div ref={endRef} />
    </div>
  );
}
