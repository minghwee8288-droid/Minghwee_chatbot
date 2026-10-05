import { figureParts } from '@/lib/documents';

/** Chunk text with every figure highlighted, so a reviewer sees each number the bot could quote. */
export function FigureText({ text, className = '' }: { text: string; className?: string }) {
  return (
    <p className={`whitespace-pre-wrap break-words text-[13px] leading-[1.6] ${className}`}>
      {figureParts(text).map((p, i) =>
        p.figure ? (
          <mark key={i} className="figure">
            {p.text}
          </mark>
        ) : (
          <span key={i}>{p.text}</span>
        ),
      )}
    </p>
  );
}
