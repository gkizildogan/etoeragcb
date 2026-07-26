import { validateCitations } from './citations';
import type { Citation } from './types';

export interface SSEEvent {
  event: string;
  data: Record<string, unknown>;
  id?: string;
}

export class SSEParser {
  private buffer = '';
  private eventName = 'message';
  private eventId: string | undefined;
  private dataLines: string[] = [];

  feed(fragment: string, final = false): SSEEvent[] {
    this.buffer += fragment;
    const events: SSEEvent[] = [];
    let newline = this.buffer.indexOf('\n');
    while (newline >= 0) {
      const raw = this.buffer.slice(0, newline).replace(/\r$/, '');
      this.buffer = this.buffer.slice(newline + 1);
      const event = this.line(raw);
      if (event) events.push(event);
      newline = this.buffer.indexOf('\n');
    }
    if (final && this.buffer) {
      const event = this.line(this.buffer.replace(/\r$/, ''));
      if (event) events.push(event);
      this.buffer = '';
    }
    if (final && this.dataLines.length > 0) {
      events.push(this.dispatch());
    }
    return events;
  }

  private line(line: string): SSEEvent | null {
    if (!line) return this.dataLines.length > 0 ? this.dispatch() : null;
    if (line.startsWith(':')) return null;
    const separator = line.indexOf(':');
    const field = separator >= 0 ? line.slice(0, separator) : line;
    let value = separator >= 0 ? line.slice(separator + 1) : '';
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') this.eventName = value;
    else if (field === 'id' && !value.includes('\0')) this.eventId = value;
    else if (field === 'data') this.dataLines.push(value);
    return null;
  }

  private dispatch(): SSEEvent {
    let data: unknown;
    try {
      data = JSON.parse(this.dataLines.join('\n'));
    } catch {
      throw new Error('invalid SSE JSON');
    }
    if (!isRecord(data)) throw new Error('invalid SSE payload');
    const event = {
      event: this.eventName,
      data,
      ...(this.eventId === undefined ? {} : { id: this.eventId })
    };
    this.eventName = 'message';
    this.eventId = undefined;
    this.dataLines = [];
    return event;
  }
}

export class ChatAccumulator {
  answer = '';
  citations: Record<string, Citation> = {};
  stages: string[] = [];
  assistantMessageId: string | null = null;
  route: string | null = null;
  errorCode: string | null = null;
  retryable = false;
  started = false;
  done = false;

  apply(event: SSEEvent): void {
    const data = event.data;
    if (event.event === 'start') {
      this.answer = '';
      this.citations = {};
      this.stages = [];
      this.assistantMessageId = null;
      this.route = null;
      this.errorCode = null;
      this.retryable = false;
      this.started = true;
      this.done = false;
    } else if (event.event === 'status') {
      if (
        typeof data.stage === 'string' &&
        ['planning', 'retrieving', 'reranking', 'generating'].includes(data.stage)
      ) {
        this.stages.push(data.stage);
      }
    } else if (event.event === 'delta' && typeof data.text === 'string') {
      this.answer += data.text;
    } else if (event.event === 'replace' && typeof data.text === 'string') {
      this.answer = data.text;
    } else if (event.event === 'citations') {
      this.citations = validateCitations(data.items);
    } else if (event.event === 'done') {
      if (typeof data.message_id === 'string') this.assistantMessageId = data.message_id;
      if (typeof data.route === 'string') this.route = data.route;
      this.done = true;
    } else if (event.event === 'error') {
      this.errorCode = typeof data.code === 'string' ? data.code : 'chat_error';
      this.retryable = data.retryable === true;
    }
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
