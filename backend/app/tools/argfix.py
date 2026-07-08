from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def _parse_path(key: str) -> list:
    tokens: list = []
    for m in _TOKEN_RE.finditer(key):
        if m.group(1) is not None:
            tokens.append(m.group(1))
        else:
            tokens.append(int(m.group(2)))
    return tokens


def _ensure_index(lst: list, idx: int) -> None:
    while len(lst) <= idx:
        lst.append(None)


def unflatten_args(args: dict) -> dict:
    """Rebuild nested tool arguments from flattened dot/bracket keys.

    Some models (notably gemini via GitHub Copilot) serialize nested tool-call
    arguments as flat keys like ``sheets[0].chart.title`` or ``slides[0].bullets[0]``
    instead of proper nested objects/arrays, which breaks tools expecting structured
    input. This reconstructs the intended structure. Returns ``args`` unchanged when no
    flattened keys are present.
    """
    if not isinstance(args, dict):
        return args
    if not any(("." in k) or ("[" in k) for k in args.keys()):
        return args

    root: dict = {}
    for key, value in args.items():
        tokens = _parse_path(key)
        if not tokens:
            continue
        cur = root
        for i in range(len(tokens) - 1):
            tok = tokens[i]
            want_list = isinstance(tokens[i + 1], int)
            if isinstance(tok, int):
                if not isinstance(cur, list):
                    break
                _ensure_index(cur, tok)
                if cur[tok] is None:
                    cur[tok] = [] if want_list else {}
                cur = cur[tok]
            else:
                if not isinstance(cur, dict):
                    break
                if cur.get(tok) is None:
                    cur[tok] = [] if want_list else {}
                cur = cur[tok]
        else:
            last = tokens[-1]
            if isinstance(last, int) and isinstance(cur, list):
                _ensure_index(cur, last)
                cur[last] = value
            elif not isinstance(last, int) and isinstance(cur, dict):
                cur[last] = value

    # Drop any None placeholders left in reconstructed lists.
    def _clean(obj):
        if isinstance(obj, list):
            return [_clean(x) for x in obj if x is not None]
        if isinstance(obj, dict):
            return {k: _clean(v) for k, v in obj.items()}
        return obj

    return _clean(root)
