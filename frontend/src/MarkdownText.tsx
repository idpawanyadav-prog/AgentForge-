import React from 'react';

function inline(text: string): React.ReactNode[] {
  return text.split(/(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^\s)]+\))/g).map((part, index) => {
    if (part.startsWith('`') && part.endsWith('`'))
      return <code key={index}>{part.slice(1, -1)}</code>;
    if (part.startsWith('**') && part.endsWith('**'))
      return <strong key={index}>{part.slice(2, -2)}</strong>;
    const link = /^\[([^\]]+)\]\(([^\s)]+)\)$/.exec(part);
    if (link) {
      try {
        const url = new URL(link[2]);
        if (url.protocol === 'https:' || url.protocol === 'http:')
          return <a key={index} href={url.href} target="_blank" rel="noopener noreferrer">{link[1]}</a>;
      } catch { /* show invalid links as inert text */ }
    }
    return <React.Fragment key={index}>{part}</React.Fragment>;
  });
}

function blocks(text: string): React.ReactNode[] {
  const lines = text.split('\n');
  const result: React.ReactNode[] = [];
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    if (line.startsWith('- ')) {
      const items: React.ReactNode[] = [];
      while (index < lines.length && lines[index].startsWith('- ')) {
        items.push(<li key={index}>{inline(lines[index].slice(2))}</li>);
        index++;
      }
      result.push(<ul key={`list-${index}`}>{items}</ul>);
      index--;
    } else if (line.startsWith('### ')) {
      result.push(<div className="md-h3" key={index}>{inline(line.slice(4))}</div>);
    } else if (line.startsWith('## ')) {
      result.push(<div className="md-h2" key={index}>{inline(line.slice(3))}</div>);
    } else if (line.startsWith('# ')) {
      result.push(<div className="md-h1" key={index}>{inline(line.slice(2))}</div>);
    } else if (!line.trim()) {
      result.push(<div className="md-gap" key={index} />);
    } else {
      result.push(<div key={index}>{inline(line)}</div>);
    }
  }
  return result;
}

/** A bounded markdown subset rendered as React nodes; raw HTML stays text. */
export function MarkdownText({ text }: { text: string }) {
  return <>{blocks(text)}</>;
}

export function MarkdownPreview({ text }: { text: string }) {
  return <>{blocks(text)}</>;
}
