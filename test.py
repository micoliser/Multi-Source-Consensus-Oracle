"""
Test skeleton for MultiSourceOracle, written against the `genlayer-test`
(gltest) mocking conventions.

These tests are illustrative: adapt the fixture names / factory calls to
match your project's actual gltest.config.yaml and installed gltest
version. The intent is to demonstrate that the contract's core logic --
registering a feed, reconciling multiple mocked sources via majority-
cluster quorum, rejecting insufficient/duplicate sources, failing closed
when no quorum is reached, and owner-gated deactivation -- can be
exercised deterministically without hitting real websites or real LLM
providers.
"""

import json
import pytest
from gltest.types import MockedLLMResponse


def test_register_feed_requires_three_sources(get_contract_factory):
    """
    Only 2 sources must be rejected outright: outlier detection is
    mathematically impossible with 2, since a disagreeing pair is always
    exactly equidistant from their own average.
    """
    factory = get_contract_factory("MultiSourceOracle")
    contract = factory.deploy()

    with pytest.raises(Exception):
        contract.register_feed(
            args=[
                "two source feed",
                ["https://example.test/a", "https://example.test/b"],
                "the number shown on the page",
                300,
            ]
        ).transact()


def test_register_feed_rejects_duplicate_urls(get_contract_factory):
    """
    Passing the same URL twice must not satisfy the 3-source minimum --
    that's the single-source problem in disguise.
    """
    factory = get_contract_factory("MultiSourceOracle")
    contract = factory.deploy()

    with pytest.raises(Exception):
        contract.register_feed(
            args=[
                "padded feed",
                [
                    "https://example.test/a",
                    "https://example.test/a",
                    "https://example.test/b",
                ],
                "the number shown on the page",
                300,
            ]
        ).transact()


def test_register_feed_succeeds_and_returns_incrementing_ids(get_contract_factory):
    factory = get_contract_factory("MultiSourceOracle")
    contract = factory.deploy()

    first_id = contract.register_feed(
        args=[
            "example metric",
            [
                "https://example.test/a",
                "https://example.test/b",
                "https://example.test/c",
            ],
            "the total count shown on the page",
            300,
        ]
    ).transact()

    second_id = contract.register_feed(
        args=[
            "another metric",
            [
                "https://example.test/d",
                "https://example.test/e",
                "https://example.test/f",
            ],
            "the score shown on the page",
            300,
        ]
    ).transact()

    assert int(second_id) == int(first_id) + 1
    assert contract.get_feed_count().call() == 2


def test_request_update_reconciles_agreeing_majority(
    get_contract_factory, get_validator_factory
):
    """
    3 sources report values within tolerance of each other; the contract
    should reconcile them into a single averaged consensus value and
    persist it as the feed's latest_value.
    """
    factory = get_contract_factory("MultiSourceOracle")

    mock_response: MockedLLMResponse = {
        "nondet_exec_prompt": {
            "example.test/a": json.dumps({"value": 100.0, "found": True}),
            "example.test/b": json.dumps({"value": 101.0, "found": True}),
            "example.test/c": json.dumps({"value": 100.5, "found": True}),
        },
        "eq_principle_prompt_comparative": {"values match": True},
    }

    validator_factory = get_validator_factory()
    mock_validators = validator_factory.batch_create_mock_validators(
        count=5, mock_llm_response=mock_response
    )
    transaction_context = {
        "validators": [v.to_dict() for v in mock_validators],
    }

    contract = factory.deploy(transaction_context=transaction_context)

    feed_id = contract.register_feed(
        args=[
            "agreeing sources example",
            [
                "https://example.test/a",
                "https://example.test/b",
                "https://example.test/c",
            ],
            "the value shown on the page",
            500,  # 5% tolerance
        ]
    ).transact(transaction_context=transaction_context)

    contract.request_update(args=[int(feed_id)]).transact(
        transaction_context=transaction_context
    )

    feed = contract.get_feed(args=[int(feed_id)]).call()
    assert feed["latest_value"] != ""
    assert feed["last_updated_round"] == 1
    assert feed["latest_sources_used"] == 3


def test_request_update_fails_closed_when_outlier_present(
    get_contract_factory, get_validator_factory
):
    """
    This is the exact scenario a reviewer flagged as broken in an earlier
    version of this contract: 2 of 3 sources agree, 1 is a wild outlier.
    The old median+fallback logic would have silently re-admitted the
    outlier and averaged it in. The fixed contract must instead recognize
    the 2-source majority cluster, exclude the outlier, and still update
    successfully using ONLY the agreeing pair.
    """
    factory = get_contract_factory("MultiSourceOracle")

    mock_response: MockedLLMResponse = {
        "nondet_exec_prompt": {
            "example.test/a": json.dumps({"value": 100.0, "found": True}),
            "example.test/b": json.dumps({"value": 101.0, "found": True}),
            "example.test/c": json.dumps({"value": 9999.0, "found": True}),  # outlier
        },
        "eq_principle_prompt_comparative": {"values match": True},
    }

    validator_factory = get_validator_factory()
    mock_validators = validator_factory.batch_create_mock_validators(
        count=5, mock_llm_response=mock_response
    )
    transaction_context = {"validators": [v.to_dict() for v in mock_validators]}

    contract = factory.deploy(transaction_context=transaction_context)

    feed_id = contract.register_feed(
        args=[
            "outlier rejection example",
            [
                "https://example.test/a",
                "https://example.test/b",
                "https://example.test/c",
            ],
            "the value shown on the page",
            500,  # 5% tolerance
        ]
    ).transact(transaction_context=transaction_context)

    result = contract.request_update(args=[int(feed_id)]).transact(
        transaction_context=transaction_context
    )
    parsed = json.loads(result)

    # Must succeed using the 2-source agreeing cluster, NOT average in the outlier
    assert "value" in parsed
    assert parsed["sources_used"] == 2
    assert 100.0 <= parsed["value"] <= 101.0  # nowhere near the 9999 outlier

    feed = contract.get_feed(args=[int(feed_id)]).call()
    assert feed["latest_sources_used"] == 2


