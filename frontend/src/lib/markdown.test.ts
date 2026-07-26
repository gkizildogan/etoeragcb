import { describe, expect, it } from 'vitest';
import { renderMarkdown } from './markdown';

describe('safe Markdown rendering', () => {
  it('escapes raw HTML and rejects unsafe link protocols', () => {
    const html = renderMarkdown(
      '<img src=x onerror=alert(1)> [bad](javascript:alert(1)) [good](https://example.com)'
    );
    expect(html).toContain('&lt;img');
    expect(html).not.toContain('<img');
    expect(html).not.toContain('javascript:');
    expect(html).toContain('href="https://example.com"');
    expect(html).toContain('rel="noopener noreferrer"');
  });
});
