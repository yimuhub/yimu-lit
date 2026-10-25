/* 安全轻量 Markdown：先转义，再只注入受控标签。绝不直接渲染未转义数据。 */

import { esc, escAttr, isHttpUrl } from './dom.js';

function inline(raw) {
  const parts = String(raw).split(/(`[^`\n]+`)/g);
  return parts
    .map((part) => {
      if (part.length > 2 && part.startsWith('`') && part.endsWith('`')) {
        return `<code>${esc(part.slice(1, -1))}</code>`;
      }
      let s = esc(part);
      s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
      s = s.replace(/(^|[\s(（])\*([^*\n]+)\*(?=$|[\s，。；、！？）),.!?])/g, '$1<em>$2</em>');
      s = s.replace(/~~([^~]+)~~/g, '<del>$1</del>');
      s = s.replace(/\[([^\]]{1,200})\]\((https?:\/\/[^\s)]+)\)/g, (match, label, url) => {
        const clean = url.replace(/&amp;/g, '&');
        if (!isHttpUrl(clean)) return match;
        return `<a href="${escAttr(clean)}" target="_blank" rel="noopener noreferrer nofollow">${label}</a>`;
      });
      return s;
    })
    .join('');
}

function isListLine(line) {
  return /^\s{0,3}([-*+]|\d{1,3}[.)])\s+/.test(line);
}

function listKind(line) {
  return /^\s{0,3}\d{1,3}[.)]\s+/.test(line) ? 'ol' : 'ul';
}

export function renderMarkdown(source) {
  const text = String(source == null ? '' : source).replace(/\r\n?/g, '\n').trim();
  if (!text) return '';

  const lines = text.split('\n');
  const out = [];
  let i = 0;
  let inCode = false;
  let codeLines = [];
  let listBuffer = [];
  let listType = 'ul';
  let paragraph = [];
  let inQuote = false;
  let quoteBuffer = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    out.push(`<p>${inline(paragraph.join('\n')).replace(/\n/g, '<br>')}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (!listBuffer.length) return;
    const items = listBuffer.map((item) => `<li>${inline(item)}</li>`).join('');
    out.push(`<${listType}>${items}</${listType}>`);
    listBuffer = [];
  };
  const flushQuote = () => {
    if (!quoteBuffer.length) return;
    out.push(`<blockquote>${quoteBuffer.map((q) => inline(q)).join('<br>')}</blockquote>`);
    quoteBuffer = [];
  };
  const flushAll = () => {
    flushParagraph();
    flushList();
    flushQuote();
  };

  while (i < lines.length) {
    const line = lines[i];

    if (/^\s*```/.test(line)) {
      if (inCode) {
        out.push(`<pre><code>${esc(codeLines.join('\n'))}</code></pre>`);
        codeLines = [];
        inCode = false;
      } else {
        flushAll();
        inCode = true;
      }
      i += 1;
      continue;
    }
    if (inCode) {
      codeLines.push(line);
      i += 1;
      continue;
    }

    if (!line.trim()) {
      flushAll();
      inQuote = false;
      i += 1;
      continue;
    }

    const heading = line.match(/^\s{0,3}(#{1,4})\s+(.+?)\s*#*\s*$/);
    if (heading) {
      flushAll();
      const level = heading[1].length;
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      i += 1;
      continue;
    }

    if (/^\s{0,3}([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) {
      flushAll();
      out.push('<hr>');
      i += 1;
      continue;
    }

    if (/^\s{0,3}>\s?/.test(line)) {
      flushParagraph();
      flushList();
      inQuote = true;
      quoteBuffer.push(line.replace(/^\s{0,3}>\s?/, ''));
      i += 1;
      continue;
    }
    if (inQuote) {
      flushQuote();
      inQuote = false;
    }

    if (isListLine(line)) {
      flushParagraph();
      const kind = listKind(line);
      if (listBuffer.length && kind !== listType) flushList();
      listType = kind;
      listBuffer.push(line.replace(/^\s{0,3}([-*+]|\d{1,3}[.)])\s+/, ''));
      i += 1;
      continue;
    }
    if (listBuffer.length) flushList();

    paragraph.push(line);
    i += 1;
  }

  if (inCode && codeLines.length) {
    out.push(`<pre><code>${esc(codeLines.join('\n'))}</code></pre>`);
  }
  flushAll();
  return out.join('');
}

export function renderMarkdownInto(el, source) {
  el.innerHTML = renderMarkdown(source);
  for (const link of el.querySelectorAll('a[href]')) {
    if (!isHttpUrl(link.getAttribute('href'))) link.removeAttribute('href');
    link.target = '_blank';
    link.rel = 'noopener noreferrer nofollow';
  }
  return el;
}

export function plainText(source, limit = 120) {
  const raw = String(source || '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/[#>*_`~\-]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return raw.length > limit ? `${raw.slice(0, limit)}…` : raw;
}
