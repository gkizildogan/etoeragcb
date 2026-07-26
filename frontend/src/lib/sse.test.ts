import { describe, expect, it } from 'vitest';
import { ChatAccumulator, SSEParser } from './sse';

describe('SSE protocol', () => {
  it('parses fragmented streams, ignores unknown events, and treats replace as authoritative', () => {
    const parser = new SSEParser();
    const accumulator = new ChatAccumulator();
    const fragments = [
      'event: start\ndata: {"request_id":"1"}\n\n',
      'event: status\ndata: {"stage":"retriev',
      'ing"}\n\nevent: delta\ndata: {"text":"draft"}\n\n',
      'event: mystery\ndata: {"ignored":true}\n\n',
      'event: replace\ndata: {"text":"final [S1]"}\n\n',
      'event: citations\ndata: {"items":{}}\n\n',
      'event: done\ndata: {"message_id":"m1","route":"answer"}\n\n'
    ];
    fragments.forEach((fragment, index) => {
      for (const event of parser.feed(fragment, index === fragments.length - 1)) {
        accumulator.apply(event);
      }
    });

    expect(accumulator.answer).toBe('final [S1]');
    expect(accumulator.stages).toEqual(['retrieving']);
    expect(accumulator.done).toBe(true);
    expect(accumulator.assistantMessageId).toBe('m1');
  });

  it('records retryable terminal errors', () => {
    const accumulator = new ChatAccumulator();
    accumulator.apply({ event: 'error', data: { code: 'generation_failed', retryable: true } });
    expect(accumulator.errorCode).toBe('generation_failed');
    expect(accumulator.retryable).toBe(true);
  });
});
