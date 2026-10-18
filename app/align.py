"""Line up transcript word timings with the source words of a chunk."""
from __future__ import annotations

from difflib import SequenceMatcher

from .ingest import norm


def align(tokens: list[tuple[int, str]], heard: list[tuple[str, float, float]], duration: float):
    """tokens: [(word_index, spoken_text)] in reading order.
    heard:  [(word, start, end)] from transcription.
    Returns [[word_index, start, end], ...] covering every token, plus the match ratio."""
    n = len(tokens)
    if n == 0:
        return [], 1.0
    src = [norm(t) for _, t in tokens]
    hyp = [norm(w) for w, _, _ in heard]
    times: list[list[float] | None] = [None] * n

    if heard:
        for i, j in _dp_pairs(src, hyp):
            times[i] = [heard[j][1], heard[j][2]]
        _fill_multiword(times, src, hyp, heard)

    matched = sum(1 for t in times if t)
    ratio = matched / max(1, sum(1 for s in src if s))

    if ratio < 0.25:  # transcript unusable: spread evenly by length
        lead = heard[0][1] if heard else 0.15
        tail = heard[-1][2] if heard else max(duration - 0.15, lead + 0.1)
        if tail <= lead:
            lead, tail = 0.1, max(duration - 0.1, 0.2)
        times = [None] * n
        _fill(times, tokens, -1, n, lead, tail)
        return [[tokens[i][0], round(t[0], 3), round(t[1], 3)] for i, t in enumerate(times)], ratio

    # enforce monotonic order, then interpolate the gaps by character length
    last = 0.0
    for i, t in enumerate(times):
        if t:
            if t[0] < last:
                times[i] = None
            else:
                last = t[1]
    anchors = [i for i, t in enumerate(times) if t]
    if not anchors:
        _fill(times, tokens, -1, n, 0.1, max(duration - 0.1, 0.2))
    else:
        first = anchors[0]
        if first > 0:
            start = max(0.0, times[first][0] - 0.35 * first)
            _fill(times, tokens, -1, first, start, times[first][0])
        for a, b in zip(anchors, anchors[1:]):
            if b - a > 1:
                _fill(times, tokens, a, b, times[a][1], times[b][0])
        lastA = anchors[-1]
        if lastA < n - 1:
            end = min(duration, times[lastA][1] + 0.4 * (n - 1 - lastA)) if duration else times[lastA][1] + 0.4 * (n - 1 - lastA)
            _fill(times, tokens, lastA, n, times[lastA][1], max(end, times[lastA][1] + 0.05))
    return [[tokens[i][0], round(t[0], 3), round(t[1], 3)] for i, t in enumerate(times)], ratio


def _fill(times, tokens, a, b, t0, t1):
    """Spread tokens a+1 .. b-1 across [t0, t1] proportional to their length."""
    idx = list(range(a + 1, b))
    if not idx:
        return
    lens = [max(1, len(tokens[i][1])) + 1 for i in idx]
    total = sum(lens)
    span = max(0.0, t1 - t0)
    t = t0
    for i, L in zip(idx, lens):
        d = span * L / total
        times[i] = [t, t + d]
        t += d


def _sim(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    if len(a) > 3 and len(b) > 3 and (a.startswith(b) or b.startswith(a)):
        return 0.8
    return SequenceMatcher(None, a, b).ratio()


def _dp_pairs(src: list[str], hyp: list[str]):
    """Monotonic word alignment (global, Needleman-Wunsch style) that tolerates
    misheard and missing words. Returns [(src_index, hyp_index)] for matched pairs."""
    n, m = len(src), len(hyp)
    GAP = -0.4
    # score matrix kept as rows of floats; back-pointers as bytes
    prev = [GAP * j for j in range(m + 1)]
    back = [bytearray(m + 1) for _ in range(n + 1)]
    for j in range(1, m + 1):
        back[0][j] = 2
    sim_cache = {}
    for i in range(1, n + 1):
        cur = [GAP * i] + [0.0] * m
        back[i][0] = 1
        a = src[i - 1]
        for j in range(1, m + 1):
            key = (a, hyp[j - 1])
            sc = sim_cache.get(key)
            if sc is None:
                sc = _sim(a, hyp[j - 1])
                sc = sc if sc >= 0.6 else -0.6
                sim_cache[key] = sc
            d = prev[j - 1] + sc
            u = prev[j] + GAP
            l = cur[j - 1] + GAP
            if d >= u and d >= l:
                cur[j], back[i][j] = d, 0
            elif u >= l:
                cur[j], back[i][j] = u, 1
            else:
                cur[j], back[i][j] = l, 2
        prev = cur
    pairs, i, j = [], n, m
    while i > 0 and j > 0:
        b = back[i][j]
        if b == 0:
            if _sim(src[i - 1], hyp[j - 1]) >= 0.6:
                pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif b == 1:
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    return pairs


def _fill_multiword(times, src, hyp, heard):
    """A lone unmatched source word between anchors whose gap holds several heard words
    (e.g. '1453' read as 'fourteen fifty three') takes the whole span."""
    n = len(times)
    i = 0
    while i < n:
        if times[i] is None and src[i]:
            prev_end = times[i - 1][1] if i > 0 and times[i - 1] else None
            nxt = i + 1
            if nxt < n and times[nxt] and prev_end is not None and (nxt - i) == 1:
                inside = [h for h in heard if h[1] >= prev_end - 1e-3 and h[2] <= times[nxt][0] + 1e-3]
                if inside:
                    times[i] = [inside[0][1], inside[-1][2]]
        i += 1
