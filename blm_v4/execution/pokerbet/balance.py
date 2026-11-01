"""Reading the account balance without inventing one.

Measured failure, 2026-10-30: the header reader returned

    15:02 R47.81 · 15:33 R0.06 (six reads) · 16:26 R100.06 · 16:29 R125.89
    16:30 R93.47 · 16:31 R60.77 · 16:48 R150.57

inside two hours with no settled bets in between.  The old implementation took
the first number it could find and returned it, so a half-rendered widget became
a real balance — and because the executor stakes the whole balance when it is
below the unit, a phantom R0.06 produced a real R0.06 bet.

The rules here are deliberately conservative:

  * a value must be seen at least twice, agreeing exactly, before it is used;
  * a value outside the recent range must be seen THREE times (a genuine
    withdrawal is rare and can afford an extra read; a render glitch cannot);
  * nothing parseable, no value — the caller then refuses to bet rather than
    sizing on a guess.

Pure functions: the DOM sampling lives in the adapter, so all of this is
testable without a browser.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence, Tuple

#: two agreeing samples are enough inside this ratio of the last good value
AGREE_SAMPLES = 2
#: ... and three when the value moved further than this (suspicious jump)
SUSPECT_SAMPLES = 3
SUSPECT_RATIO = 0.5     # |new - last| > 50% of last  ->  suspicious

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def parse_balance_text(text: Optional[str]) -> Optional[float]:
    """The balance from the header's raw text, or None.

    Takes the LAST number in the string: PokerBet renders the currency code
    first and the amount after it, and a partially rendered element can leave
    an earlier fragment ("R 0.0" of "R 0.06") in front of the real value.
    Accepts "1 234.56", "1,234.56", "R1234.56".
    """
    if not text:
        return None
    cleaned = text.replace("\u00a0", " ")
    # a thousands separator can be a space: "1 234.56" -> "1234.56"
    cleaned = re.sub(r"(?<=\d)\s(?=\d{3}\b)", "", cleaned)
    nums = _NUM.findall(cleaned)
    if not nums:
        return None
    raw = nums[-1].replace(",", "").replace(" ", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _suspicious(candidate: float, last_good: Optional[float]) -> bool:
    if last_good is None or last_good <= 0:
        return False
    return abs(candidate - last_good) > last_good * SUSPECT_RATIO


def decide(samples: Sequence[Optional[float]],
           last_good: Optional[float] = None) -> Tuple[Optional[float], str]:
    """(balance, reason).  ``balance`` is None whenever it cannot be proven.

    ``samples`` is every value parsed in order from one read attempt.  Reasons
    are stable strings so a log line says which rule refused.
    """
    usable = [s for s in samples if s is not None]
    if not usable:
        return None, "unreadable"

    candidate = usable[-1]
    agree = sum(1 for s in usable if s == candidate)

    if _suspicious(candidate, last_good):
        if agree >= SUSPECT_SAMPLES:
            return candidate, "confirmed_after_jump"
        return None, f"suspect_jump({candidate} vs {last_good})"

    if agree >= AGREE_SAMPLES:
        return candidate, "confirmed"

    return None, f"disagreement({usable})"


def describe(samples: Iterable[Optional[float]]) -> str:
    """A compact form for the log: the values that were seen, in order."""
    return "[" + ", ".join("None" if s is None else f"{s:.2f}" for s in samples) + "]"
