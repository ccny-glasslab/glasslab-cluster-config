#!/usr/bin/env python3
"""Scrape LibreTexts textbooks into clean markdown for the knowledge corpus.

Usage:
    python3 scrape_libretexts.py --book Introduction-to-Real-Analysis-Trench --out corpus/
    python3 scrape_libretexts.py --all --out corpus/      # all books in the manifest

Requires: pandoc (for HTML -> markdown with LaTeX math), curl, beautifulsoup4.

Each book's HTML pages are fetched, chrome is stripped, lt-math spans become
$...$/$$...$$, and pandoc converts to markdown (math preserved as LaTeX).
The result is a folder of .md files ready for upload_knowledge_dir.py.

Book slugs are the LibreTexts shelf paths; the manifest lists the curated
undergraduate math/stats base (see docs/glasslab-v2/runbooks/knowledge-corpus.md).
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

BOOKS: list[dict[str, str]] = [
    {"domain": "math", "shelf": "Calculus", "book": "Calculus_(OpenStax)"},
    {"domain": "math", "shelf": "Linear_Algebra", "book": "A_First_Course_in_Linear_Algebra_(Kuttler)"},
    {"domain": "math", "shelf": "Analysis", "book": "Introduction_to_Real_Analysis_(Trench)"},
    {"domain": "math", "shelf": "Differential_Equations", "book": "Elementary_Differential_Equations_with_Boundary_Value_Problems_(Trench)"},
    {"domain": "math", "shelf": "Combinatorics_and_Discrete_Mathematics", "book": "A_Cool_Brisk_Walk_Through_Discrete_Mathematics_(Davies)"},
    {"domain": "stats", "shelf": "Probability_Theory", "book": "Probability_Mathematical_Statistics_and_Stochastic_Processes_(Siegrist)"},
    {"domain": "stats", "shelf": "Introductory_Statistics", "book": "Introductory_Statistics_2e_(OpenStax)"},
    {"domain": "math", "shelf": "Abstract_and_Geometric_Algebra", "book": "Abstract_Algebra%3A_Theory_and_Applications_(Judson)"},
]


def fetch(url: str, retries: int = 3) -> str:
    for attempt in range(retries):
        try:
            r = subprocess.run(
                ['curl', '-fsSL', '-m', '40', url],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0:
                return r.stdout
        except Exception:
            pass
        time.sleep(2)
    return ''


def clean_article(article_html: str) -> str:
    """Strip chrome; convert lt-math spans and raw TeX to $...$ / $$...$$."""
    def math_repl(m):
        tex = html_mod.unescape(m.group(1)).strip()
        if tex.startswith('\\[') and tex.endswith('\\]'):
            return '$$\n' + tex[2:-2].strip() + '\n$$'
        return '$' + tex + '$'
    article = re.sub(
        r'<span class="lt-math-[^"]*"[^>]*>(.*?)</span>',
        math_repl, article_html, flags=re.S,
    )
    article = article.replace('\\(', '$').replace('\\)', '$')
    article = article.replace('\\[', '$$').replace('\\]', '$$')
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(article, 'html.parser')
    for tag in soup.find_all(['style', 'script', 'nav', 'header', 'footer']):
        tag.decompose()
    for div in soup.find_all('div'):
        if not hasattr(div, 'attrs') or div.attrs is None:
            continue
        cls = ' '.join(div.get('class') or [])
        did = div.get('id') or ''
        if any(m in cls or m in did for m in (
            'header', 'aside', 'noindex', 'guide-content', 'Headertext',
            'author-container', 'toc-container', 'flash-messages', 'dekiFlash',
            'license', 'breadcrumb', 'mt-sidebarbutton',
        )):
            div.decompose()
    return str(soup)


def finalize_markdown(md: str) -> str:
    """Polish pandoc output into clean, math-readable markdown."""
    md = md.replace('\\$', '$')
    md = md.replace('{}', '').replace(' \\nonumber', '')
    md = md.replace('\\_{', '_{').replace('\\_}', '_}')
    lines = md.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith('# ')), 0)
    title = lines[start]
    chrome_markers = (
        'Last updated', 'Save as PDF', 'Page ID', 'data-timestamp',
        'mt-last-updated', 'mt-icon-article-pdf', 'mt-toc-container',
        '<span class="mt-', 'Print this page', 'Download Page',
    )
    body: list[str] = []
    in_chrome = True
    for ln in lines[start + 1:]:
        s = ln.strip()
        if in_chrome:
            if s.startswith('## ') or (
                len(s) > 60 and '<' not in s and not re.match(r'^\d+\.', s)
            ):
                in_chrome = False
            else:
                continue
        if any(m in s for m in chrome_markers):
            continue
        if re.match(r'^(<\/?div|<style|----------)', s):
            continue
        if s.startswith('<div class="aside'):
            continue
        body.append(ln)
    return (title + '\n\n' + '\n'.join(body)).strip() + '\n'


def convert(url: str) -> str:
    html = fetch(url)
    m = re.search(r'<article[^>]*id="elm-main-content".*?</article>', html, re.S)
    if not m:
        return ''
    tmp = '/tmp/libretexts-clean.html'
    open(tmp, 'w', encoding='utf-8').write(clean_article(m.group(0)))
    r = subprocess.run(['pandoc', '-f', 'html', '-t', 'gfm', tmp],
                       capture_output=True, text=True)
    return finalize_markdown(r.stdout) if r.returncode == 0 else ''


def page_links(html: str, base: str) -> list[str]:
    out = []
    for m in re.finditer(r'href="([^"]*' + re.escape(base) + r'[^"]*)"', html):
        u = m.group(1)
        if u.rstrip('/') in (base, base.rstrip('/')):
            continue
        out.append(u)
    return list(dict.fromkeys(out))


def crawl_book(entry: dict[str, str], out_root: Path) -> None:
    domain, shelf, book = entry['domain'], entry['shelf'], entry['book']
    slug = re.sub(r'[^A-Za-z0-9]+', '-', urllib.parse.unquote(book)).strip('-')[:50]
    outdir = out_root / slug
    outdir.mkdir(parents=True, exist_ok=True)
    base = f'https://{domain}.libretexts.org/Bookshelves/{shelf}/{urllib.parse.quote(book)}'
    base_unquoted = urllib.parse.unquote(base)
    print(f'== crawling {slug}', flush=True)
    root = fetch(base)
    if not root:
        print('  ROOT FETCH FAILED')
        return
    pages: dict[str, str] = {base: root}
    for ch in page_links(root, base_unquoted):
        pages.setdefault(ch, '')
    for ch in list(pages):
        if not pages[ch]:
            pages[ch] = fetch(ch)
            for sec in page_links(pages[ch], ch):
                pages.setdefault(sec, '')
    done = skipped = 0
    for idx, (url, html) in enumerate(sorted(pages.items())):
        title = urllib.parse.unquote(url).split('/')[-1]
        if re.search(r'[Ll]icensing|Front_Matter|Back_Matter', title):
            skipped += 1
            continue
        fname = re.sub(r'[^A-Za-z0-9._-]+', '-', title).strip('-')
        if not fname:
            fname = f'page-{idx}'
        target = outdir / f'{idx:03d}-{fname[:80]}.md'
        if target.exists():
            done += 1
            continue
        md = convert(url) if html else ''
        if md and len(md) > 200:
            target.write_text(md, encoding='utf-8')
            done += 1
        else:
            skipped += 1
        time.sleep(0.3)
    print(f'== {slug}: pages={len(pages)} done={done} skipped={skipped}', flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--all', action='store_true', help='scrape every manifest book')
    parser.add_argument('--book', default='', help='book slug prefix to scrape (e.g. Introduction-to-Real-Analysis)')
    parser.add_argument('--out', default='corpus', help='output directory')
    parser.add_argument('--manifest', help='JSON manifest path (default: embedded BOOKS list)')
    args = parser.parse_args(argv)
    books = json.load(open(args.manifest)) if args.manifest else BOOKS
    for entry in books:
        slug = re.sub(r'[^A-Za-z0-9]+', '-', urllib.parse.unquote(entry['book'])).strip('-')[:50]
        if args.all or (args.book and slug.startswith(args.book)):
            crawl_book(entry, Path(args.out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
