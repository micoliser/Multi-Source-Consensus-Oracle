# Multi-Source Consensus Oracle

A reusable GenLayer Intelligent Contract primitive that answers a real question:
**how do you get a trustworthy number onto a blockchain when no single web
source should be fully trusted?**

## Why this exists

Traditional oracles (even "decentralized" ones) usually still resolve to a
single upstream API per data point. If that API is wrong, stale, or
compromised, everything built on top inherits the error. This contract
removes that single-source assumption: register a feed against 3+
independent public web pages describing the same fact, and the contract
fetches all of them, extracts a value from each, and reconciles them into
one consensus number by finding the largest cluster of mutually-agreeing
values and requiring that cluster to reach a strict majority before
accepting it.

It is intentionally a **primitive**, not an app: no frontend, no specific
data type baked in. Any builder can register a feed for a price, a count, a
rate, a score, an index level, anything expressible as "read this page,
find this number."

## How GenLayer consensus is actually used here

1. **Inner consensus (multi-source → one number).** Inside a single
   non-deterministic block, the contract fetches every registered source,
   asks an LLM to extract a number from each, then finds the largest
   cluster of values that mutually agree within a configurable tolerance
   band (`tolerance_bps`) of each other. That cluster must reach a strict
   majority of the feed's **configured** source count, or the update is
   rejected outright — a single bad or manipulated source can never be
   outvoted with fewer than 3 sources, which is why 3 is the enforced
   minimum. Quorum is deliberately computed against how many sources the
   feed was _registered_ with, not against however many happened to
   respond on a given update — see "Partial outages" below for why this
   distinction matters.

2. **Outer consensus (validator → validator).** GenVM's leader runs the
   whole fetch-and-reconcile process once. Every validator independently
   _re-runs the same process_, hitting the live sources again at their own
   moment in time. Because real-world values drift second to second, two
   honest validators will almost never produce byte-identical results,
   which means `eq_principle_strict_eq` is the wrong tool. This contract
   uses `eq_principle_prompt_comparative`, where the LLM is given an
   explicit principle: two reconciled results are equivalent if they agree
   on error/success state and, when both succeed, their values fall within
   the feed's configured tolerance of each other.

   This is the genuine Equivalence Principle problem a multi-source oracle
   has to solve, not "did the LLM say yes," but "are two independently
   reconciled real-world measurements close enough to be the same fact."

3. **Fail-closed, not fail-open.** If fewer sources return a usable value
   than the feed's configured majority requires, or if no cluster of
   extracted values reaches that same majority threshold, the block
   returns an explicit error (`insufficient_sources` or
   `no_quorum_agreement`) and the contract does **not** update feed state.
   A minimum of 3 sources is enforced at registration specifically because
   outlier detection is mathematically impossible with only 2: a disagreeing pair is always exactly
   equidistant from their own average, so there's no way to tell which
   one (if either) is wrong, and a single bad source can never be
   outvoted. Multiple URLs from the same domain are also rejected at
   registration, so a feed can't satisfy the 3-source minimum by padding
   with different paths on the same site.

## Partial outages don't lower the real threshold

Quorum is computed against the feed's **configured** `source_count`,
the number of sources it was registered with, never against however
many sources merely responded on a given update. This distinction is the
fix for a real gap a reviewer caught: computing quorum against only the
responders would let a partial outage silently weaken the effective
threshold. Concretely, a feed registered with 5 sources where 2 go down
or are deliberately blocked would otherwise only need 2 of the remaining
3 responders to agree, 2-of-5 overall, not the 3-of-5 a genuine
majority of the full configured feed requires. An attacker able to
suppress a couple of sources (or simply wait for natural outages)
should never thereby need fewer agreeing readings to control the result.

The fix: `quorum_needed = (source_count // 2) + 1` is computed once
against the feed's configured source count, and used for both (a) the
minimum-reachability check, rejecting outright as `insufficient_sources`
if fewer sources than quorum even returned a usable value at all, and
(b) the majority-cluster check on whatever values did come back. A feed
can never reach consensus with fewer agreeing sources than a true
majority of its full registered set, regardless of how many sources
happen to be reachable at update time.

**Honest limitation, not fully closed by this fix:** this protects
against outages/manipulation of a _minority_ of registered sources. It
does not protect against an adversary who controls (or can influence) a
genuine majority of the sources a feed was registered with in the first
place, that's a feed-configuration trust assumption (who gets to
register which URLs), not something the reconciliation algorithm itself
can solve. Choosing genuinely independent, hard-to-collude sources at
registration time is still the feed creator's responsibility.

## Contract interface

| Method                                                                | Type  | Description                                                                                                             |
| --------------------------------------------------------------------- | ----- | ----------------------------------------------------------------------------------------------------------------------- |
| `register_feed(description, sources, extraction_hint, tolerance_bps)` | write | Registers a new feed. Requires 3 to 10 sources, all on distinct domains. Returns `feed_id`.                             |
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
  storage-generic edge cases, a deliberate simplicity/robustness trade-off, not an oversight.

## Error handling

All input-validation and access-control failures raise `gl.vm.UserError(...)`
rather than a bare Python `Exception`. GenVM's schema/contract loader
rejects bare exceptions raised across the contract boundary, if you fork
this primitive, keep raises as `gl.vm.UserError` (the `try/except Exception`
blocks inside the non-deterministic block that swallow bad per-source
responses are fine as-is; those never cross the contract boundary).

## SDK version compatibility

This contract targets the namespaced `gl.*` API introduced in SDK v0.1.3+
(`gl.nondet.web.render`, `gl.nondet.exec_prompt`,
`gl.eq_principle.prompt_comparative`). If your pinned `py-genlayer`
dependency resolves to an older v0.1.0-era build, the equivalent flat calls
are `gl.get_webpage`, `gl.exec_prompt`, and
`gl.eq_principle_prompt_comparative`, swap the call sites accordingly.

## Limitations / honest caveats

- Extraction quality depends on the LLM correctly reading the requested
  number off arbitrary page text. Sources with the number buried in
  non-obvious formatting (tables, images, JS-rendered content) may return
  `found: false` more often, `mode='text'` won't see JS-rendered values.
- `tolerance_bps` must be between 1 and 5000 (0.01% - 50%). A tolerance of 0
  is rejected because it requires bit-for-bit equality across independent fetches,
  while the cap prevents treating wildly divergent sources as equivalent.
- This primitive does not include payment/staking for feed updates,
  anyone can call `request_update`. That's intentional (keeps it a bare
  primitive), but production use would likely add a caller-pays-gas or
  keeper-incentive layer on top.

## Testing

See `test.py` for a test skeleton using `genlayer-test`
(`gltest`)'s mock LLM and mock web response system, which lets you exercise
the reconciliation logic deterministically without hitting real websites or
real LLM providers.
