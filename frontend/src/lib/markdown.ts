import { safeExternalUrl } from './citations';

export function renderMarkdown(source: string): string {
  const protectedBlocks: string[] = [];
  let value = source.replaceAll('\u0000', '');

  value = value.replace(/```(?:[^\n]*)\n([\s\S]*?)```/g, (_match, code: string) =>
    token(protectedBlocks, `<pre><code>${escapeHtml(code.trimEnd())}</code></pre>`)
  );
  value = value.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (_match, label: string, url: string) => {
    if (!safeExternalUrl(url)) return escapeHtml(label);
    return token(
      protectedBlocks,
      `<a href="${escapeAttribute(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`
    );
  });
  value = value.replace(/`([^`\n]+)`/g, (_match, code: string) =>
    token(protectedBlocks, `<code>${escapeHtml(code)}</code>`)
  );

  value = escapeHtml(value);
  value = value.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  value = value.replace(/__([^_\n]+)__/g, '<strong>$1</strong>');
  value = value.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');

  const blocks = value
    .split(/\n{2,}/)
    .map((block) => {
      if (block.startsWith('\u0000P') && block.endsWith('\u0000')) return block;
      if (/^(?:- .+(?:\n|$))+/.test(block)) {
        const items = block
          .split('\n')
          .filter(Boolean)
          .map((line) => `<li>${line.replace(/^- /, '')}</li>`)
          .join('');
        return `<ul>${items}</ul>`;
      }
      const heading = /^(#{1,3}) (.+)$/.exec(block);
      if (heading) {
        const level = heading[1]?.length ?? 1;
        return `<h${level}>${heading[2]}</h${level}>`;
      }
      return `<p>${block.replaceAll('\n', '<br>')}</p>`;
    })
    .join('');

  return protectedBlocks.reduce(
    (rendered, block, index) => rendered.replace(`\u0000P${index}\u0000`, block),
    blocks
  );
}

function token(values: string[], value: string): string {
  const marker = `\u0000P${values.length}\u0000`;
  values.push(value);
  return marker;
}

function escapeHtml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function escapeAttribute(value: string): string {
  return escapeHtml(value);
}
