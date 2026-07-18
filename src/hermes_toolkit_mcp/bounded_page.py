from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


COMPACT_JSON_SEPARATORS = (",", ":")


class BoundedOutputError(Exception):
    """Raised when a single projected item cannot fit the per-item byte budget."""

    def __init__(self, message: str, id_key: str, id_value: str, item_bytes: int, budget: int) -> None:
        super().__init__(message)
        self.id_key = id_key
        self.id_value = id_value
        self.item_bytes = item_bytes
        self.budget = budget


def json_compact_bytes(value: Any) -> bytes:
    """Return deterministic, compact UTF-8 JSON bytes for a value."""
    return json.dumps(value, ensure_ascii=False, separators=COMPACT_JSON_SEPARATORS, sort_keys=True, default=str).encode("utf-8")


def limit_in_bounds(value: Any, *, default: int, maximum: int) -> int:
    """Return an integer limit clamped between 1 and maximum inclusive.

    ``None`` or non-int resolves to ``default``. Values below 1 or above
    ``maximum`` are clamped to the nearest boundary.
    """
    if not isinstance(value, int):
        return default
    if value < 1:
        return default
    return min(value, maximum)


def offset_in_bounds(value: Any, *, default: int, maximum: int | None = None) -> int:
    """Return a non-negative integer offset.

    ``None`` or non-int resolves to ``default``. Negative values resolve
    to ``default`` (callers that need strict rejection can validate before
    invoking). If ``maximum`` is supplied, values above it are clamped to it.
    """
    if not isinstance(value, int):
        return default
    if value < 0:
        return default
    if maximum is not None and value > maximum:
        return maximum
    return value


@dataclass(frozen=True)
class BoundedPage:
    """A deterministic, compact-JSON-budgeted page of items."""

    items: list[dict[str, Any]]
    total_count: int
    count: int
    limit: int
    offset: int
    returned_count: int
    next_offset: int | None
    truncated: bool
    byte_limited: bool
    envelope_truncated: bool
    max_limit: int
    serialized_item_bytes: int = 0
    item_truncated_ids: list[str] = field(default_factory=list)
    omitted_for_envelope_count: int = 0

    def model_dump(self, *, mode: str = "json") -> dict[str, Any]:
        return {
            "items": self.items,
            "count": self.count,
            "total_count": self.total_count,
            "limit": self.limit,
            "offset": self.offset,
            "returned_count": self.returned_count,
            "next_offset": self.next_offset,
            "truncated": self.truncated,
            "byte_limited": self.byte_limited,
            "envelope_truncated": self.envelope_truncated,
            "max_limit": self.max_limit,
            "serialized_item_bytes": self.serialized_item_bytes,
            "item_truncated_ids": self.item_truncated_ids,
            "omitted_for_envelope_count": self.omitted_for_envelope_count,
        }


def _item_id(item: dict[str, Any], id_key: str) -> str:
    raw = item.get(id_key)
    return str(raw) if raw is not None else ""


def _without_none_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_none_keys(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_none_keys(v) for v in value]
    return value


def _json_compact_bytes_len(value: Any) -> int:
    """Return the byte length of deterministic compact UTF-8 JSON for ``value``."""
    return len(json_compact_bytes(value))


