#!/usr/bin/env python
"""For each hot-search topic, find the most-commented post from that topic's own period.

Method, established by testing (docs/SAMPLING_DESIGN.md):
  * Plain search, NO timescope. The default 综合 ranking surfaces the viral post;
    adding timescope switches to chronological listing and the viral post drops
    off page 1 entirely (measured: 4,585 comments -> 1).
  * Filter the returned cards by date afterwards, keeping those within
    --window days of the hot-search date, then take the highest comment count.
  * Card dates come in three formats and all three must be handled:
    `YYYY年MM月DD日` (previous years), `MM月DD日` (current year),
    `今天 HH:MM` / `N分钟前`. Matching only the first silently drops every
    recent topic.

Also records the page's global top post even when it is out of period, so the
gap between "best in period" and "best overall" is visible in the output.

Safety: randomized delay, a per-session request cap, and a clean stop the moment
weibo 302s to login. Resumable — rerun and it continues from the CSV.

Usage:
  python scripts/find_posts.py TOPICS.jsonl OUT.csv [--limit N] [--delay 8 15]
"""
import argparse
import csv
import datetime as dt
import html as H
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
from lib.weibo import read_csv_bom, write_csv_bom, is_blocked, url_to_mid
from probe_comments import build_headers, HTML_COMMENT_RE, ACT_RE, LI_RE, DIGITS_RE, TAG_RE, LINK_RE

CARD_RE = re.compile(r'<div class="card-wrap".*?(?=<div class="card-wrap"|\Z)', re.DOTALL)
# the post's own text, already present in the search card — capturing it costs
# nothing extra and gives every topic a description, where weibo's own
# description field covers only ~41%.
TXT_RE = re.compile(r'<p[^>]*class="txt"[^>]*>(.*?)</p>', re.DOTALL)
ZW_RE = re.compile(r'[\u200b\u200c\u200d\ufeff]')
DATE_FULL = re.compile(r'(\d{4})年(\d{1,2})月(\d{1,2})日')
DATE_MD = re.compile(r'(?<!\d)(\d{1,2})月(\d{1,2})日')
DATE_TODAY = re.compile(r'今天\s*\d{1,2}:\d{2}')
DATE_REL = re.compile(r'\d+\s*(分钟|小时)前')

TOPN = int(os.environ.get('FIND_TOPN', '5'))
# The top N in-period posts, not just the best one: they arrive in the same
# search response, so recording only the first throws away free data. Which of
# them actually get their comments scraped is a later decision.
FIELDS = (['topic', 'date', 'weibo_cat', 'peak_hot', 'max_discuss', 'best_rank',
           'status', 'n_cards', 'n_dated', 'n_inperiod']
          + [f'p{i}_{k}' for i in range(1, TOPN + 1)
             for k in ('comments', 'url', 'mid', 'date', 'text')]
          + ['inperiod_comment_sum', 'global_top_comments', 'global_top_date'])


