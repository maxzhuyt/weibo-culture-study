#!/usr/bin/env python
"""Label hot-search topics by category with an LLM, via OpenRouter.

Purpose is measurement, not filtering: we label everything in the window so we
can see what the data actually contains. Whether any category is later excluded
is a separate decision.

Eight classes are emitted rather than the six that maximise inter-annotator
agreement (C+L merge to X, G merges into E). Eight can always be collapsed to
six afterwards; six cannot be split back.

Output format is `id:letter` pairs on one line. That is ~9 output tokens per
topic against ~51 for a pretty-printed JSON array, and runs about 8x faster —
which is what actually matters over 60k topics.

Usage:
  KLAB_OPENROUTER_KEY=... python scripts/label_topics.py INPUT.jsonl OUTPUT.json
Input is one JSON object per line with: title, date, desc (may be empty).
Resumable: rerun and it skips titles already present in OUTPUT.
"""
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

KEY = os.environ['KLAB_OPENROUTER_KEY']
MODEL = os.environ.get('LABEL_MODEL', 'deepseek/deepseek-v4-flash-0731')
PROVIDER = os.environ.get('LABEL_PROVIDER', 'DeepInfra')
BATCH = int(os.environ.get('LABEL_BATCH', '25'))
WORKERS = int(os.environ.get('LABEL_WORKERS', '16'))

VALID = set('SNEGTPCL')

SYS = """你是中文社交媒体研究的标注员。给定一批微博热搜话题（含日期），判断每条属于哪一类。只能选一个字母。

S = 社会民生：事故、灾难伤亡、案件、纠纷、影响普通人的政策、医疗教育住房就业、消费维权、
    社会不公、性别/地域/阶层/代际对立、普通人的遭遇
N = 国际涉外：他国内部事务、国际冲突与战争、中国外交表态、中外关系摩擦、涉外争议、海外华人
E = 娱乐：明星动态、影视剧综艺、音乐、动漫、粉圈、网红八卦
G = 游戏电竞：游戏本身、电竞战队与赛事、游戏角色与更新
T = 体育：传统体育赛事、运动员竞技表现
P = 官方宣传：领导人活动、政府正面宣传、纪念日、表彰、国家工程成就、节日问候、"走基层""致敬"
C = 科技商业：科技产品与发布、互联网公司、财经股市、汽车、楼市行情
L = 生活文化：美食、穿搭、健康养生、萌宠、网络段子与梗、个人见闻趣事、无灾害的天气与天象

判定优先级（按顺序适用，先命中先归类）：
1. 涉及他国或中外关系的一律 N，即使内容是社会事件或官方表态。
2. 明星/运动员涉及违法或严重社会争议（偷税、家暴、诈骗、辱华）归 S。
3. 官方通报或处理某个具体社会事件（如"警方通报某案"）归 S；官方正面宣传归 P。
4. 电竞一律 G，不归 T 也不归 E。
5. 自然灾害造成人员伤亡或重大影响归 S；单纯天气、天象、无伤亡地震归 L。
6. 以上都不适用时，才在 E/T/C/L 中选择。

很多标题很短且没有说明文字。这时依据日期推断当时的语境，结合常识做出最合理的判断，
不要因为不确定就回避——必须给出一个字母。

只输出一行，格式为 序号:字母 用空格分隔，例如 0:S 1:E 2:N。不要任何其他文字。"""

_lock = threading.Lock()
_stats = {'in': 0, 'out': 0, 'cached': 0, 'done': 0, 'fail': 0}


