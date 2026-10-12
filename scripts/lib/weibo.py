"""Shared helpers for the weibo-corpus pipeline.

base62_decode / url_to_mid are ported from the vendored upstream
(vendor/WeiboSpider/weibospider/spiders/common.py) so the two agree exactly:
the mblogid is chunked 4 characters at a time from the right, each chunk
base62-decoded, and every chunk after the first zero-filled to 7 digits.
mid_to_url is the inverse (7-digit chunks -> 4-character groups).
"""
import csv
import re

ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ZERO_WIDTH = re.compile('[​‌‍﻿]')


def base62_decode(string):
    s = str(string)
    num = 0
    for idx, char in enumerate(s):
        num += ALPHABET.index(char) * (62 ** (len(s) - idx - 1))
    return num


def base62_encode(num):
    num = int(num)
    if num == 0:
        return '0'
    out = ''
    while num:
        num, rem = divmod(num, 62)
        out = ALPHABET[rem] + out
    return out


def url_to_mid(url):
    """mblogid -> numeric mid.  url_to_mid('z0JH2lOMb') == 3501756485200075"""
    s = str(url)
    chunks = [s[max(i - 4, 0):i] for i in range(len(s), 0, -4)]
    chunks.reverse()
    out = []
    for i, chunk in enumerate(chunks):
        part = str(base62_decode(chunk))
        if i > 0 and len(part) < 7:
            part = '0' * (7 - len(part)) + part
        out.append(part)
    return int(''.join(out))


def mid_to_url(mid):
    """numeric mid -> mblogid (inverse of url_to_mid)."""
    s = str(mid)
    chunks = [s[max(i - 7, 0):i] for i in range(len(s), 0, -7)]
    chunks.reverse()
    out = []
    for i, chunk in enumerate(chunks):
        part = base62_encode(int(chunk))
        if i > 0 and len(part) < 4:
            part = '0' * (4 - len(part)) + part
        out.append(part)
    return ''.join(out)


def strip_zero_width(s):
    """Weibo content carries zero-width characters; strip them before analysis."""
    return _ZERO_WIDTH.sub('', s or '')


def is_blocked(status, location):
    """A soft-blocked cookie 302s to login.sina.com.cn (notes §3)."""
    return int(status) in (301, 302) and 'login.sina' in (location or '')


def count_blocks(log_path):
    """Count block signatures in a scrapy log.

    RedirectMiddleware is disabled and scrapy does not log the redirect
    Location, so the '302' line is the signal - grepping for 'login.sina'
    finds nothing (notes §3). More than ~8 means the cookie is blocked.
    """
    n = 0
    with open(log_path, 'rt', encoding='utf-8', errors='replace') as f:
        for line in f:
            if 'Ignoring response <302' in line:
                n += 1
    return n


def read_csv_bom(path):
    """Read a CSV written with a BOM (all outputs here are utf-8-sig)."""
    with open(path, 'rt', encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def write_csv_bom(path, rows, fieldnames):
    with open(path, 'wt', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