def test_request_update_fails_closed_on_no_quorum(
    get_contract_factory, get_validator_factory
):
    """
    All 3 sources disagree with each other -- no cluster reaches a strict
    majority. The contract must reject the update outright rather than
    fall back to averaging everything in, which was the original bug a
    reviewer caught.
    """
    factory = get_contract_factory("MultiSourceOracle")

    mock_response: MockedLLMResponse = {
        "nondet_exec_prompt": {
            "example.test/a": json.dumps({"value": 100.0, "found": True}),
            "example.test/b": json.dumps({"value": 500.0, "found": True}),
            "example.test/c": json.dumps({"value": 9000.0, "found": True}),
        },
    }

    validator_factory = get_validator_factory()
    mock_validators = validator_factory.batch_create_mock_validators(
        count=5, mock_llm_response=mock_response
    )
    transaction_context = {"validators": [v.to_dict() for v in mock_validators]}

    contract = factory.deploy(transaction_context=transaction_context)

    feed_id = contract.register_feed(
        args=[
            "no quorum example",
            [
                "https://example.test/a",
                "https://example.test/b",
                "https://example.test/c",
            ],
            "the value shown on the page",
            300,  # tight 3% tolerance -- none of these will cluster
        ]
    ).transact(transaction_context=transaction_context)

    result = contract.request_update(args=[int(feed_id)]).transact(
        transaction_context=transaction_context
    )
    parsed = json.loads(result)
    assert parsed.get("error") == "no_quorum_agreement"

    feed = contract.get_feed(args=[int(feed_id)]).call()
    assert feed["latest_value"] == ""
    assert feed["last_updated_round"] == 0


def test_request_update_fails_closed_on_insufficient_sources(
    get_contract_factory, get_validator_factory
):
    """
    If fewer than 3 sources return a usable value at all (even if
    registered with 3 URLs, e.g. 2 are unreachable/unparseable), the
    feed's latest_value must remain unchanged -- fail closed rather than
    persist a low-confidence number.
    """
    factory = get_contract_factory("MultiSourceOracle")

    mock_response: MockedLLMResponse = {
        "nondet_exec_prompt": {
            "example.test/a": json.dumps({"value": None, "found": False}),
            "example.test/b": json.dumps({"value": None, "found": False}),
            "example.test/c": json.dumps({"value": 100.0, "found": True}),
        },
    }

    validator_factory = get_validator_factory()
    mock_validators = validator_factory.batch_create_mock_validators(
        count=5, mock_llm_response=mock_response
    )
    transaction_context = {"validators": [v.to_dict() for v in mock_validators]}

    contract = factory.deploy(transaction_context=transaction_context)

    feed_id = contract.register_feed(
        args=[
            "unreliable sources example",
            [
                "https://example.test/a",
                "https://example.test/b",
                "https://example.test/c",
            ],
            "the value shown on the page",
            300,
        ]
    ).transact(transaction_context=transaction_context)

    result = contract.request_update(args=[int(feed_id)]).transact(
        transaction_context=transaction_context
    )
    parsed = json.loads(result)
    assert parsed.get("error") == "insufficient_sources"

    feed = contract.get_feed(args=[int(feed_id)]).call()
    assert feed["latest_value"] == ""
    assert feed["last_updated_round"] == 0


def test_only_owner_can_deactivate_feed(get_contract_factory, get_account_factory):
    factory = get_contract_factory("MultiSourceOracle")
    owner_account = get_account_factory().create_new_account()
    other_account = get_account_factory().create_new_account()

    contract = factory.deploy(account=owner_account)

    feed_id = contract.register_feed(
        args=[
            "ownership example",
            [
                "https://example.test/a",
                "https://example.test/b",
                "https://example.test/c",
            ],
            "the value shown on the page",
            300,
        ],
        account=owner_account,
    ).transact()

    with pytest.raises(Exception):
        contract.deactivate_feed(args=[int(feed_id)], account=other_account).transact()

    contract.deactivate_feed(args=[int(feed_id)], account=owner_account).transact()
    feed = contract.get_feed(args=[int(feed_id)]).call()
    assert feed["active"] is False