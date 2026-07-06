# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
Multi-Source Consensus Oracle
==============================

A reusable Intelligent Contract primitive for GenLayer.

PROBLEM
-------
Most on-chain oracles trust a single data source (one API, one page). If that
source is wrong, down, or manipulated, every consumer of the oracle inherits
the error. This primitive removes the single-source trust assumption by:

  1. Fetching the same data point from N independent web sources.
  2. Using an LLM to extract a numeric value from each source's raw content.
  3. Reconciling the values with a median + tolerance-band outlier filter,
     so no single rogue or stale source can dominate the result.

CONSENSUS DESIGN
-----------------------------------------------------------------------
Steps 1-3 above happen inside a single non-deterministic block. GenVM's
leader executes it once; every validator independently re-executes the
*same* fetch-reconcile process, hitting the live sources at their own
moment in time. Because prices/values on real websites drift second to
second, validators will almost never get byte-identical results -- so
`eq_principle_strict_eq` is the wrong tool here.

Instead this contract uses `eq_principle_prompt_comparative`: validators
ask an LLM "are the leader's reconciled result and my own reconciled result
equivalent, given the feed's configured tolerance and normal market/data
drift?" This is the actual Equivalence Principle problem multi-source
oracles have to solve, not a cosmetic wrapper around one LLM call.

If fewer than 2 sources return a usable value, the block reports an
explicit error state instead of fabricating a number, and the contract
does not update the feed -- callers can detect and handle degraded
reliability rather than silently trusting bad data.

STORAGE DESIGN NOTE
--------------------
Sources are stored as a single "|"-joined string (`sources_joined`) rather
than a nested DynArray[str] field inside the Feed struct. GenVM storage
supports nested generics (DynArray/TreeMap fields inside @allow_storage
dataclasses), but it requires explicit in-memory allocation helpers whose
exact call pattern varies across SDK versions. Joining to a delimited
string keeps the primitive simple, version-stable, and easy for other
builders to copy without fighting storage-generic edge cases.

REUSE
-----
Any builder can register a feed by pointing it at 2+ public web pages that
contain the same real-world numeric fact (a price, a count, a rate, an
index level) plus a short natural-language hint describing what to
extract. This makes the contract a general-purpose primitive, not a
one-off demo tied to a specific data type.

Compatibility note: this file targets the SDK's namespaced v0.1.3+ API
(gl.nondet.web.render, gl.nondet.exec_prompt, gl.eq_principle.prompt_comparative).
Older SDK builds (v0.1.0) use the flat namespace instead: gl.get_webpage,
gl.exec_prompt, and gl.eq_principle_prompt_comparative -- swap the call
sites if your pinned dependency resolves to that generation.
"""

import json
import typing
from dataclasses import dataclass
from genlayer import *


@allow_storage
@dataclass
class Feed:
    description: str          # human-readable label, e.g. "ETH/USD spot price"
    sources_joined: str        # "|"-joined source URLs
    source_count: u256
    extraction_hint: str        # tells the LLM what number to pull off each page
    tolerance_bps: u256         # outlier tolerance, in basis points (100 = 1%)
    owner: Address
    latest_value: str           # decimal string, "" until first successful update
    latest_sources_used: u256   # how many sources survived the outlier filter
    last_updated_round: u256    # increments on every successful reconciliation
    active: bool


class MultiSourceOracle(gl.Contract):
    feeds: TreeMap[u256, Feed]
    feed_count: u256

    def __init__(self):
        self.feeds = TreeMap()
        self.feed_count = u256(0)

    @gl.public.write
    def register_feed(
        self,
        description: str,
        sources: list[str],
        extraction_hint: str,
        tolerance_bps: int,
    ) -> int:
        if len(sources) < 2:
            raise gl.vm.UserError("register_feed requires at least 2 independent sources")
        if tolerance_bps < 0 or tolerance_bps > 5000:
            raise gl.vm.UserError("tolerance_bps must be between 0 and 5000 (0-50%)")

        feed_id = self.feed_count
        self.feeds[feed_id] = Feed(
            description,
            "|".join(sources),
            u256(len(sources)),
            extraction_hint,
            u256(tolerance_bps),
            gl.message.sender_address,
            "",
            u256(0),
            u256(0),
            True,
        )
        self.feed_count = u256(int(self.feed_count) + 1)
        return int(feed_id)

    @gl.public.write
    def request_update(self, feed_id: int) -> str:
        key = u256(feed_id)
        if key not in self.feeds:
            raise gl.vm.UserError("unknown feed_id")

        feed = self.feeds[key]
        if not feed.active:
            raise gl.vm.UserError("feed is deactivated")

        # Copy everything the non-deterministic block needs into plain
        # locals -- storage is inaccessible from inside gl.eq_principle.*.
        source_urls = feed.sources_joined.split("|")
        hint = feed.extraction_hint
        tolerance_bps_local = int(feed.tolerance_bps)

        def fetch_and_reconcile() -> str:
            extracted_values = []

            for url in source_urls:
                try:
                    page_text = gl.nondet.web.render(url, mode="text")
                except Exception:
                    continue  # unreachable source: skip, don't fail the whole feed

                prompt = f"""
