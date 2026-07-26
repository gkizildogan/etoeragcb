<script lang="ts">
  import { renderMarkdown } from '$lib/markdown';

  let { content }: { content: string } = $props();
</script>

<div class="markdown">{@html renderMarkdown(content)}</div>

<style>
  .markdown {
    overflow-wrap: anywhere;
    line-height: 1.68;
  }

  .markdown :global(p) {
    margin: 0.6rem 0;
  }

  .markdown :global(p:first-child) {
    margin-top: 0;
  }

  .markdown :global(p:last-child) {
    margin-bottom: 0;
  }

  .markdown :global(a) {
    color: var(--accent-strong);
    text-underline-offset: 0.16em;
  }

  .markdown :global(code) {
    padding: 0.13rem 0.3rem;
    border-radius: 0.3rem;
    color: #21483f;
    background: #e7eee9;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.88em;
  }

  .markdown :global(pre) {
    max-width: 100%;
    padding: 0.9rem;
    overflow: auto;
    border-radius: 0.55rem;
    background: #17282d;
  }

  .markdown :global(pre code) {
    padding: 0;
    color: #dce9e6;
    background: none;
  }

  .markdown :global(ul) {
    margin: 0.7rem 0;
    padding-left: 1.35rem;
  }
</style>
