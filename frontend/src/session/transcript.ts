/**
 * Pure transcript-history rules (docs/07 partial vs final, docs/06 §12).
 *
 * - At most one provisional (partial) line, always last, replaced in place.
 * - A partial never becomes final by itself; only an `is_final` message commits.
 * - Empty or whitespace-only text is never shown.
 * - History is bounded to the most recent `MAX_FINAL_LINES` finals.
 */
export interface TranscriptLine {
  readonly id: string;
  readonly turnId: string | null;
  readonly text: string;
  readonly isFinal: boolean;
  /**
   * Set only for a delivered assistant line whose generation hit the
   * provider length limit (docs/08 §12 `response_completion_status in
   * {truncated_partial, truncated_fallback}`). Omitted (not `false`) on
   * every other line so it never shows up in an equality check that does
   * not care about it.
   */
  readonly truncated?: boolean;
}

export const MAX_FINAL_LINES = 50;

function withoutProvisional(lines: readonly TranscriptLine[]): readonly TranscriptLine[] {
  const last = lines.at(-1);
  return last !== undefined && !last.isFinal ? lines.slice(0, -1) : lines;
}

/**
 * Drops a still-streaming (non-final) line, used when its generation is
 * cancelled or interrupted (docs/06 §10, docs/08 §13): a cancelled
 * generation must never linger on screen as if still in progress.
 */
export function dropProvisionalLine(lines: readonly TranscriptLine[]): readonly TranscriptLine[] {
  return withoutProvisional(lines);
}

function boundFinals(lines: readonly TranscriptLine[]): readonly TranscriptLine[] {
  const last = lines.at(-1);
  if (last === undefined || last.isFinal) {
    return lines.slice(-MAX_FINAL_LINES);
  }
  return [...lines.slice(0, -1).slice(-MAX_FINAL_LINES), last];
}

function isLateDuplicate(lines: readonly TranscriptLine[], incoming: TranscriptLine): boolean {
  if (lines.some((line) => line.id === incoming.id)) {
    return true;
  }
  return (
    !incoming.isFinal &&
    incoming.turnId !== null &&
    lines.some((line) => line.isFinal && line.turnId === incoming.turnId)
  );
}

export function applyTranscriptLine(
  lines: readonly TranscriptLine[],
  incoming: TranscriptLine,
): readonly TranscriptLine[] {
  if (incoming.text.trim() === "") {
    // An empty final closes its turn: a dangling partial of that turn must not linger.
    const last = lines.at(-1);
    const dangling =
      incoming.isFinal && last !== undefined && !last.isFinal && last.turnId === incoming.turnId;
    return dangling ? lines.slice(0, -1) : lines;
  }
  if (isLateDuplicate(lines, incoming)) {
    return lines;
  }
  return boundFinals([...withoutProvisional(lines), incoming]);
}
