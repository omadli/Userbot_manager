"""
Random Uzbek-word generator for NamePool entries.

Pulls one-word-per-line wordlists from the upstream `omadli/uzbek-wordlist`
GitHub repo, caches them under MEDIA_ROOT/wordlists/, and combines random
picks into multi-word group names.

Public surface: `generate_names()` — sync, safe to call from sync_to_async.
"""
from __future__ import annotations

import random
import urllib.request
from pathlib import Path

from django.conf import settings


_URLS = {
    'latin': 'https://raw.githubusercontent.com/omadli/uzbek-wordlist/main/wordlist-latin.txt',
    'cyrillic': 'https://raw.githubusercontent.com/omadli/uzbek-wordlist/main/wordlist-cyrillic.txt',
}

_CACHE: dict[str, list[str]] = {}


def _cache_path(script: str) -> Path:
    base = Path(settings.MEDIA_ROOT) / 'wordlists'
    base.mkdir(parents=True, exist_ok=True)
    return base / f'uzbek-{script}.txt'


def _download_if_missing(script: str) -> Path:
    path = _cache_path(script)
    if path.exists() and path.stat().st_size > 1024:
        return path
    url = _URLS[script]
    req = urllib.request.Request(url, headers={'User-Agent': 'userbot-manager/1.0'})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed allow-list
        data = resp.read()
    path.write_bytes(data)
    return path


def _load_words(script: str, min_len: int = 4, max_len: int = 14) -> list[str]:
    cache_key = f'{script}:{min_len}:{max_len}'
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached
    path = _download_if_missing(script)
    text = path.read_text(encoding='utf-8')
    words = [
        w.strip() for w in text.splitlines()
        if w.strip() and min_len <= len(w.strip()) <= max_len
    ]
    _CACHE[cache_key] = words
    return words


def _apply_case(word: str, case: str) -> str:
    if case == 'title':
        return word.capitalize()
    if case == 'upper':
        return word.upper()
    if case == 'lower':
        return word.lower()
    return word


def generate_names(
    count: int,
    *,
    words_per_name: int = 2,
    script: str = 'latin',
    case: str = 'title',
    separator: str = ' ',
    min_word_len: int = 4,
    max_word_len: int = 14,
) -> list[str]:
    """Return up to `count` unique random group names.

    The wordlist is downloaded on first use and cached on disk + in memory.
    Duplicates within the generated batch are skipped; if the requested
    combinations are too few to fill `count`, returns whatever was made.
    """
    if script not in _URLS:
        script = 'latin'
    words = _load_words(script, min_len=min_word_len, max_len=max_word_len)
    if not words:
        return []

    words_per_name = max(1, min(words_per_name, 5))
    rng = random.SystemRandom()

    seen: set[str] = set()
    out: list[str] = []
    max_attempts = max(count * 5, 50)
    for _ in range(max_attempts):
        if len(out) >= count:
            break
        chosen = [_apply_case(rng.choice(words), case) for _ in range(words_per_name)]
        name = separator.join(chosen)
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out
