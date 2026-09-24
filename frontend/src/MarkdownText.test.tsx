import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MarkdownPreview, MarkdownText } from './MarkdownText';

describe('safe markdown rendering', () => {
  it('renders formatting as elements and leaves HTML payloads inert', () => {
    const { container } = render(<div><MarkdownText text={'**strong** `code` <img src=x onerror=alert(1)>'} /></div>);
    expect(screen.getByText('strong').tagName).toBe('STRONG');
    expect(screen.getByText('code').tagName).toBe('CODE');
    expect(container.querySelector('img')).toBeNull();
    expect(container.textContent).toContain('<img src=x onerror=alert(1)>');
  });

  it('renders preview headings without parsing embedded HTML', () => {
    const { container } = render(<MarkdownPreview text={'# Heading\n- <script>alert(1)</script>'} />);
    expect(container.querySelector('.md-h1')?.textContent).toBe('Heading');
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('li')?.textContent).toContain('<script>');
  });

  it('turns relative document links into same-origin anchors', () => {
    const { container } = render(
      <MarkdownText text={'- **BAS**: [read BAS](/api/v1/projects/p1/documents/BAS)'} />);
    const anchor = container.querySelector('a');
    expect(anchor?.getAttribute('href')).toBe(`${window.location.origin}/api/v1/projects/p1/documents/BAS`);
    expect(anchor?.getAttribute('rel')).toContain('noopener');
  });
});
