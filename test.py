"""
Test skeleton for MultiSourceOracle, written against the `genlayer-test`
(gltest) mocking conventions.

These tests are illustrative: adapt the fixture names / factory calls to
match your project's actual gltest.config.yaml and installed gltest
version. The intent is to demonstrate that the contract's core logic,
registering a feed, reconciling multiple mocked sources via majority-
cluster quorum, rejecting insufficient/duplicate sources, failing closed
when no quorum is reached, and owner-gated deactivation, can be
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
                ["https://example-a.test/a", "https://example-b.test/b"],
                "the number shown on the page",
                300,
            ]
        ).transact()


def test_register_feed_rejects_duplicate_domains(get_contract_factory):
    """
    Passing multiple URLs from the same domain must not satisfy the 3-source minimum,
    that's the single-source problem in disguise.
    """
    factory = get_contract_factory("MultiSourceOracle")
    contract = factory.deploy()

    with pytest.raises(Exception):
        contract.register_feed(
            args=[
                "padded feed",
                [
                    "https://example-a.test/path1",
                    "https://example-a.test/path2",
                    "https://example-b.test/b",
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
                "https://example-a.test/a",
                "https://example-b.test/b",
                "https://example-c.test/c",
            ],
            "the total count shown on the page",
            300,
        ]
    ).transact()

    second_id = contract.register_feed(
        args=[
            "another metric",
            [
                "https://example-d.test/d",
                "https://example-e.test/e",
                "https://example-f.test/api",
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
            "example-a.test/a": json.dumps({"value": 100.0, "found": True}),
            "example-b.test/b": json.dumps({"value": 101.0, "found": True}),
            "example-c.test/c": json.dumps({"value": 100.5, "found": True}),
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
                "https://example-a.test/a",
                "https://example-b.test/b",
                "https://example-c.test/c",
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
            "example-a.test/a": json.dumps({"value": 100.0, "found": True}),
            "example-b.test/b": json.dumps({"value": 101.0, "found": True}),
            "example-c.test/c": json.dumps({"value": 9999.0, "found": True}),  # outlier
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
                "https://example-a.test/a",
                "https://example-b.test/b",
                "https://example-c.test/c",
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
    All 3 sources disagree with each other, no cluster reaches a strict
    majority. The contract must reject the update outright rather than
    fall back to averaging everything in, which was the original bug a
    reviewer caught.
    """
    factory = get_contract_factory("MultiSourceOracle")

    mock_response: MockedLLMResponse = {
        "nondet_exec_prompt": {
            "example-a.test/a": json.dumps({"value": 100.0, "found": True}),
            "example-b.test/b": json.dumps({"value": 500.0, "found": True}),
            "example-c.test/c": json.dumps({"value": 9000.0, "found": True}),
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
                "https://example-a.test/a",
                "https://example-b.test/b",
                "https://example-c.test/c",
            ],
            "the value shown on the page",
            300,  # tight 3% tolerance, none of these will cluster
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
    feed's latest_value must remain unchanged, fail closed rather than
    persist a low-confidence number.
    """
    factory = get_contract_factory("MultiSourceOracle")

    mock_response: MockedLLMResponse = {
        "nondet_exec_prompt": {
            "example-a.test/a": json.dumps({"value": None, "found": False}),
            "example-b.test/b": json.dumps({"value": None, "found": False}),
            "example-c.test/c": json.dumps({"value": 100.0, "found": True}),
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
                "https://example-a.test/a",
                "https://example-b.test/b",
                "https://example-c.test/c",
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


def test_request_update_fails_closed_on_partial_outage_undermining_quorum(
    get_contract_factory, get_validator_factory
):
    """
    This is the exact scenario a reviewer flagged: a feed registered with
    5 sources where 2 go unreachable/unparseable, leaving only 3 usable
    readings. Of those 3, only 2 agree with each other. Quorum for a
    5-source feed is (5 // 2) + 1 = 3, so a 2-value cluster must NOT be
    accepted, even though 2 happens to be a "majority" of the 3 sources
    that responded. Computing quorum against only the responders (the old,
    buggy behavior) would incorrectly accept this; computing it against
    the feed's configured source_count correctly rejects it.
    """
    factory = get_contract_factory("MultiSourceOracle")

    mock_response: MockedLLMResponse = {
        "nondet_exec_prompt": {
            "example-a.test/a": json.dumps({"value": 100.0, "found": True}),
            "example-b.test/b": json.dumps({"value": 101.0, "found": True}),
            # source c is unreachable/unparseable, simulated by omission,
            # the fetch itself would raise and get skipped in the real flow
            "example-d.test/d": json.dumps({"value": 500.0, "found": True}),
            # source e is unreachable/unparseable, simulated by omission
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
            "partial outage example",
            [
                "https://example-a.test/a",
                "https://example-b.test/b",
                "https://example-c.test/c",  # will fail to resolve/parse
                "https://example-d.test/d",
                "https://example-e.test/e",  # will fail to resolve/parse
            ],
            "the value shown on the page",
            300,
        ]
    ).transact(transaction_context=transaction_context)

    result = contract.request_update(args=[int(feed_id)]).transact(
        transaction_context=transaction_context
    )
    parsed = json.loads(result)

    # Only 3 of 5 configured sources returned usable values (a, b, d),
    # and only 2 of those 3 (a, b) agree with each other. Quorum for a
    # 5-source feed is 3, so this must fail closed, NOT succeed with a
    # "2 out of 3 responders" false majority.
    assert "error" in parsed
    assert parsed["error"] in ("insufficient_sources", "no_quorum_agreement")

    feed = contract.get_feed(args=[int(feed_id)]).call()
    assert feed["latest_value"] == ""
    assert feed["last_updated_round"] == 0


def test_request_update_succeeds_when_majority_of_configured_sources_agree(
    get_contract_factory, get_validator_factory
):
    """
    Companion to the above: a 5-source feed where exactly 3 (a true
    majority of 5) agree, and 2 are unreachable, SHOULD succeed,
    confirming the fix isn't overly strict, just correctly computed
    against the configured source count rather than the responder count.
    """
    factory = get_contract_factory("MultiSourceOracle")

    mock_response: MockedLLMResponse = {
        "nondet_exec_prompt": {
            "example-a.test/a": json.dumps({"value": 100.0, "found": True}),
            "example-b.test/b": json.dumps({"value": 100.5, "found": True}),
            "example-c.test/c": json.dumps({"value": 99.5, "found": True}),
            # source d and e are unreachable/unparseable, simulated by omission
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
            "true majority example",
            [
                "https://example-a.test/a",
                "https://example-b.test/b",
                "https://example-c.test/c",
                "https://example-d.test/d",  # will fail to resolve/parse
                "https://example-e.test/e",  # will fail to resolve/parse
            ],
            "the value shown on the page",
            500,
        ]
    ).transact(transaction_context=transaction_context)

    result = contract.request_update(args=[int(feed_id)]).transact(
        transaction_context=transaction_context
    )
    parsed = json.loads(result)

    assert "value" in parsed
    assert parsed["sources_used"] == 3

    feed = contract.get_feed(args=[int(feed_id)]).call()
    assert feed["latest_sources_used"] == 3


def test_only_owner_can_deactivate_feed(get_contract_factory, get_account_factory):
    factory = get_contract_factory("MultiSourceOracle")
    owner_account = get_account_factory().create_new_account()
    other_account = get_account_factory().create_new_account()

    contract = factory.deploy(account=owner_account)

    feed_id = contract.register_feed(
        args=[
            "ownership example",
            [
                "https://example-a.test/a",
                "https://example-b.test/b",
                "https://example-c.test/c",
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