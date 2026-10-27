"""Bounded, multi-position sampling of a reference answer.

The sampler is deterministic and performs no model or file I/O. Long answers
are deliberately shown as separated excerpts, never as a contiguous narrative.
"""

from __future__ import annotations


_BOUNDARIES = ("\n\n", "\n", "。", "！", "？", "；")
_LABELS = ("【开头片段】", "【中部片段】", "【原文末段】")
_OMISSION = "【中间省略】"
_SEPARATOR = "\n\n"


def _validate_budget(max_chars):
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")


def _end_at_boundary(text, start, end):
    """Shorten an excerpt at a nearby boundary without moving its start."""
    if end - start <= 1:
        return end
    floor = max(start + 1, end - 80)
    candidates = []
    for marker in _BOUNDARIES:
        found = text.rfind(marker, floor, end)
        if found >= 0 and found + len(marker) < end:
            candidates.append(found + len(marker))
    return max(candidates) if candidates else end


def _start_at_boundary(text, start, end):
    """Advance to a nearby boundary without moving an excerpt beyond its end."""
    if end - start <= 1:
        return start
    ceiling = min(end - 1, start + 80)
    candidates = []
    for marker in _BOUNDARIES:
        found = text.find(marker, start, ceiling)
        if found >= 0 and found + len(marker) < end:
            candidates.append(found + len(marker))
    return min(candidates) if candidates else start


def _bounded_excerpt(text, start, end, *, start_boundary=False, end_boundary=False):
    """Keep an excerpt inside ``[start, end)`` and never return an empty slice."""
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    if start >= end:
        return ""
    if start_boundary:
        start = _start_at_boundary(text, start, end)
    if end_boundary:
        end = _end_at_boundary(text, start, end)
    return text[start:end]


def _content_lengths(content_budget):
    """Allocate approximately 40/30/30 while using every available character."""
    lengths = [max(1, int(content_budget * weight)) for weight in (0.4, 0.3, 0.3)]
    while sum(lengths) > content_budget:
        index = max(range(3), key=lambda item: lengths[item])
        lengths[index] -= 1
    while sum(lengths) < content_budget:
        lengths[0] += 1
    return lengths


def sample_reference_narrative(answer, max_chars=3000):
    """Return a bounded opening/middle/tail sample of *answer*.

    A short answer is returned unchanged apart from surrounding whitespace.
    Long-answer labels and omission markers make missing material explicit.
    """
    _validate_budget(max_chars)
    if not answer:
        return ""
    text = str(answer).strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text

    # Three labels, three excerpts and two omission markers are eight rendered
    # components, so joining them puts seven separators in the final string.
    scaffold = [_LABELS[0], "", _OMISSION, _LABELS[1], "", _OMISSION, _LABELS[2], ""]
    fixed = len(_SEPARATOR.join(scaffold))
    if max_chars <= fixed + 3:
        # There is insufficient room to describe separated excerpts safely.
        return text[:max_chars]

    head_length, middle_length, tail_length = _content_lengths(max_chars - fixed)
    n = len(text)
    head_start, head_end = 0, head_length
    tail_start, tail_end = n - tail_length, n
    # n > max_chars >= fixed + the three lengths, so this centered middle
    # window fits strictly between head and tail without overlap.
    middle_start = max(head_end, n // 2 - middle_length // 2)
    middle_start = min(middle_start, tail_start - middle_length)
    middle_end = middle_start + middle_length

    head = _bounded_excerpt(text, head_start, head_end, end_boundary=True)
    middle = _bounded_excerpt(text, middle_start, middle_end,
                              start_boundary=True, end_boundary=True)
    # The tail remains anchored at n. Do not advance it past a final marker.
    tail = _bounded_excerpt(text, tail_start, tail_end, start_boundary=True)

    result = _SEPARATOR.join((_LABELS[0], head, _OMISSION, _LABELS[1], middle,
                              _OMISSION, _LABELS[2], tail))
    if len(result) > max_chars:
        raise AssertionError("reference sample exceeded max_chars")
    return result