You are extracting a single numeric data point from raw web page text.

DATA POINT REQUESTED: {hint}

PAGE CONTENT (may include navigation, ads, and unrelated text -- ignore that):
{page_text[:6000]}

Respond ONLY with JSON in exactly this shape, nothing else:
{{"value": <number or null>, "found": true or false}}

If you cannot confidently find the requested number on this page, set
"found" to false and "value" to null. Do not guess.
"""
                raw = gl.nondet.exec_prompt(prompt)
                raw = raw.strip().replace("```json", "").replace("```", "")
                try:
                    parsed = json.loads(raw)
                except Exception:
                    continue

                if parsed.get("found") and parsed.get("value") is not None:
                    try:
                        extracted_values.append(float(parsed["value"]))
                    except (TypeError, ValueError):
                        continue

            if len(extracted_values) < 2:
                return json.dumps(
                    {
                        "error": "insufficient_sources",
                        "usable_sources": len(extracted_values),
                    }
                )

            extracted_values.sort()
            mid = len(extracted_values) // 2
            if len(extracted_values) % 2 == 1:
                median = extracted_values[mid]
            else:
                median = (extracted_values[mid - 1] + extracted_values[mid]) / 2

            tol = tolerance_bps_local / 10000.0
            if median != 0:
                kept = [
                    v for v in extracted_values if abs(v - median) <= abs(median) * tol
                ]
            else:
                kept = extracted_values

            if not kept:
                kept = extracted_values  # tolerance excluded everything: fall back

            consensus = sum(kept) / len(kept)
            return json.dumps(
                {
                    "value": round(consensus, 8),
                    "sources_used": len(kept),
                    "sources_total": len(extracted_values),
                }
            )

        principle = (
            "Both answers are JSON objects describing a reconciled numeric value "
            "pulled independently from live web sources at slightly different "
            "moments in time. Treat them as equivalent if: (a) both report the "
            f"same error state, or (b) both report a 'value', those values are "
            f"within {tolerance_bps_local} basis points of each other (accounting "
            "for normal real-world data drift between fetches), and 'sources_used' "
            "counts are not wildly inconsistent (differ by at most 1). They are "
            "NOT equivalent if one reports an error while the other reports a "
            "valid value, or if the values diverge by more than the stated "
            "tolerance."
        )

        raw_result = gl.eq_principle.prompt_comparative(fetch_and_reconcile, principle)
        parsed_result = json.loads(raw_result)

        if "error" in parsed_result:
            # Don't mutate feed state on a failed reconciliation -- callers
            # can inspect this return value to detect degraded reliability.
            return raw_result

        feed.latest_value = str(parsed_result["value"])
        feed.latest_sources_used = u256(parsed_result["sources_used"])
        feed.last_updated_round = u256(int(feed.last_updated_round) + 1)
        self.feeds[key] = feed
        return raw_result

    @gl.public.write
    def deactivate_feed(self, feed_id: int) -> None:
        key = u256(feed_id)
        if key not in self.feeds:
            raise gl.vm.UserError("unknown feed_id")
        feed = self.feeds[key]
        if feed.owner != gl.message.sender_address:
            raise gl.vm.UserError("only the feed owner can deactivate it")
        feed.active = False
        self.feeds[key] = feed

    @gl.public.view
    def get_feed(self, feed_id: int) -> typing.Any:
        key = u256(feed_id)
        if key not in self.feeds:
            raise gl.vm.UserError("unknown feed_id")
        feed = self.feeds[key]
        return {
            "description": feed.description,
            "sources": feed.sources_joined.split("|"),
            "extraction_hint": feed.extraction_hint,
            "tolerance_bps": int(feed.tolerance_bps),
            "owner": hex(feed.owner.as_int),
            "latest_value": feed.latest_value,
            "latest_sources_used": int(feed.latest_sources_used),
            "last_updated_round": int(feed.last_updated_round),
            "active": feed.active,
        }

    @gl.public.view
    def get_latest_value(self, feed_id: int) -> str:
        key = u256(feed_id)
        if key not in self.feeds:
            raise gl.vm.UserError("unknown feed_id")
        return self.feeds[key].latest_value

    @gl.public.view
    def get_feed_count(self) -> int:
        return int(self.feed_count)