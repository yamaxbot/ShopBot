from types import SimpleNamespace

from playerok_bot.gateway import PlayerokGateway


def gateway() -> PlayerokGateway:
    result = PlayerokGateway(None, None)  # type: ignore[arg-type]
    result._resolved_item_id = "listing-id"
    result._resolved_item_slug = "stable-listing-slug"
    return result


def test_deal_item_matches_by_exact_id() -> None:
    assert gateway()._is_target_item(
        SimpleNamespace(id="listing-id", slug="other-slug")
    )


def test_deal_snapshot_matches_by_stable_slug() -> None:
    assert gateway()._is_target_item(
        SimpleNamespace(id="deal-snapshot-id", slug="stable-listing-slug")
    )


def test_unrelated_deal_does_not_match() -> None:
    assert not gateway()._is_target_item(
        SimpleNamespace(id="other-id", slug="other-slug")
    )