def call(batch, attempt=0):
    lines = []
    for t in batch:
        d = f"（{t['desc'][:100]}）" if t.get('desc') else ''
        lines.append(f"{t['_i']}. [{t['date']}] {t['title']}{d}")
    # the model emits reasoning tokens before the answer (~1000 per batch) and
    # max_tokens covers reasoning + content, so a tight budget starves the answer
    # and returns finish_reason=length with empty content. Budget for both.
    body = {'model': MODEL, 'temperature': 0, 'max_tokens': 40 * len(batch) + 2500,
            'messages': [{'role': 'system', 'content': SYS},
                         {'role': 'user', 'content': '\n'.join(lines)}]}
    if PROVIDER:
        body['provider'] = {'order': [PROVIDER], 'allow_fallbacks': True}
    try:
        r = requests.post('https://openrouter.ai/api/v1/chat/completions',
                          headers={'Authorization': f'Bearer {KEY}',
                                   'Content-Type': 'application/json'},
                          json=body, timeout=(15, 90))
        r.raise_for_status()
        j = r.json()
        ch = (j.get('choices') or [{}])[0]
        txt = (ch.get('message') or {}).get('content')
        if not isinstance(txt, str) or not txt.strip():
            # reasoning ran long and starved the answer: the reasoning trace still
            # contains per-item verdicts like "0. 赵露思发长文回应：明星动态，E。"
            msg = ch.get('message') or {}
            rz = msg.get('reasoning') or ''
            if rz:
                got = dict(re.findall(r'(?:^|[。\n])\s*(\d+)\.[^。\n]*?([SNEGTPCL])。', rz))
                if got:
                    with _lock:
                        _stats['in'] += (j.get('usage') or {}).get('prompt_tokens', 0)
                        _stats['out'] += (j.get('usage') or {}).get('completion_tokens', 0)
                    return {int(k): v for k, v in got.items()}
            raise ValueError(f'no text content (finish_reason={ch.get("finish_reason")})')
        u = j.get('usage', {})
        out = {}
        for m in re.finditer(r'(\d+)\s*:\s*([A-Z])', txt):
            i, c = int(m.group(1)), m.group(2)
            if c in VALID:
                out[i] = c
        with _lock:
            _stats['in'] += u.get('prompt_tokens', 0)
            _stats['out'] += u.get('completion_tokens', 0)
            _stats['cached'] += (u.get('prompt_tokens_details') or {}).get('cached_tokens', 0)
        return out
    except Exception as e:
        if attempt < 4:
            time.sleep(3 * (attempt + 1))
            return call(batch, attempt + 1)
        with _lock:
            _stats['fail'] += len(batch)
        print(f'  batch failed: {type(e).__name__} {str(e)[:100]}', flush=True)
        return {}


def main():
    inp, outp = sys.argv[1], sys.argv[2]
    topics = [json.loads(l) for l in open(inp, encoding='utf-8')]
    done = {}
    if os.path.exists(outp):
        done = json.load(open(outp, encoding='utf-8'))
        print(f'resuming: {len(done):,} already labelled')
    todo = [t for t in topics if t['title'] not in done]
    for i, t in enumerate(todo):
        t['_i'] = i
    print(f'{len(topics):,} topics, {len(todo):,} to do, '
          f'{(len(todo)+BATCH-1)//BATCH:,} batches, {WORKERS} workers -> {MODEL} @ {PROVIDER}')
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for n, res in enumerate(ex.map(call, batches), 1):
            for b in batches[n - 1]:
                if b['_i'] in res:
                    done[b['title']] = res[b['_i']]
            with _lock:
                _stats['done'] = len(done)
            if n % 20 == 0 or n == len(batches):
                el = time.time() - t0
                rate = n / el
                print(f'  [{n}/{len(batches)}] {len(done):,} labelled  '
                      f'{el/60:.1f}min  eta {(len(batches)-n)/rate/60:.0f}min  '
                      f'cache {100*_stats["cached"]/max(_stats["in"],1):.0f}%', flush=True)
                json.dump(done, open(outp, 'w'), ensure_ascii=False)
    json.dump(done, open(outp, 'w'), ensure_ascii=False)
    cost = _stats['in'] / 1e6 * 0.08 + _stats['out'] / 1e6 * 0.18
    print(f'\n{len(done):,} labelled, {_stats["fail"]} failed, {(time.time()-t0)/60:.1f} min')
    print(f'tokens in {_stats["in"]:,} (cached {_stats["cached"]:,}) out {_stats["out"]:,}  ~${cost:.2f}')
    from collections import Counter
    print('distribution:', dict(Counter(done.values()).most_common()))


if __name__ == '__main__':
    main()
