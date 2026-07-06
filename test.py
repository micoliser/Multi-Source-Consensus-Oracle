"""
Test skeleton for MultiSourceOracle, written against the `genlayer-test`
(gltest) mocking conventions.

These tests are illustrative: adapt the fixture names / factory calls to
match your project's actual gltest.config.yaml and installed gltest
version. The intent is to demonstrate that the contract's core logic --
registering a feed, reconciling multiple mocked sources, rejecting
insufficient sources, and owner-gated deactivation -- can be exercised
deterministically without hitting real websites or real LLM providers.
"""

import json
import pytest
from gltest.types import MockedLLMResponse


def test_register_feed_requires_two_sources(get_contract_factory):
    factory = get_contract_factory("MultiSourceOracle")
    contract = factory.deploy()

    with pytest.raises(Exception):
        contract.register_feed(
            args=[
                "single source feed",
                ["https://example.test/only-one"],
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
            ["https://example.test/a", "https://example.test/b"],
            "the total count shown on the page",
            300,
        ]
    ).transact()

    second_id = contract.register_feed(
        args=[
            "another metric",
            ["https://example.test/c", "https://example.test/d"],
            "the score shown on the page",
            300,
        ]
    ).transact()

    assert int(second_id) == int(first_id) + 1
    assert contract.get_feed_count().call() == 2


def test_request_update_reconciles_agreeing_sources(
    get_contract_factory, get_validator_factory
):
    """
    Two sources report values within tolerance of each other; the contract
    should reconcile them into a single averaged consensus value and
    persist it as the feed's latest_value.
    """
    factory = get_contract_factory("MultiSourceOracle")

    mock_response: MockedLLMResponse = {
        # Mocks the per-source extraction prompt: both sources "agree"
        "nondet_exec_prompt": {
            "example.test/a": json.dumps({"value": 100.0, "found": True}),
            "example.test/b": json.dumps({"value": 101.0, "found": True}),
        },
        # Mocks the validator-vs-leader comparative judgment
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
            ["https://example.test/a", "https://example.test/b"],
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


def test_request_update_fails_closed_on_insufficient_sources(
    get_contract_factory, get_validator_factory
):
    """
    If fewer than 2 sources return a usable value, the feed's latest_value
    must remain unchanged (empty) -- the contract should fail closed rather
    than persist a low-confidence number.
    """
    factory = get_contract_factory("MultiSourceOracle")

    mock_response: MockedLLMResponse = {
        "nondet_exec_prompt": {
            "example.test/a": json.dumps({"value": None, "found": False}),
            "example.test/b": json.dumps({"value": None, "found": False}),
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
            ["https://example.test/a", "https://example.test/b"],
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
            ["https://example.test/a", "https://example.test/b"],
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