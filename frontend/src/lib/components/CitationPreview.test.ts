import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import CitationPreview from './CitationPreview.svelte';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const citation = {
  marker: '[S1]',
  source_id: 'S1',
  source_type: 'document' as const,
  title: 'Handbook',
  source_filename: 'handbook.pdf',
  document_id: '11111111-1111-4111-8111-111111111111',
  document_version_id: '22222222-2222-4222-8222-222222222222',
  page_start: 2,
  page_end: 2
};

describe('citation preview dialog', () => {
  it('loads plain text, closes with Escape, and restores trigger focus', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Response.json({
          message_id: '33333333-3333-4333-8333-333333333333',
          source_id: 'S1',
          title: 'Handbook',
          source_filename: 'handbook.pdf',
          page_start: 2,
          page_end: 2,
          text: '<script>plain source text</script>'
        })
      )
    );
    render(CitationPreview, {
      messageId: '33333333-3333-4333-8333-333333333333',
      citation
    });
    const trigger = screen.getByRole('button', { name: 'Preview' });
    await fireEvent.click(trigger);
    expect(await screen.findByText('<script>plain source text</script>')).toBeInTheDocument();
    const dialog = screen.getByRole('dialog');
    await fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(dialog).not.toHaveAttribute('open');
    expect(trigger).toHaveFocus();
  });

  it('uses the generic unavailable message for a missing historical source', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Response(null, { status: 404 }))
    );
    render(CitationPreview, {
      messageId: '33333333-3333-4333-8333-333333333333',
      citation
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    expect(await screen.findByText('Preview is no longer available.')).toBeInTheDocument();
  });
});
