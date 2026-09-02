from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from soloscale.media_cost import (
    BillingUnit,
    BudgetBlockedError,
    BudgetGuardDecision,
    BudgetPolicy,
    CostReceiptStatus,
    PricingCatalog,
    PricingRate,
    PricingStatus,
    SpendSnapshot,
    aggregate_costs,
    authorize_paid_operation,
    estimate_avatar_seconds,
    estimate_local_operation,
    evaluate_budget,
    load_budget_policy,
    load_cost_receipts,
    load_pricing_catalog,
    make_cost_receipt,
    save_budget_policy,
    save_cost_receipt,
    save_pricing_catalog,
    validate_paid_authorization,
)


def _catalog() -> PricingCatalog:
    return PricingCatalog(
        entries=[
            PricingRate(
                provider="heygen",
                service="avatar",
                billing_unit=BillingUnit.SECOND,
                usd_per_unit=Decimal("0.0125"),
                pricing_status=PricingStatus.ESTIMATED,
                effective_date=date(2026, 8, 27),
                source="operator-entered-test-price",
            )
        ]
    )


def test_unit_pricing_and_unknown_pricing_are_truthful() -> None:
    known = estimate_avatar_seconds(
        seconds=Decimal("18"),
        catalog=_catalog(),
    )
    unknown = estimate_avatar_seconds(
        seconds=Decimal("18"),
        catalog=PricingCatalog(),
    )

    assert known.pricing_status is PricingStatus.ESTIMATED
    assert known.estimated_cost_usd == Decimal("0.2250")
    assert known.billing_unit is BillingUnit.SECOND
    assert unknown.pricing_status is PricingStatus.UNKNOWN
    assert unknown.estimated_cost_usd is None


def test_budget_guard_allows_warns_blocks_and_requires_unknown_override() -> None:
    estimate = estimate_avatar_seconds(
        seconds=Decimal("18"),
        catalog=_catalog(),
    )
    allow = evaluate_budget(
        estimate=estimate,
        policy=BudgetPolicy(per_video_usd=Decimal("2")),
    )
    warn = evaluate_budget(
        estimate=estimate,
        policy=BudgetPolicy(per_video_usd=Decimal("0.25")),
    )
    block = evaluate_budget(
        estimate=estimate,
        policy=BudgetPolicy(per_video_usd=Decimal("0.20")),
    )
    unknown = estimate_avatar_seconds(
        seconds=Decimal("18"), catalog=PricingCatalog()
    )
    unknown_decision = evaluate_budget(estimate=unknown, policy=BudgetPolicy())

    assert allow.decision is BudgetGuardDecision.ALLOW
    assert warn.decision is BudgetGuardDecision.WARN
    assert block.decision is BudgetGuardDecision.BLOCK
    assert block.exceeded_by_usd == Decimal("0.0250")
    assert unknown_decision.decision is BudgetGuardDecision.UNKNOWN_COST
    with pytest.raises(BudgetBlockedError):
        authorize_paid_operation(
            estimate=unknown,
            evaluation=unknown_decision,
            subject={"scene": "SCENE-01"},
        )
    override = authorize_paid_operation(
        estimate=unknown,
        evaluation=unknown_decision,
        subject={"scene": "SCENE-01"},
        allow_once=True,
    )
    assert override.override_once is True
    assert override.decision is BudgetGuardDecision.ALLOW


def test_budget_guard_uses_current_scope_spend() -> None:
    estimate = estimate_avatar_seconds(
        seconds=Decimal("18"),
        catalog=_catalog(),
    )
    result = evaluate_budget(
        estimate=estimate,
        policy=BudgetPolicy(daily_usd=Decimal("1")),
        spend=SpendSnapshot(daily_usd=Decimal("0.90")),
    )

    assert result.decision is BudgetGuardDecision.BLOCK
    assert result.limiting_scope == "daily"
    assert result.projected_spend_usd == Decimal("1.1250")


def test_paid_authorization_is_bound_to_the_exact_operation_subject() -> None:
    estimate = estimate_avatar_seconds(
        seconds=Decimal("18"),
        catalog=_catalog(),
    )
    evaluation = evaluate_budget(estimate=estimate, policy=BudgetPolicy())
    authorization = authorize_paid_operation(
        estimate=estimate,
        evaluation=evaluation,
        subject={"scene": "SCENE-01"},
    )
    validate_paid_authorization(
        authorization=authorization,
        operation="heygen.avatar_segment",
        subject={"scene": "SCENE-01"},
    )
    with pytest.raises(BudgetBlockedError, match="does not match"):
        validate_paid_authorization(
            authorization=authorization,
            operation="heygen.avatar_segment",
            subject={"scene": "SCENE-02"},
        )


def test_private_catalog_policy_and_receipt_persist_without_content(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    catalog_path = save_pricing_catalog(data_root, _catalog())
    policy_path = save_budget_policy(
        data_root,
        BudgetPolicy(monthly_usd=Decimal("100")),
    )
    estimate = estimate_avatar_seconds(
        seconds=Decimal("18"),
        catalog=load_pricing_catalog(data_root),
    )
    started = datetime.now(UTC)
    receipt = make_cost_receipt(
        run_id="content-run-01",
        story_id="M1-23",
        video_run_id="video-run-01",
        estimate=estimate,
        status=CostReceiptStatus.SUCCEEDED,
        started_at=started,
        finished_at=started + timedelta(seconds=2),
        duration_ms=2000,
        provider_request_id="synthetic-request-id",
    )
    receipt_path = save_cost_receipt(data_root, receipt)

    assert catalog_path.stat().st_mode & 0o777 == 0o600
    assert policy_path.stat().st_mode & 0o777 == 0o600
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    assert load_budget_policy(data_root).monthly_usd == Decimal("100")
    loaded = load_cost_receipts(data_root)
    assert loaded == [receipt]
    raw = receipt_path.read_text()
    assert "prompt" not in raw.casefold()
    assert "credential" not in raw.casefold()
    assert "/Users/" not in raw
    assert json.loads(raw)["provider_request_id"] == "synthetic-request-id"


def test_failed_paid_request_and_local_zero_cost_are_aggregated() -> None:
    paid = estimate_avatar_seconds(
        seconds=Decimal("18"),
        catalog=_catalog(),
    )
    local = estimate_local_operation(
        service="remotion",
        feature="content_video",
        operation="render",
    )
    started = datetime.now(UTC)
    failed = make_cost_receipt(
        run_id="content-run-01",
        estimate=paid,
        status=CostReceiptStatus.FAILED,
        started_at=started,
        finished_at=started + timedelta(milliseconds=400),
        duration_ms=400,
    )
    rendered = make_cost_receipt(
        run_id="content-run-01",
        estimate=local,
        status=CostReceiptStatus.SUCCEEDED,
        started_at=started,
        finished_at=started + timedelta(seconds=3),
        duration_ms=3000,
        actual_cost_usd=Decimal(0),
    )

    summary = aggregate_costs([failed, rendered])
    assert summary.estimated_spend_usd == Decimal("0.2250")
    assert summary.actual_spend_usd == 0
    assert summary.receipt_count == 2
    assert summary.by_service_estimated_usd == {
        "avatar": Decimal("0.2250"),
        "remotion": Decimal(0),
    }
