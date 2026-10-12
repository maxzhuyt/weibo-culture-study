#!/usr/bin/env python
"""Fetch weibotop.cn daily hot-search archives (2019-10-26 → today).

Why this source: it polls the board roughly every 2 minutes (~720 snapshots/day
against our own archive's ~22), covers 2,517 days with only 4 missing, and
publishes peak hot, best rank and appearance count per topic. Its daily page
lists the day's top 21.

Its ordering rule is undocumented, so it was recovered empirically and verified
on 8 days x 21 items with zero errors:

    sort by (best rank achieved that day ASC, peak hot DESC)

That is a *relative* criterion, and it matters: hot is not comparable across
hours. At a fixed board position the hot value swings 9-14x over a day (rank 1
is ~11万 at 04:00 and ~161万 at 22:00), so ranking by absolute hot
systematically over-selects evening topics. Rank normalises that away.

Plain HTTP is enough — the listing is server-rendered. Each row is parsed from
its <li> markup rather than from flattened page text, because the surrounding
copy ("展示全天综合前 21 条") otherwise swallows row 2.

Usage:
  python scripts/fetch_weibotop.py OUT.csv [--start 2019-10-26] [--end 2026-09-19]
                                           [--delay 6 10] [--limit N]
Resumable: rerun and it continues from OUT.csv.
"""
import argparse
import csv
import datetime as dt
import html as H
import os
import random
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.weibo import read_csv_bom, write_csv_bom

BASE = 'http://www.weibotop.cn/daily/'
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36')
# a first-party client id the site issues to ordinary browsers; sending it makes
# a long run look like a returning visitor rather than a stream of cold clients
CLIENT = os.environ.get('WT_CLIENT', '')

COMMENT = re.compile(r'<!--.*?-->', re.DOTALL)
LI = re.compile(r'<li class="border-b[^"]*">(.*?)</li>', re.DOTALL)
ITEM = re.compile(
    r'href="(/topic/[^"]+)".*?'
    r'tabular-nums[^>]*>(\d+)</span>.*?'
    r'group-hover/item:text-primary">(.*?)</div>.*?'
    r'最佳第\s*(\d+)\s*名\s*·\s*记录\s*(\d+)\s*次', re.DOTALL)
# anchor on the 峰值 label: a title such as 「医保局回应一针药卖70万」 otherwise
# supplies the first 万-number in the row and is read as the hot value
HOT = re.compile(r'峰值\s*([\d.]+)\s*万')
TOPROW = re.compile(r'全天综合榜首\s+(\S.{0,34}?)\s+([\d.]+)万\s*峰值热度\s*'
                    r'最佳排名第\s*(\d+)\s*位\s*·\s*当天记录\s*(\d+)\s*次')
NSNAP = re.compile(r'(\d+)\s*次微博热搜榜快照')
NTOPIC = re.compile(r'共记录\s*(\d+)\s*个独立话题')
TAG = re.compile(r'<[^>]+>')

FIELDS = ['date', 'pos', 'title', 'topic_id', 'best_rank', 'records',
          'peak_hot', 'n_snapshots', 'n_topics_day']


def parse(html, date):
    body = COMMENT.sub('', html)
    rows = []
    for li in LI.findall(body):
        m = ITEM.search(li)
        if not m:
            continue
        hot = HOT.search(li)
        rows.append({
            'date': date, 'pos': int(m.group(2)),
            'title': H.unescape(TAG.sub('', m.group(3))).strip(),
            'topic_id': m.group(1).rsplit('/', 1)[-1],
            'best_rank': int(m.group(4)), 'records': int(m.group(5)),
            'peak_hot': int(float(hot.group(1)) * 10000) if hot else '',
        })
    flat = ' '.join(H.unescape(TAG.sub(' ', COMMENT.sub(
        '', re.sub(r'<script.*?</script>', '', html, flags=re.DOTALL)))).split())
    m0 = TOPROW.search(flat)
    if m0:
        rows.append({'date': date, 'pos': 1, 'title': m0.group(1).strip(),
                     'topic_id': '', 'best_rank': int(m0.group(3)),
                     'records': int(m0.group(4)),
                     'peak_hot': int(float(m0.group(2)) * 10000)})
    ns = NSNAP.search(flat)
    nt = NTOPIC.search(flat)
    dedup = {}
    for r in rows:                      # the 榜首 block repeats row 1
        dedup.setdefault(r['pos'], r)
    out = [dedup[k] for k in sorted(dedup)]
    for r in out:
        r['n_snapshots'] = int(ns.group(1)) if ns else ''
        r['n_topics_day'] = int(nt.group(1)) if nt else ''
    return out


def fetch(sess, date, attempts=4):
    for i in range(attempts):
        try:
            r = sess.get(BASE + date, timeout=(10, 40))
        except requests.RequestException as e:
            if i == attempts - 1:
                return 'NETFAIL', []
            time.sleep(min(90, 8 * 2 ** i))
            continue
        if r.status_code == 404:
            return 'MISSING', []
        if r.status_code != 200:
            if i == attempts - 1:
                return f'HTTP{r.status_code}', []
            time.sleep(min(120, 15 * 2 ** i))
            continue
        rows = parse(r.text, date)
        return ('OK' if rows else 'EMPTY'), rows
    return 'NETFAIL', []


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('out')
    ap.add_argument('--start', default='2019-10-26')
    ap.add_argument('--end', default=dt.date.today().isoformat())
    ap.add_argument('--delay', type=float, nargs=2, default=[6, 10])
    ap.add_argument('--limit', type=int, default=0)
    a = ap.parse_args()

    sess = requests.Session()
    sess.headers.update({'User-Agent': UA, 'Accept-Language': 'zh-CN,zh;q=0.9',
                         'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8'})
    if CLIENT:
        sess.headers['Cookie'] = f'wt_client={CLIENT}'

    rows, done = [], set()
    if Path(a.out).exists():
        rows = read_csv_bom(a.out)
        done = {r['date'] for r in rows}
        print(f'resuming: {len(done):,} days already fetched')

    d0, d1 = dt.date.fromisoformat(a.start), dt.date.fromisoformat(a.end)
    todo = [(d0 + dt.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]
    todo = [d for d in todo if d not in done]
    if a.limit:
        todo = todo[:a.limit]
    print(f'{len(todo):,} days to fetch, delay {a.delay[0]}-{a.delay[1]}s '
          f'(~{len(todo)*sum(a.delay)/2/3600:.1f}h)')

    t0 = time.time()
    ok = miss = bad = 0
    for i, d in enumerate(todo, 1):
        st, got = fetch(sess, d)
        if st == 'OK':
            rows += got
            ok += 1
        elif st == 'MISSING':
            miss += 1
        else:
            bad += 1
            print(f'  {st} on {d}', flush=True)
        if i % 25 == 0 or i == len(todo):
            write_csv_bom(a.out, rows, FIELDS)
            el = time.time() - t0
            print(f'  [{i}/{len(todo)}] ok {ok} 缺 {miss} 异常 {bad}  '
                  f'{len(rows):,} 行  {el/60:.0f}min  '
                  f'eta {(len(todo)-i)*el/i/60:.0f}min', flush=True)
        time.sleep(random.uniform(*a.delay))
    write_csv_bom(a.out, rows, FIELDS)
    print(f'\n{len(rows):,} 行 / {ok} 天 -> {a.out}  (缺 {miss}, 异常 {bad})')


if __name__ == '__main__':
    main()
