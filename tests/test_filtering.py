"""Tests for filtering.filter_live_businesses (feature 06).

Pure-function module, so tests are dependency-free dataclass exercises:
build a few `Business` instances, call the filter, assert the (kept,
dropped) partitions.
"""

from datetime import datetime, timezone

from leadscout.filtering import _normalize_name, filter_live_businesses
from leadscout.models import Business, UrlClassification, UrlSource


def _make(name: str, review_count: int) -> Business:
    """Minimal Business factory. Only the fields the filter inspects
    matter; everything else gets a sensible default."""
    return Business(
        place_id=name,  # any unique-per-call string is fine
        name=name,
        review_count=review_count,
        url_source=UrlSource.GOOGLE_PLACES,
        url_classification=UrlClassification.OFFICIAL_SITE,
        last_scanned=datetime.now(timezone.utc),
    )


class TestReviewCountFilter:
    def test_drops_zero_review_businesses_at_default(self):
        # Default min_review_count = 1, so review_count=0 fails the check.
        # The other two pass (review_count >= 1).
        kept, dropped = filter_live_businesses([
            _make("Dead Listing", 0),
            _make("New Spot", 1),
            _make("Established", 50),
        ])
        names_kept = [b.name for b in kept]
        names_dropped = [b.name for b in dropped]
        assert names_kept == ["New Spot", "Established"]
        assert names_dropped == ["Dead Listing"]

    def test_keeps_review_count_at_threshold(self):
        # review_count == min_review_count is kept (strict less-than is
        # the drop predicate).
        kept, _ = filter_live_businesses(
            [_make("Edge", 5)], min_review_count=5,
        )
        assert [b.name for b in kept] == ["Edge"]

    def test_min_review_count_zero_disables_filter(self):
        # Pass 0 to disable. Even literal zero-review listings come through.
        kept, dropped = filter_live_businesses(
            [_make("Dead", 0), _make("Live", 1)],
            min_review_count=0,
        )
        assert len(kept) == 2
        assert dropped == []

    def test_higher_threshold_drops_more(self):
        kept, dropped = filter_live_businesses([
            _make("Few Reviews", 3),
            _make("Plenty Reviews", 50),
        ], min_review_count=10)
        assert [b.name for b in kept] == ["Plenty Reviews"]
        assert [b.name for b in dropped] == ["Few Reviews"]


class TestChainFilter:
    def test_blocks_chain_via_substring_match(self):
        # "McDonald's #4521" normalizes to "mcdonalds 4521" (apostrophe
        # and hash become whitespace, then collapse). Substring match
        # for "mcdonald" hits.
        kept, dropped = filter_live_businesses(
            [
                _make("McDonald's #4521", 100),
                _make("Local Diner", 100),
            ],
            blocked_chain_names=frozenset({"mcdonald"}),
        )
        assert [b.name for b in kept] == ["Local Diner"]
        assert [b.name for b in dropped] == ["McDonald's #4521"]

    def test_punctuation_does_not_defeat_match(self):
        # Multiple punctuation forms all normalize the same way.
        for raw in ["McDonalds", "McDonald's", "Mc-Donald's!", "MCDONALD'S"]:
            kept, dropped = filter_live_businesses(
                [_make(raw, 100)],
                blocked_chain_names=frozenset({"mcdonald"}),
            )
            assert [b.name for b in kept] == []
            assert [b.name for b in dropped] == [raw]

    def test_fragment_is_substring_not_word_boundary(self):
        # The block-list is intentionally substring-based: a corporate
        # variant like "Subway Restaurants" should still match "subway".
        kept, dropped = filter_live_businesses(
            [_make("Subway Restaurants", 50)],
            blocked_chain_names=frozenset({"subway"}),
        )
        assert dropped[0].name == "Subway Restaurants"
        assert kept == []

    def test_no_blocked_set_is_no_op(self):
        kept, dropped = filter_live_businesses(
            [_make("Subway #1", 100), _make("Diner", 100)],
            min_review_count=0,
        )
        # Default blocked_chain_names is None: no chain filter applied.
        assert len(kept) == 2
        assert dropped == []

    def test_empty_blocked_set_is_no_op(self):
        kept, dropped = filter_live_businesses(
            [_make("Subway #1", 100)],
            blocked_chain_names=frozenset(),
        )
        assert len(kept) == 1
        assert dropped == []


class TestPartitionInvariants:
    def test_returns_two_disjoint_lists_covering_input(self):
        # Every input appears exactly once across kept + dropped, in
        # input order within each list. This is the contract the
        # campaign summary depends on for accurate filtered counts.
        biz = [
            _make("Keep1", 5),
            _make("Drop1", 0),
            _make("Keep2", 5),
            _make("Drop2", 0),
        ]
        kept, dropped = filter_live_businesses(biz)
        assert [b.name for b in kept] == ["Keep1", "Keep2"]
        assert [b.name for b in dropped] == ["Drop1", "Drop2"]
        # No leakage: total length must equal input length.
        assert len(kept) + len(dropped) == len(biz)

    def test_does_not_mutate_input_list(self):
        biz = [_make("X", 0)]
        original = list(biz)
        filter_live_businesses(biz)
        # Same identity AND length; the function returns NEW lists, it
        # doesn't pop from the caller's.
        assert biz == original
        assert len(biz) == 1


class TestNormalizeName:
    def test_lowercases(self):
        assert _normalize_name("McDonald's") == "mcdonalds"

    def test_strips_apostrophes_and_hash(self):
        assert _normalize_name("McDonald's #4521") == "mcdonalds 4521"

    def test_collapses_whitespace(self):
        # Multiple spaces / runs of punctuation collapse to a single space.
        assert _normalize_name("Hello,,,  World") == "hello world"

    def test_preserves_internal_underscores(self):
        # \w in the strip regex includes underscore by design — types
        # like "fast_food_restaurant" stay intact when normalizing
        # business_type values (we don't actually normalize types here,
        # but the helper might be reused).
        assert _normalize_name("fast_food_restaurant") == "fast_food_restaurant"