def card_date(card, today):
    m = DATE_FULL.search(card)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = DATE_MD.search(card)
    if m:
        try:
            d = dt.date(today.year, int(m.group(1)), int(m.group(2)))
            return d if d <= today else dt.date(today.year - 1, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    if DATE_TODAY.search(card) or DATE_REL.search(card):
        return today
    return None


def card_info(card):
    """(comment_count, url) for one result card, or None."""
    c = HTML_COMMENT_RE.sub('', card)          # drop the commented-out 收藏 li first
    act = ACT_RE.search(c)
    if not act:
        return None
    lis = LI_RE.findall(act.group(0))
    if len(lis) < 2:
        return None
    m = DIGITS_RE.search(TAG_RE.sub('', lis[1]))
    n = int(m.group(1).replace(',', '')) if m else 0
    link = LINK_RE.search(c)
    if not link:
        return None
    m2 = TXT_RE.search(c)
    body = ''
    if m2:
        body = ZW_RE.sub('', H.unescape(TAG_RE.sub('', m2.group(1))))
        body = ' '.join(body.split())[:400]
    return n, f'https://weibo.com/{link.group(1)}/{link.group(2)}', link.group(2), body


def search(topic, headers, timeout=(10, 30), attempts=5):
    """One search. Transient network faults are retried with backoff rather than
    allowed to kill a multi-day run — 'Connection reset by peer' happens perhaps
    once every few thousand requests and must not be fatal."""
    url = f"https://s.weibo.com/weibo?q={quote(topic)}&page=1"
    for i in range(attempts):
        try:
            r = requests.get(url, headers=headers, allow_redirects=False,
                             timeout=timeout)
        except requests.RequestException as e:
            if i == attempts - 1:
                return f'NETFAIL:{type(e).__name__}', ''
            time.sleep(min(60, 5 * 2 ** i))
            continue
        if is_blocked(r.status_code, r.headers.get('Location', '')):
            return 'BLOCKED', ''
        if r.status_code in (429, 500, 502, 503, 504):
            if i == attempts - 1:
                return f'HTTP{r.status_code}', ''
            time.sleep(min(120, 15 * 2 ** i))
            continue
        if r.status_code != 200:
            return f'HTTP{r.status_code}', ''
        if '抱歉，未找到相关结果' in r.text:
            return 'NORESULT', r.text
        return 'OK', r.text
    return 'NETFAIL', ''


def process(topic, html, window, today):
    d0 = dt.date.fromisoformat(topic['date'])
    cards = CARD_RE.findall(html)
    items = []
    for c in cards:
        info = card_info(c)
        if not info:
            continue
        n, url, mblogid, body = info
        items.append({'n': n, 'url': url, 'mblogid': mblogid, 'body': body,
                      'date': card_date(c, today)})
    dated = [x for x in items if x['date']]
    inper = sorted([x for x in dated if abs((x['date'] - d0).days) <= window],
                   key=lambda x: -x['n'])
    gbest = max(items, key=lambda x: x['n']) if items else None
    row = {'topic': topic['title'], 'date': topic['date'],
           'weibo_cat': topic.get('weibo_cat', ''), 'peak_hot': topic.get('peak_hot', ''),
           'max_discuss': topic.get('max_discuss', ''), 'best_rank': topic.get('best_rank', ''),
           'n_cards': len(cards), 'n_dated': len(dated), 'n_inperiod': len(inper),
           'inperiod_comment_sum': sum(x['n'] for x in inper[:TOPN]),
           'global_top_comments': gbest['n'] if gbest else '',
           'global_top_date': gbest['date'].isoformat() if gbest and gbest['date'] else ''}
    for i in range(1, TOPN + 1):
        x = inper[i - 1] if i <= len(inper) else None
        row[f'p{i}_comments'] = x['n'] if x else ''
        row[f'p{i}_url'] = x['url'] if x else ''
        row[f'p{i}_date'] = x['date'].isoformat() if x else ''
        row[f'p{i}_text'] = x.get('body', '') if x else ''
        try:
            row[f'p{i}_mid'] = url_to_mid(x['mblogid']) if x else ''
        except Exception:
            row[f'p{i}_mid'] = ''
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('topics')
    ap.add_argument('out')
    ap.add_argument('--limit', type=int, default=0, help='stop after N new requests this session')
    ap.add_argument('--delay', type=float, nargs=2, default=[4, 7],
                    help='randomized seconds between requests (default 4 7). '
                         'Measured: one cookie sustained 23,680 searches over 92 '
                         'continuous hours at 8-15s without a single challenge, so '
                         '4-7s (the rate the original probe used) has headroom.')
    ap.add_argument('--window', type=int, default=7, help='+-days counted as in-period')
    ap.add_argument('--cookie', default=str(Path.home() / 'weibo-corpus/secrets/cookie.txt'))
    a = ap.parse_args()

    headers = build_headers(Path(a.cookie).read_text(encoding='utf-8').strip())
    today = dt.date.today()
    topics = [json.loads(l) for l in open(a.topics, encoding='utf-8')]

    rows, done = [], set()
    if Path(a.out).exists():
        rows = read_csv_bom(a.out)
        done = {r['topic'] for r in rows}
        print(f'resuming: {len(done):,} topics already searched')

    todo = [t for t in topics if t['title'] not in done]
    if a.limit:
        todo = todo[:a.limit]
    print(f'{len(todo):,} to search, delay {a.delay[0]}-{a.delay[1]}s '
          f'(~{len(todo)*sum(a.delay)/2/3600:.1f}h)')

    t0 = time.time()
    hit = miss = 0
    # session endurance log: how many requests, over how long, before weibo blocks.
    # The often-quoted "400 searches" is a lower bound from a run that simply
    # finished at 400 — the real ceiling has never been measured.
    seslog = Path(a.out).with_suffix('.sessions.csv')
    for i, t in enumerate(todo, 1):
        st, html = search(t['title'], headers)
        if st == 'BLOCKED':
            el = time.time() - t0
            print(f'\nBLOCKED after {i-1} requests in {el/60:.1f} min '
                  f'({(i-1)/(el/3600):.0f} req/h) — re-login, refresh '
                  f'{a.cookie}, then rerun (it resumes).')
            new = not seslog.exists()
            with open(seslog, 'a', encoding='utf-8') as f:
                if new:
                    f.write('started,requests,minutes,req_per_hour,delay_lo,delay_hi,outcome\n')
                f.write(f'{dt.datetime.fromtimestamp(t0).isoformat(timespec="seconds")},'
                        f'{i-1},{el/60:.1f},{(i-1)/(el/3600):.0f},'
                        f'{a.delay[0]},{a.delay[1]},blocked\n')
            break
        if st.startswith('HTTP') or st.startswith('NETFAIL'):
            print(f'  {st} on {t["title"][:20]} — skipping, will retry on next run',
                  flush=True)
            time.sleep(20)
            continue
        row = process(t, html, a.window, today) if html else {
            'topic': t['title'], 'date': t['date'], 'n_cards': 0, 'n_dated': 0,
            'n_inperiod': 0}
        row['status'] = st
        rows.append({k: row.get(k, '') for k in FIELDS})
        hit += bool(row.get('p1_url'))
        miss += not row.get('p1_url')
        if i % 10 == 0 or i == len(todo):
            write_csv_bom(a.out, rows, FIELDS)
            el = time.time() - t0
            print(f'  [{i}/{len(todo)}] 命中 {hit} 未命中 {miss} '
                  f'({100*hit/(hit+miss):.0f}%)  {el/60:.1f}min  '
                  f'eta {(len(todo)-i)*el/i/60:.0f}min', flush=True)
        time.sleep(random.uniform(*a.delay))

    write_csv_bom(a.out, rows, FIELDS)
    el = time.time() - t0
    n_sess = hit + miss
    if n_sess:
        new = not seslog.exists()
        with open(seslog, 'a', encoding='utf-8') as f:
            if new:
                f.write('started,requests,minutes,req_per_hour,delay_lo,delay_hi,outcome\n')
            f.write(f'{dt.datetime.fromtimestamp(t0).isoformat(timespec="seconds")},'
                    f'{n_sess},{el/60:.1f},{n_sess/(el/3600):.0f},'
                    f'{a.delay[0]},{a.delay[1]},completed\n')
    print(f'\n{len(rows):,} rows -> {a.out}   本次会话 {n_sess} 次请求 / '
          f'{el/60:.0f} 分钟，命中 {hit}')
    print(f'会话记录 -> {seslog}')


if __name__ == '__main__':
    main()
