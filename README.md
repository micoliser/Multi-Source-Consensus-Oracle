# Multi-Source Consensus Oracle

A reusable GenLayer Intelligent Contract primitive that answers a real question:
**how do you get a trustworthy number onto a blockchain when no single web
source should be fully trusted?**

## Why this exists

Traditional oracles (even "decentralized" ones) usually still resolve to a
single upstream API per data point. If that API is wrong, stale, or
compromised, everything built on top inherits the error. This contract
removes that single-source assumption: register a feed against 2+
independent public web pages describing the same fact, and the contract
fetches all of them, extracts a value from each, and reconciles them into
one consensus number using a median + outlier-tolerance filter.

It is intentionally a **primitive**, not an app: no frontend, no specific
data type baked in. Any builder can register a feed for a price, a count, a
rate, a score, an index level -- anything expressible as "read this page,
find this number."

## How GenLayer consensus is actually used here

1. **Inner consensus (multi-source → one number).** Inside a single
   non-deterministic block, the contract fetches every registered source,
   asks an LLM to extract a number from each, sorts the results, computes
   the median, and discards anything outside a configurable tolerance band
   (`tolerance_bps`) before averaging what's left. A single bad or
   manipulated source can't dominate the result as long as a majority of
   sources agree.

2. **Outer consensus (validator → validator).** GenVM's leader runs the
   whole fetch-and-reconcile process once. Every validator independently
   _re-runs the same process_, hitting the live sources again at their own
   moment in time. Because real-world values drift second to second, two
   honest validators will almost never produce byte-identical results --
   which means `eq_principle_strict_eq` is the wrong tool. This contract
   uses `eq_principle_prompt_comparative`, where the LLM is given an
   explicit principle: two reconciled results are equivalent if they agree
   on error/success state and, when both succeed, their values fall within
   the feed's configured tolerance of each other.

   This is the genuine Equivalence Principle problem a multi-source oracle
   has to solve -- not "did the LLM say yes," but "are two independently
   reconciled real-world measurements close enough to be the same fact."

3. **Fail-closed, not fail-open.** If fewer than 2 sources return a usable
   value, the block returns an explicit `insufficient_sources` error and
   the contract does **not** update feed state. Callers can detect and
   react to degraded reliability instead of silently receiving a stale or
   fabricated number.

## Contract interface

| Method                                                                | Type  | Description                                                                                                             |
| --------------------------------------------------------------------- | ----- | ----------------------------------------------------------------------------------------------------------------------- |
| `register_feed(description, sources, extraction_hint, tolerance_bps)` | write | Registers a new feed. Requires ≥2 sources. Returns `feed_id`.                                                           |
| `request_update(feed_id)`                                             | write | Triggers a full fetch-reconcile-consensus round. Updates feed state on success; returns the raw JSON result either way. |
| `deactivate_feed(feed_id)`                                            | write | Owner-only. Marks a feed inactive.                                                                                      |
| `get_feed(feed_id)`                                                   | view  | Returns full feed metadata and latest value.                                                                            |
| `get_latest_value(feed_id)`                                           | view  | Returns just the latest consensus value as a string.                                                                    |
| `get_feed_count()`                                                    | view  | Total number of registered feeds.                                                                                       |

## Example usage (via GenLayer Studio or genlayer-js)

```python
# Register a feed checking a metric across three independent pages
feed_id = contract.register_feed(
    description="Example: registered library count",
    sources=[
        "https://example-source-one.test/stats",
        "https://example-source-two.test/stats",
        "https://example-source-three.test/stats",
    ],
    extraction_hint="the total number of registered libraries shown on the page",
    tolerance_bps=300,  # 3% tolerance between sources/validators
)

result = contract.request_update(feed_id)
# -> '{"value": 41822.0, "sources_used": 3, "sources_total": 3}'

contract.get_latest_value(feed_id)
# -> "41822.0"
```

## Storage design notes

- Feed data lives in `TreeMap[u256, Feed]`, keyed by an auto-incrementing
  `feed_count`.
- Source URLs are stored as a single `"|"`-joined string on the `Feed`
  struct rather than a nested `DynArray[str]` field. GenVM does support
  storage-generics nested inside `@allow_storage` dataclasses, but the
  in-memory allocation pattern for that (`gl.storage.inmem_allocate`)
  varies across SDK versions. Joining to a delimited string keeps this
  primitive simple and stable for other builders to copy without fighting
  storage-generic edge cases -- a deliberate simplicity/robustness
  trade-off, not an oversight.

## Error handling

All input-validation and access-control failures raise `gl.vm.UserError(...)`
rather than a bare Python `Exception`. GenVM's schema/contract loader
rejects bare exceptions raised across the contract boundary -- if you fork
this primitive, keep raises as `gl.vm.UserError` (the `try/except Exception`
blocks inside the non-deterministic block that swallow bad per-source
responses are fine as-is; those never cross the contract boundary).

## SDK version compatibility

This contract targets the namespaced `gl.*` API introduced in SDK v0.1.3+
(`gl.nondet.web.render`, `gl.nondet.exec_prompt`,
`gl.eq_principle.prompt_comparative`). If your pinned `py-genlayer`
dependency resolves to an older v0.1.0-era build, the equivalent flat calls
are `gl.get_webpage`, `gl.exec_prompt`, and
`gl.eq_principle_prompt_comparative` -- swap the call sites accordingly.

## Limitations / honest caveats

- Extraction quality depends on the LLM correctly reading the requested
  number off arbitrary page text. Sources with the number buried in
  non-obvious formatting (tables, images, JS-rendered content) may return
  `found: false` more often -- `mode='text'` won't see JS-rendered values.
- `tolerance_bps` is capped at 5000 (50%) to prevent registering a feed
  that would treat wildly divergent sources as equivalent.
- This primitive does not include payment/staking for feed updates --
  anyone can call `request_update`. That's intentional (keeps it a bare
  primitive), but production use would likely add a caller-pays-gas or
  keeper-incentive layer on top.

## Testing

See `test.py` for a test skeleton using `genlayer-test`
(`gltest`)'s mock LLM and mock web response system, which lets you exercise
the reconciliation logic deterministically without hitting real websites or
real LLM providers.