def _build_bounded_page_dict(
    *,
    items: list[dict[str, Any]],
    arguments: dict[str, Any],
    default_limit: int,
    max_limit: int,
    per_item_budget: int,
    envelope_budget: int,
    id_key: str,
    items_key: str = "items",
    raise_on_oversized_item: bool = True,
    extra_envelope_overhead: dict[str, Any] | None = None,
    total_count: int | None = None,
    is_already_paged: bool = False,
) -> dict[str, Any]:
    """Core page-budgeting logic returning a plain dict for adapter layers."""
    limit = limit_in_bounds(arguments.get("limit"), default=default_limit, maximum=max_limit)

    if is_already_paged:
        # Upstream already paged; items are the requested window.
        effective_total = total_count if total_count is not None else len(items)
        offset = offset_in_bounds(arguments.get("offset"), default=0)
        sliced = items[:limit]
    else:
        # Upstream ignores pagination; we slice locally.
        effective_total = len(items)
        offset = offset_in_bounds(arguments.get("offset"), default=0, maximum=effective_total)
        if offset >= effective_total:
            sliced = []
        else:
            sliced = items[offset : offset + limit]

    # Validate the envelope overhead even when the requested window is empty. If
    # the fixed metadata/receipt/evidence overhead alone exceeds the budget, there
    # is no acceptable success response; surface the existing bounded-output
    # error rather than returning an oversized payload.
    fixed_overhead = dict(extra_envelope_overhead or {})
    # The final returned wrapper always includes ``byte_limited`` and
    # ``envelope_truncated``, even for metadata-only / zero-item responses, so
    # the synthetic empty-payload budget check must match that exact shape.
    empty_payload: dict[str, Any] = {
        **fixed_overhead,
        "total_count": effective_total,
        "count": effective_total,
        "limit": limit,
        "offset": offset,
        "returned_count": 0,
        "next_offset": None,
        "truncated": effective_total > limit,
        "byte_limited": False,
        "envelope_truncated": False,
        "max_limit": max_limit,
        "serialized_item_bytes": 0,
        "item_truncated_ids": [],
        "omitted_for_envelope_count": 0,
        items_key: [],
    }
    empty_payload_bytes = _json_compact_bytes_len(empty_payload)
    if empty_payload_bytes > envelope_budget:
        raise BoundedOutputError(
            f"projected metadata overhead exceeds the envelope budget of {envelope_budget} bytes",
            id_key=id_key,
            id_value="",
            item_bytes=empty_payload_bytes,
            budget=envelope_budget,
        )

    if not is_already_paged and offset >= effective_total:
        # Empty local window: fast path after the overhead check.
        return {
            "items": [],
            "total_count": effective_total,
            "count": effective_total,
            "limit": limit,
            "offset": offset,
            "returned_count": 0,
            "next_offset": None,
            "truncated": effective_total > limit,
            "byte_limited": False,
            "envelope_truncated": False,
            "max_limit": max_limit,
            "serialized_item_bytes": 0,
            "item_truncated_ids": [],
            "omitted_for_envelope_count": 0,
        }

    # Reject any single projected item that breaches the per-item budget.
    for item in sliced:
        item_bytes = _json_compact_bytes_len(item)
        if item_bytes > per_item_budget:
            raise BoundedOutputError(
                f"projected item exceeds the per-item budget of {per_item_budget} bytes",
                id_key=id_key,
                id_value=_item_id(item, id_key),
                item_bytes=item_bytes,
                budget=per_item_budget,
            )

    # Preserve order and drop from the end of the requested window until the
    # compact-JSON envelope fits. Suffix truncation guarantees deterministic
    # cursor continuity.
    omitted_ids: list[str] = []
    kept = list(sliced)

    # The final returned wrapper will include every key in ``envelope_overhead``,
    # including ``None``-valued keys, plus the standard pagination metadata. We
    # budget against that exact shape so the compact-JSON byte measurement is
    # byte-identical to the data the caller receives.
    envelope_overhead = dict(extra_envelope_overhead or {})

    def _payload(with_items: list[dict[str, Any]], *, next_offset: int | None, include_status: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            **envelope_overhead,
            "total_count": effective_total,
            "count": effective_total,
            "limit": limit,
            "offset": offset,
            "returned_count": len(with_items),
            "next_offset": next_offset,
            "truncated": effective_total > limit or bool(omitted_ids),
            "max_limit": max_limit,
            "serialized_item_bytes": 0,
            "item_truncated_ids": omitted_ids,
            "omitted_for_envelope_count": 0,
            items_key: with_items,
        }
        if include_status:
            payload["byte_limited"] = False
            payload["envelope_truncated"] = False
        return payload

    byte_limited = False
    envelope_truncated = False
    omitted_for_envelope_count = 0

    # Validate the zero-item envelope first. If the fixed overhead alone already
    # exceeds the budget, there is no acceptable success response; surface the
    # existing bounded-output error rather than returning an oversized payload.
    if _json_compact_bytes_len(_payload([], next_offset=None, include_status=False)) > envelope_budget:
        raise BoundedOutputError(
            f"projected metadata overhead exceeds the envelope budget of {envelope_budget} bytes",
            id_key=id_key,
            id_value="",
            item_bytes=_json_compact_bytes_len(envelope_overhead),
            budget=envelope_budget,
        )

    while kept:
        # next_offset does not materially affect payload size; use the
        # requested-window boundary for measurement parity.
        candidate_next = offset + len(sliced)
        candidate = _payload(kept, next_offset=candidate_next if candidate_next < effective_total else None)
        candidate_bytes = _json_compact_bytes_len(candidate)
        if candidate_bytes <= envelope_budget:
            break
        if len(kept) == 1:
            # The first item in the requested window does not fit.
            only = kept[0]
            only_bytes = _json_compact_bytes_len(only)
            raise BoundedOutputError(
                f"projected item exceeds the final envelope budget of {envelope_budget} bytes",
                id_key=id_key,
                id_value=_item_id(only, id_key),
                item_bytes=only_bytes,
                budget=envelope_budget,
            )
        byte_limited = True
        envelope_truncated = True
        omitted_for_envelope_count += 1
        removed = kept.pop()
        removed_id = _item_id(removed, id_key)
        if removed_id and removed_id not in omitted_ids:
            omitted_ids.append(removed_id)

    # Final verification: measure the exact shape that will be returned, with
    # actual flags populated, to guarantee no residual drift from the budgeted
    # candidate.
    final_payload = _payload(
        kept,
        next_offset=(offset + len(kept)) if (offset + len(kept)) < effective_total else None,
    )
    final_payload["byte_limited"] = byte_limited
    final_payload["envelope_truncated"] = envelope_truncated
    final_payload["serialized_item_bytes"] = max((_json_compact_bytes_len(item) for item in kept), default=0)
    if _json_compact_bytes_len(final_payload) > envelope_budget:
        raise BoundedOutputError(
            f"final wrapper exceeds the envelope budget of {envelope_budget} bytes",
            id_key=id_key,
            id_value="",
            item_bytes=_json_compact_bytes_len(final_payload),
            budget=envelope_budget,
        )

    # Resume immediately after the last delivered source item.
    delivered_end = offset + len(kept)
    next_offset: int | None = delivered_end if delivered_end < effective_total else None

    final_truncated = effective_total > limit or bool(omitted_ids) or byte_limited
    max_item_bytes = max((_json_compact_bytes_len(item) for item in kept), default=0)
    return {
        "items": kept,
        "count": effective_total,
        "total_count": effective_total,
        "limit": limit,
        "offset": offset,
        "returned_count": len(kept),
        "next_offset": next_offset,
        "truncated": final_truncated,
        "byte_limited": byte_limited,
        "envelope_truncated": envelope_truncated,
        "max_limit": max_limit,
        "serialized_item_bytes": max_item_bytes,
        "item_truncated_ids": omitted_ids,
        "omitted_for_envelope_count": omitted_for_envelope_count,
    }


