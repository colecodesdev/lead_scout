"""Lead-quality filters for campaign mode (feature 06).

Drops two categories of business before they reach the discover/audit
stages and burn API quota:

1. **Dead listings** — businesses with no review activity. A literal
   zero-review listing is almost always a closed/abandoned location
   that Google Maps still has on file. Filtering at `review_count == 0`
   by default removes them while keeping brand-new businesses (which
   are prime leads, often without a website yet).

2. **Big franchise chains** — McDonald's, Subway, Starbucks, etc.
   These already have corporate websites and aren't pitchable as web
   design clients, regardless of how each individual location looks
   in Places.

This module is pure logic. It returns `(kept, dropped)` tuples; callers
decide what to do with the dropped list (log, count, persist for review).
The campaign command counts both buckets in its `CampaignSummary`.

Note on persistence: `merge_business` in storage.py overwrites only with
truthy values, which means a business previously enriched with a
nonzero `review_count` is NOT clobbered by a fresh API call returning
zero. So a transient Places hiccup (returning empty stats for a known
business) won't accidentally erase prior data — the new zero-count
arrival just gets filtered here before it could overwrite anything.
"""

import logging
import re

from leadscout.models import Business

logger = logging.getLogger(__name__)


# Compile once at module load. Used to normalize business names before
# substring-matching against the chain block-list. Strips any character
# that isn't a word char (\w covers letters/digits/_) or whitespace,
# so apostrophes, ampersands, hashes, and the like all flatten out.
# Example: "McDonald's #4521" -> "McDonalds 4521" (lowercased -> "mcdonalds 4521").
_NAME_NORMALIZE_RE = re.compile(r"[^\w\s]+")
# Collapse any run of whitespace to a single space, then strip ends.
# Two passes (first the punctuation strip, then this) keep the regexes
# simple and individually readable.
_WHITESPACE_COLLAPSE_RE = re.compile(r"\s+")


def _normalize_name(name: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace.

    Result is what we substring-match against `BLOCKED_CHAIN_NAMES`.
    Pure function, fully deterministic. No I/O.
    """
    # str.lower() handles Unicode case folding adequately for English
    # franchise names; for non-ASCII names we'd want str.casefold() but
    # the block-list itself is ASCII so .lower() is fine here.
    lowered = name.lower()
    # Strip punctuation outright (replace with empty string, not a space).
    # Empty replacement is what lets "Mc-Donald's" collapse to "mcdonalds":
    # the apostrophe, dash, and hash all vanish so "mcdonald" is a clean
    # substring. Replacing with a space would leave "mc donald s" instead,
    # and the chain block-list's "mcdonald" fragment would miss it.
    # Existing inter-word spaces survive because \s is preserved by the
    # negated character class — "Joe's BBQ & Bar" -> "joes bbq  bar".
    no_punct = _NAME_NORMALIZE_RE.sub("", lowered)
    # Collapse multi-space runs the previous step may have produced
    # (e.g., a removed ampersand leaving two spaces around it) and trim ends.
    return _WHITESPACE_COLLAPSE_RE.sub(" ", no_punct).strip()


def _is_blocked_chain(
    name: str, blocked_chain_names: frozenset[str]
) -> bool:
    """Return True if any blocked-chain fragment is a substring of the
    normalized name. Substring (not equality) because franchise names
    appear in many forms: "McDonald's #4521", "Subway Restaurants",
    "Starbucks Reserve", "Wendy's Old Fashioned Hamburgers", etc.
    """
    normalized = _normalize_name(name)
    # `any(... for ...)` short-circuits on the first match, so for a
    # name that doesn't start with a chain prefix we only walk the set
    # in the worst case.
    return any(fragment in normalized for fragment in blocked_chain_names)


def filter_live_businesses(
    businesses: list[Business],
    *,
    min_review_count: int = 1,
    blocked_chain_names: frozenset[str] | None = None,
) -> tuple[list[Business], list[Business]]:
    """Partition businesses into (kept, dropped) by lead-quality filters.

    Pure function — no I/O, no mutation of inputs. Both lists together
    contain every input business exactly once, in input order.

    Args:
        businesses: Input list, typically straight from `search_places`.
        min_review_count: Drop any business whose `review_count` is
            strictly less than this. Default 1 drops only literal
            zero-review listings. Pass 0 to disable the dead-listing
            filter entirely (every input passes the review check).
        blocked_chain_names: Optional frozenset of normalized substring
            fragments. Each business's name is normalized (lowercased,
            punctuation stripped, whitespace collapsed) and dropped if
            any fragment is a substring. Pass None or an empty set to
            disable the chain filter.

    Returns:
        Tuple `(kept, dropped)` of disjoint Business lists.
    """
    # Default to an empty frozenset so the inner loop's `_is_blocked_chain`
    # call doesn't have to handle None. Empty set means no chain filtering.
    chain_set = blocked_chain_names if blocked_chain_names is not None else frozenset()

    kept: list[Business] = []
    dropped: list[Business] = []

    for biz in businesses:
        # Dead-listing filter first because it's the cheapest check
        # (one int comparison) and the most common drop reason in
        # practice.
        if biz.review_count < min_review_count:
            logger.debug(
                "Filtered (dead): %s (review_count=%d)",
                biz.name, biz.review_count,
            )
            dropped.append(biz)
            continue

        # Chain filter only runs when we have at least one fragment.
        # Avoids the normalize-name regex work on every single business
        # when the caller didn't pass a block-list.
        if chain_set and _is_blocked_chain(biz.name, chain_set):
            logger.debug("Filtered (chain): %s", biz.name)
            dropped.append(biz)
            continue

        kept.append(biz)

    if dropped:
        # Single info-level summary per call so the operator sees how
        # much each filter pruned without spamming per-business lines
        # at info (those are debug above).
        logger.info(
            "Filtered %d/%d businesses (dead/chain) before discover.",
            len(dropped), len(businesses),
        )

    return kept, dropped
