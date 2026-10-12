#!/usr/bin/env python
"""Cheap comment-count probe: top posts per topic without fetching each post.

Stage 6 of the pipeline. One s.weibo.com search request per topic (notes §7),
parsing the result cards' HTML directly. Three traps the parser must handle:

  1. The card-act footer order is 转发 | 评论 | 赞, but a 收藏 <li> sits between
     the first two inside an HTML comment. Strip `<!--...-->` BEFORE indexing,
     or the "2nd <li>" is 收藏 and every count is wrong.
  2. A bare label with no digits means 0, not a parse failure.
  3. The post link is the first weibo.com/<uid>/<mblogid> anywhere in the card.
     A regex anchored on the `from` div breaks on ad-tile and nested-div cards.

Rate: 1 request per topic at a randomized 4-7s delay. That survived 400 searches
on a single cookie without tripping the block (notes §5) - distinct-query bursts
are what burn an account fastest, so do not tighten this.

Environment:
  PROBE_SEL           selected.json for the campaign (list of {'title': ...})
  PROBE_OUT           destination CSV (utf-8-sig, resumable)
  PROBE_TOPN          posts recorded per topic (default 5)
  PROBE_SCORE_FIELD   score column to carry through (default llm_score)
  WEIBO_COOKIE_FILE   cookie (default ~/weibo-corpus/secrets/cookie.txt)

Resumable: topics already present in PROBE_OUT are skipped, and the CSV is
rewritten after every topic, so a block mid-run loses nothing. On a 302 to
login.sina.com.cn the run stops cleanly - re-login and rerun.
"""
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.weibo import read_csv_bom, write_csv_bom, is_blocked

HTML_COMMENT_RE = re.compile(r'<!--.*?-->', re.DOTALL)
CARD_RE = re.compile(r'<div class="card-wrap".*?(?=<div class="card-wrap"|\Z)', re.DOTALL)
ACT_RE = re.compile(r'<div class="card-act".*?</div>', re.DOTALL)
LI_RE = re.compile(r'<li.*?</li>', re.DOTALL)
LINK_RE = re.compile(r'weibo\.com/(\d+)/([A-Za-z0-9]{6,12})')
DIGITS_RE = re.compile(r'(\d[\d,]*)')
TAG_RE = re.compile(r'<[^>]+>')
DELAY = (4, 7)
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10.13; rv:61.0) '
      'Gecko/20100101 Firefox/61.0')


def parse_cards(html):
    """Extract (comment count, post url, mblogid) for each search result card."""
    out = []
    for card in CARD_RE.findall(html):
        card = HTML_COMMENT_RE.sub('', card)   # drop commented-out 收藏 li FIRST
        act = ACT_RE.search(card)
        if not act:
            continue
        lis = LI_RE.findall(act.group(0))
        if len(lis) < 2:
            continue
        m = DIGITS_RE.search(TAG_RE.sub('', lis[1]))
        comments = int(m.group(1).replace(',', '')) if m else 0
        link = LINK_RE.search(card)
        if not link:
            continue
        out.append({'comments': comments,
                    'url': f'https://weibo.com/{link.group(1)}/{link.group(2)}',
                    'mblogid': link.group(2)})
    return out


def build_headers(cookie):
    """s.weibo.com search needs only UA+Cookie, but keep the ajax headers so the
    same header dict works for weibo.com/ajax/* calls (notes §1)."""
    xsrf = ''
    for part in cookie.split(';'):
        part = part.strip()
        if part.startswith('XSRF-TOKEN='):
            xsrf = part.split('=', 1)[1]
            break
    return {'User-Agent': UA, 'Cookie': cookie,
            'Referer': 'https://weibo.com/', 'X-XSRF-TOKEN': xsrf}


def fields_for(topn, score_field):
    return (['topic', 'category', 'max_hot', 'topic_discuss', score_field, 'n_posts']
            + [f'c{i}' for i in range(1, topn + 1)]
            + [f'url{i}' for i in range(1, topn + 1)]
            + ['top5_comment_sum', 'top5_comment_max'])


def probe_row(topic, cards, topn, score_field):
    cards = sorted(cards, key=lambda c: -c['comments'])[:topn]
    row = {'topic': topic['title'], 'category': topic.get('category', ''),
           'max_hot': topic.get('max_hot', ''),
           'topic_discuss': topic.get('max_discuss', ''),
           score_field: topic.get(score_field, ''),
           'n_posts': len(cards)}
    for j in range(1, topn + 1):
        c = cards[j - 1] if j <= len(cards) else None
        row[f'c{j}'] = c['comments'] if c else ''
        row[f'url{j}'] = c['url'] if c else ''
    row['top5_comment_sum'] = sum(c['comments'] for c in cards)
    row['top5_comment_max'] = cards[0]['comments'] if cards else 0
    return row


def main():
    sel_path = os.environ['PROBE_SEL']
    out_path = Path(os.environ['PROBE_OUT'])
    topn = int(os.environ.get('PROBE_TOPN', '5'))
    score_field = os.environ.get('PROBE_SCORE_FIELD', 'llm_score')
    cookie_file = os.environ.get(
        'WEIBO_COOKIE_FILE', str(Path.home() / 'weibo-corpus/secrets/cookie.txt'))
    cookie = Path(cookie_file).read_text(encoding='utf-8').strip()
    headers = build_headers(cookie)

    topics = json.loads(Path(sel_path).read_text(encoding='utf-8'))
    rows, done = [], set()
    if out_path.exists():
        rows = read_csv_bom(out_path)
        done = {r['topic'] for r in rows}
        print(f'resuming: {len(done)} topics already probed')

    fields = fields_for(topn, score_field)
    for i, topic in enumerate(topics, 1):
        if topic['title'] in done:
            continue
        url = f"https://s.weibo.com/weibo?q={quote(topic['title'])}&page=1"
        try:
            r = requests.get(url, headers=headers, allow_redirects=False, timeout=30)
        except requests.RequestException as e:
            print(f'[{i}/{len(topics)}] {topic["title"]}: request failed ({e}) — skipping')
            time.sleep(random.uniform(*DELAY))
            continue
        if is_blocked(r.status_code, r.headers.get('Location', '')):
            print(f'BLOCKED at topic {i}/{len(topics)}: {topic["title"]}\n'
                  f'  cookie is soft-blocked — re-login, refresh {cookie_file}, rerun')
            break
        row = probe_row(topic, parse_cards(r.text), topn, score_field)
        rows.append(row)
        write_csv_bom(out_path, rows, fields)   # flush per topic: a block loses nothing
        print(f'[{i}/{len(topics)}] {row["topic"]}: max={row["top5_comment_max"]} '
              f'n={row["n_posts"]}')
        time.sleep(random.uniform(*DELAY))
    print(f'{len(rows)} rows -> {out_path}')


if __name__ == '__main__':
    main()