def build_bounded_page(
    *,
    items: list[dict[str, Any]],
    arguments: dict[str, Any],
    default_limit: int,
    max_limit: int,
    per_item_budget: int,
    envelope_budget: int,
    id_key: str,
    items_key: str = "items",
    total_count: int | None = None,
    is_already_paged: bool = False,
    extra_envelope_overhead: dict[str, Any] | None = None,
) -> BoundedPage:
    """Slice ``items`` into a page that fits compact-JSON byte budgets."""
    page_dict = _build_bounded_page_dict(
        items=items,
        arguments=arguments,
        default_limit=default_limit,
        max_limit=max_limit,
        per_item_budget=per_item_budget,
        envelope_budget=envelope_budget,
        id_key=id_key,
        items_key=items_key,
        total_count=total_count,
        is_already_paged=is_already_paged,
        extra_envelope_overhead=extra_envelope_overhead,
    )
    return BoundedPage(
        items=page_dict["items"],
        total_count=page_dict["total_count"],
        count=page_dict["count"],
        limit=page_dict["limit"],
        offset=page_dict["offset"],
        returned_count=page_dict["returned_count"],
        next_offset=page_dict["next_offset"],
        truncated=page_dict["truncated"],
        byte_limited=page_dict["byte_limited"],
        envelope_truncated=page_dict["envelope_truncated"],
        max_limit=page_dict["max_limit"],
        serialized_item_bytes=page_dict["serialized_item_bytes"],
        item_truncated_ids=page_dict["item_truncated_ids"],
        omitted_for_envelope_count=page_dict["omitted_for_envelope_count"],
    )
