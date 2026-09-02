"""Private cost receipts, replaceable pricing, and bounded paid-operation guards."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from soloscale.model_gateway import ModelCallProfile, ModelProviderId
from soloscale.resume_workspace import ResumeWorkspaceStorageError, _atomic_private_write

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,239}$")
_RECEIPT_ID = re.compile(r"^COST-[a-f0-9]{16}$")
_LEDGER_DIR = "cost-ledger"
_CATALOG_PATH = Path("media") / "pricing-catalog.json"
_POLICY_PATH = Path("media") / "budget-policy.json"


class MediaCostError(ValueError):
    """Raised when a cost contract or private receipt boundary is unsafe."""


class BudgetBlockedError(MediaCostError):
    """Raised before a paid operation that has no valid budget authorization."""


class PricingStatus(StrEnum):
    KNOWN = "KNOWN"
    ESTIMATED = "ESTIMATED"
    UNKNOWN = "UNKNOWN"
    NOT_BILLABLE = "NOT_BILLABLE"


class BillingUnit(StrEnum):
    INPUT_TOKEN = "INPUT_TOKEN"
    OUTPUT_TOKEN = "OUTPUT_TOKEN"
    TOKEN = "TOKEN"
    REQUEST = "REQUEST"
    SECOND = "SECOND"
    MINUTE = "MINUTE"
    CREDIT = "CREDIT"
    UNIT = "UNIT"


class CostReceiptStatus(StrEnum):
    ESTIMATED = "ESTIMATED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class BudgetGuardDecision(StrEnum):
    ALLOW = "ALLOW"
    WARN = "WARN"
    BLOCK = "BLOCK"
    UNKNOWN_COST = "UNKNOWN_COST"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PricingRate(_StrictModel):
    provider: str = Field(min_length=1, max_length=120)
    service: str = Field(min_length=1, max_length=120)
    model: str | None = Field(default=None, min_length=1, max_length=240)
    billing_unit: BillingUnit
    usd_per_unit: Decimal = Field(ge=0)
    pricing_status: Literal[
        PricingStatus.KNOWN,
        PricingStatus.ESTIMATED,
        PricingStatus.NOT_BILLABLE,
    ]
    currency: Literal["USD"] = "USD"
    effective_date: date
    source: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_rate(self) -> PricingRate:
        if self.pricing_status is PricingStatus.NOT_BILLABLE and self.usd_per_unit != 0:
            raise ValueError("not-billable pricing must have a zero rate")
        if _SAFE_ID.fullmatch(self.provider) is None or _SAFE_ID.fullmatch(self.service) is None:
            raise ValueError("pricing provider and service must use safe identifiers")
        return self


class PricingCatalog(_StrictModel):
    schema_version: str = "1.0"
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    entries: list[PricingRate] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def reject_duplicate_rates(self) -> PricingCatalog:
        keys = [
            (entry.provider, entry.service, entry.model, entry.billing_unit)
            for entry in self.entries
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("pricing catalog contains duplicate rates")
        return self

    def rate_for(
        self,
        *,
        provider: str,
        service: str,
        model: str | None,
        billing_unit: BillingUnit,
    ) -> PricingRate | None:
        exact = next(
            (
                entry
                for entry in self.entries
                if entry.provider == provider
                and entry.service == service
                and entry.model == model
                and entry.billing_unit is billing_unit
            ),
            None,
        )
        if exact is not None:
            return exact
        return next(
            (
                entry
                for entry in self.entries
                if entry.provider == provider
                and entry.service == service
                and entry.model is None
                and entry.billing_unit is billing_unit
            ),
            None,
        )


class CostEstimate(_StrictModel):
    schema_version: str = "1.0"
    feature: str = Field(min_length=1, max_length=120)
    operation: str = Field(min_length=1, max_length=160)
    provider: str = Field(min_length=1, max_length=120)
    service: str = Field(min_length=1, max_length=120)
    model: str | None = Field(default=None, min_length=1, max_length=240)
    billing_unit: BillingUnit
    billable_quantity: Decimal = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    audio_seconds: Decimal | None = Field(default=None, ge=0)
    video_seconds: Decimal | None = Field(default=None, ge=0)
    provider_credits: Decimal | None = Field(default=None, ge=0)
    request_count: int = Field(default=1, ge=0)
    pricing_status: PricingStatus
    price_source: str = Field(min_length=1, max_length=1000)
    estimated_cost_usd: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_pricing_semantics(self) -> CostEstimate:
        for value in (self.feature, self.operation, self.provider, self.service):
            if _SAFE_ID.fullmatch(value) is None:
                raise ValueError("cost estimate identifiers are unsafe")
        if self.pricing_status is PricingStatus.UNKNOWN:
            if self.estimated_cost_usd is not None:
                raise ValueError("unknown pricing cannot claim an estimated cost")
        elif self.pricing_status is PricingStatus.NOT_BILLABLE:
            if self.estimated_cost_usd != 0:
                raise ValueError("not-billable operations must report zero API cost")
        elif self.estimated_cost_usd is None:
            raise ValueError("known or estimated pricing requires a cost")
        return self


class CostReceipt(_StrictModel):
    schema_version: str = "1.0"
    receipt_id: str = Field(pattern=r"^COST-[a-f0-9]{16}$")
    run_id: str = Field(min_length=1, max_length=160)
    story_id: str | None = Field(default=None, min_length=1, max_length=160)
    video_run_id: str | None = Field(default=None, min_length=1, max_length=160)
    feature: str = Field(min_length=1, max_length=120)
    operation: str = Field(min_length=1, max_length=160)
    provider: str = Field(min_length=1, max_length=120)
    service: str = Field(min_length=1, max_length=120)
    model: str | None = Field(default=None, min_length=1, max_length=240)
    billing_unit: BillingUnit
    billable_quantity: Decimal = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    audio_seconds: Decimal | None = Field(default=None, ge=0)
    video_seconds: Decimal | None = Field(default=None, ge=0)
    provider_credits: Decimal | None = Field(default=None, ge=0)
    request_count: int = Field(default=1, ge=0)
    pricing_status: PricingStatus
    price_source: str = Field(min_length=1, max_length=1000)
    estimated_cost_usd: Decimal | None = Field(default=None, ge=0)
    actual_cost_usd: Decimal | None = Field(default=None, ge=0)
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    status: CostReceiptStatus
    provider_request_id: str | None = Field(default=None, min_length=1, max_length=240)

    @model_validator(mode="after")
    def validate_receipt(self) -> CostReceipt:
        CostEstimate(
            feature=self.feature,
            operation=self.operation,
            provider=self.provider,
            service=self.service,
            model=self.model,
            billing_unit=self.billing_unit,
            billable_quantity=self.billable_quantity,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            audio_seconds=self.audio_seconds,
            video_seconds=self.video_seconds,
            provider_credits=self.provider_credits,
            request_count=self.request_count,
            pricing_status=self.pricing_status,
            price_source=self.price_source,
            estimated_cost_usd=self.estimated_cost_usd,
        )
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("cost receipt finished_at precedes started_at")
        if self.status is not CostReceiptStatus.ESTIMATED and self.finished_at is None:
            raise ValueError("completed cost receipts require finished_at")
        return self


class BudgetPolicy(_StrictModel):
    schema_version: str = "1.0"
    per_paid_operation_usd: Decimal | None = Field(default=None, ge=0)
    per_story_usd: Decimal | None = Field(default=None, ge=0)
    per_video_usd: Decimal | None = Field(default=None, ge=0)
    daily_usd: Decimal | None = Field(default=None, ge=0)
    monthly_usd: Decimal | None = Field(default=None, ge=0)
    warning_ratio: Decimal = Field(default=Decimal("0.8"), gt=0, le=1)


class SpendSnapshot(_StrictModel):
    story_usd: Decimal = Field(default=Decimal(0), ge=0)
    video_usd: Decimal = Field(default=Decimal(0), ge=0)
    daily_usd: Decimal = Field(default=Decimal(0), ge=0)
    monthly_usd: Decimal = Field(default=Decimal(0), ge=0)


class BudgetEvaluation(_StrictModel):
    schema_version: str = "1.0"
    decision: BudgetGuardDecision
    estimated_cost_usd: Decimal | None = Field(default=None, ge=0)
    limiting_scope: str | None = Field(default=None, max_length=80)
    budget_usd: Decimal | None = Field(default=None, ge=0)
    current_spend_usd: Decimal | None = Field(default=None, ge=0)
    projected_spend_usd: Decimal | None = Field(default=None, ge=0)
    exceeded_by_usd: Decimal | None = Field(default=None, ge=0)


class PaidOperationAuthorization(_StrictModel):
    schema_version: str = "1.0"
    operation: str = Field(min_length=1, max_length=160)
    subject_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    estimate_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal[BudgetGuardDecision.ALLOW, BudgetGuardDecision.WARN]
    override_once: bool = False
    granted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CostSummary(_StrictModel):
    schema_version: str = "1.0"
    estimated_spend_usd: Decimal = Field(ge=0)
    actual_spend_usd: Decimal = Field(ge=0)
    unknown_cost_receipts: int = Field(ge=0)
    receipt_count: int = Field(ge=0)
    by_service_estimated_usd: dict[str, Decimal] = Field(default_factory=dict)


def _canonical_sha256(value: object) -> str:
    def json_ready(item: object) -> object:
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json")
        if isinstance(item, dict):
            return {str(key): json_ready(nested) for key, nested in item.items()}
        if isinstance(item, (list, tuple)):
            return [json_ready(nested) for nested in item]
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, (datetime, date)):
            return item.isoformat()
        if isinstance(item, StrEnum):
            return item.value
        return item

    raw = json.dumps(
        json_ready(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def operation_subject_sha256(operation: str, subject: object) -> str:
    return _canonical_sha256({"operation": operation, "subject": subject})


def default_pricing_catalog() -> PricingCatalog:
    """Return truthful local-only defaults; external provider prices stay unknown."""

    today = date.today()
    return PricingCatalog(
        entries=[
            PricingRate(
                provider="ollama",
                service="llm",
                billing_unit=BillingUnit.TOKEN,
                usd_per_unit=Decimal(0),
                pricing_status=PricingStatus.NOT_BILLABLE,
                effective_date=today,
                source="local-runtime",
            ),
            PricingRate(
                provider="local",
                service="qwen3_tts",
                billing_unit=BillingUnit.SECOND,
                usd_per_unit=Decimal(0),
                pricing_status=PricingStatus.NOT_BILLABLE,
                effective_date=today,
                source="local-runtime",
            ),
            PricingRate(
                provider="local",
                service="remotion",
                billing_unit=BillingUnit.REQUEST,
                usd_per_unit=Decimal(0),
                pricing_status=PricingStatus.NOT_BILLABLE,
                effective_date=today,
                source="local-runtime",
            ),
            PricingRate(
                provider="local",
                service="reference_video_analysis",
                billing_unit=BillingUnit.REQUEST,
                usd_per_unit=Decimal(0),
                pricing_status=PricingStatus.NOT_BILLABLE,
                effective_date=today,
                source="local-runtime",
            ),
        ]
    )


def _safe_private_path(data_root: Path, relative: Path) -> Path:
    root = data_root.resolve(strict=False)
    path = (root / relative).resolve(strict=False)
    if path == root or root not in path.parents:
        raise MediaCostError("cost storage path escapes the private data root")
    return path


def _load_json_model(path: Path, model: type[_StrictModel]) -> _StrictModel:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise MediaCostError("cost storage path is unsafe")
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise MediaCostError("cost storage is invalid") from exc


def load_pricing_catalog(data_root: Path) -> PricingCatalog:
    path = _safe_private_path(data_root, _CATALOG_PATH)
    try:
        loaded = _load_json_model(path, PricingCatalog)
    except FileNotFoundError:
        return default_pricing_catalog()
    assert isinstance(loaded, PricingCatalog)
    return loaded


def save_pricing_catalog(data_root: Path, catalog: PricingCatalog) -> Path:
    path = _safe_private_path(data_root, _CATALOG_PATH)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        _atomic_private_write(
            path,
            json.dumps(catalog.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
        )
    except (OSError, ResumeWorkspaceStorageError) as exc:
        raise MediaCostError("could not save the pricing catalog") from exc
    return path


def load_budget_policy(data_root: Path) -> BudgetPolicy:
    path = _safe_private_path(data_root, _POLICY_PATH)
    try:
        loaded = _load_json_model(path, BudgetPolicy)
    except FileNotFoundError:
        return BudgetPolicy()
    assert isinstance(loaded, BudgetPolicy)
    return loaded


def save_budget_policy(data_root: Path, policy: BudgetPolicy) -> Path:
    path = _safe_private_path(data_root, _POLICY_PATH)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        _atomic_private_write(
            path,
            json.dumps(policy.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
        )
    except (OSError, ResumeWorkspaceStorageError) as exc:
        raise MediaCostError("could not save the budget policy") from exc
    return path


def _not_billable_estimate(
    *,
    feature: str,
    operation: str,
    provider: str,
    service: str,
    model: str | None,
    billing_unit: BillingUnit,
    billable_quantity: Decimal,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    audio_seconds: Decimal | None = None,
    video_seconds: Decimal | None = None,
) -> CostEstimate:
    return CostEstimate(
        feature=feature,
        operation=operation,
        provider=provider,
        service=service,
        model=model,
        billing_unit=billing_unit,
        billable_quantity=billable_quantity,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        audio_seconds=audio_seconds,
        video_seconds=video_seconds,
        pricing_status=PricingStatus.NOT_BILLABLE,
        price_source="local-runtime",
        estimated_cost_usd=Decimal(0),
    )


def estimate_model_call(
    *,
    profile: ModelCallProfile,
    catalog: PricingCatalog,
    feature: str,
    operation: str,
) -> CostEstimate:
    provider = profile.provider.value
    input_tokens = profile.prompt_eval_tokens
    output_tokens = profile.output_tokens
    quantity = Decimal((input_tokens or 0) + (output_tokens or 0))
    if profile.provider is ModelProviderId.OLLAMA:
        return _not_billable_estimate(
            feature=feature,
            operation=operation,
            provider=provider,
            service="llm",
            model=profile.model,
            billing_unit=BillingUnit.TOKEN,
            billable_quantity=quantity,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    if input_tokens is None or output_tokens is None:
        return CostEstimate(
            feature=feature,
            operation=operation,
            provider=provider,
            service="llm",
            model=profile.model,
            billing_unit=BillingUnit.TOKEN,
            billable_quantity=quantity,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            pricing_status=PricingStatus.UNKNOWN,
            price_source="usage-or-price-unavailable",
        )
    input_rate = catalog.rate_for(
        provider=provider,
        service="llm",
        model=profile.model,
        billing_unit=BillingUnit.INPUT_TOKEN,
    )
    output_rate = catalog.rate_for(
        provider=provider,
        service="llm",
        model=profile.model,
        billing_unit=BillingUnit.OUTPUT_TOKEN,
    )
    if input_rate is None or output_rate is None:
        return CostEstimate(
            feature=feature,
            operation=operation,
            provider=provider,
            service="llm",
            model=profile.model,
            billing_unit=BillingUnit.TOKEN,
            billable_quantity=quantity,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            pricing_status=PricingStatus.UNKNOWN,
            price_source="pricing-catalog-miss",
        )
    status = (
        PricingStatus.ESTIMATED
        if PricingStatus.ESTIMATED
        in {input_rate.pricing_status, output_rate.pricing_status}
        else PricingStatus.KNOWN
    )
    return CostEstimate(
        feature=feature,
        operation=operation,
        provider=provider,
        service="llm",
        model=profile.model,
        billing_unit=BillingUnit.TOKEN,
        billable_quantity=quantity,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        pricing_status=status,
        price_source=f"{input_rate.source};{output_rate.source}",
        estimated_cost_usd=(
            Decimal(input_tokens) * input_rate.usd_per_unit
            + Decimal(output_tokens) * output_rate.usd_per_unit
        ),
    )


def estimate_avatar_seconds(
    *,
    seconds: Decimal,
    catalog: PricingCatalog,
    model: str | None = None,
    feature: str = "content_video",
    operation: str = "heygen.avatar_segment",
) -> CostEstimate:
    rate = catalog.rate_for(
        provider="heygen",
        service="avatar",
        model=model,
        billing_unit=BillingUnit.SECOND,
    )
    if rate is None:
        return CostEstimate(
            feature=feature,
            operation=operation,
            provider="heygen",
            service="avatar",
            model=model,
            billing_unit=BillingUnit.SECOND,
            billable_quantity=seconds,
            video_seconds=seconds,
            pricing_status=PricingStatus.UNKNOWN,
            price_source="pricing-catalog-miss",
        )
    estimated = seconds * rate.usd_per_unit
    return CostEstimate(
        feature=feature,
        operation=operation,
        provider="heygen",
        service="avatar",
        model=model,
        billing_unit=BillingUnit.SECOND,
        billable_quantity=seconds,
        video_seconds=seconds,
        pricing_status=rate.pricing_status,
        price_source=rate.source,
        estimated_cost_usd=estimated,
    )


def estimate_local_operation(
    *,
    service: str,
    feature: str,
    operation: str,
    quantity: Decimal = Decimal(1),
    billing_unit: BillingUnit = BillingUnit.REQUEST,
) -> CostEstimate:
    return _not_billable_estimate(
        feature=feature,
        operation=operation,
        provider="local",
        service=service,
        model=None,
        billing_unit=billing_unit,
        billable_quantity=quantity,
    )


def evaluate_budget(
    *,
    estimate: CostEstimate,
    policy: BudgetPolicy,
    spend: SpendSnapshot | None = None,
) -> BudgetEvaluation:
    if estimate.pricing_status is PricingStatus.UNKNOWN:
        return BudgetEvaluation(decision=BudgetGuardDecision.UNKNOWN_COST)
    amount = estimate.estimated_cost_usd or Decimal(0)
    if estimate.pricing_status is PricingStatus.NOT_BILLABLE:
        return BudgetEvaluation(
            decision=BudgetGuardDecision.ALLOW,
            estimated_cost_usd=amount,
        )
    current = spend or SpendSnapshot()
    checks = (
        ("per_paid_operation", policy.per_paid_operation_usd, Decimal(0)),
        ("per_story", policy.per_story_usd, current.story_usd),
        ("per_video", policy.per_video_usd, current.video_usd),
        ("daily", policy.daily_usd, current.daily_usd),
        ("monthly", policy.monthly_usd, current.monthly_usd),
    )
    selected: tuple[str, Decimal, Decimal, Decimal] | None = None
    warning: tuple[str, Decimal, Decimal, Decimal] | None = None
    for scope, limit, already_spent in checks:
        if limit is None:
            continue
        projected = already_spent + amount
        if projected > limit:
            selected = (scope, limit, already_spent, projected)
            break
        if limit > 0 and projected >= limit * policy.warning_ratio and warning is None:
            warning = (scope, limit, already_spent, projected)
    if selected is not None:
        scope, limit, already_spent, projected = selected
        return BudgetEvaluation(
            decision=BudgetGuardDecision.BLOCK,
            estimated_cost_usd=amount,
            limiting_scope=scope,
            budget_usd=limit,
            current_spend_usd=already_spent,
            projected_spend_usd=projected,
            exceeded_by_usd=projected - limit,
        )
    if warning is not None:
        scope, limit, already_spent, projected = warning
        return BudgetEvaluation(
            decision=BudgetGuardDecision.WARN,
            estimated_cost_usd=amount,
            limiting_scope=scope,
            budget_usd=limit,
            current_spend_usd=already_spent,
            projected_spend_usd=projected,
        )
    return BudgetEvaluation(
        decision=BudgetGuardDecision.ALLOW,
        estimated_cost_usd=amount,
    )


def authorize_paid_operation(
    *,
    estimate: CostEstimate,
    evaluation: BudgetEvaluation,
    subject: object,
    allow_once: bool = False,
) -> PaidOperationAuthorization:
    decision = evaluation.decision
    override = False
    if decision in {BudgetGuardDecision.BLOCK, BudgetGuardDecision.UNKNOWN_COST}:
        if not allow_once:
            raise BudgetBlockedError(f"paid operation is not authorized: {decision.value}")
        decision = BudgetGuardDecision.ALLOW
        override = True
    if decision is BudgetGuardDecision.ALLOW:
        authorized_decision: Literal[
            BudgetGuardDecision.ALLOW, BudgetGuardDecision.WARN
        ] = BudgetGuardDecision.ALLOW
    elif decision is BudgetGuardDecision.WARN:
        authorized_decision = BudgetGuardDecision.WARN
    else:
        raise BudgetBlockedError("paid operation is not authorized")
    return PaidOperationAuthorization(
        operation=estimate.operation,
        subject_sha256=operation_subject_sha256(estimate.operation, subject),
        estimate_sha256=_canonical_sha256(estimate),
        decision=authorized_decision,
        override_once=override,
    )


def make_cost_receipt(
    *,
    run_id: str,
    estimate: CostEstimate,
    status: CostReceiptStatus,
    started_at: datetime,
    finished_at: datetime | None = None,
    duration_ms: int | None = None,
    story_id: str | None = None,
    video_run_id: str | None = None,
    actual_cost_usd: Decimal | None = None,
    provider_request_id: str | None = None,
) -> CostReceipt:
    return CostReceipt(
        receipt_id=f"COST-{uuid4().hex[:16]}",
        run_id=run_id,
        story_id=story_id,
        video_run_id=video_run_id,
        **estimate.model_dump(mode="python", exclude={"schema_version"}),
        actual_cost_usd=actual_cost_usd,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
        status=status,
        provider_request_id=provider_request_id,
    )


def save_cost_receipt(data_root: Path, receipt: CostReceipt) -> Path:
    if _RECEIPT_ID.fullmatch(receipt.receipt_id) is None:
        raise MediaCostError("cost receipt ID is invalid")
    folder = _safe_private_path(data_root, Path(_LEDGER_DIR))
    path = folder / f"{receipt.receipt_id}.json"
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        _atomic_private_write(
            path,
            json.dumps(receipt.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
        )
    except (OSError, ResumeWorkspaceStorageError) as exc:
        raise MediaCostError("could not save the cost receipt") from exc
    return path


def load_cost_receipts(data_root: Path) -> list[CostReceipt]:
    folder = _safe_private_path(data_root, Path(_LEDGER_DIR))
    if not folder.exists():
        return []
    try:
        metadata = folder.lstat()
    except OSError as exc:
        raise MediaCostError("cost ledger is unavailable") from exc
    if folder.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise MediaCostError("cost ledger is unsafe")
    receipts: list[CostReceipt] = []
    for path in sorted(folder.glob("COST-*.json")):
        loaded = _load_json_model(path, CostReceipt)
        assert isinstance(loaded, CostReceipt)
        receipts.append(loaded)
    return receipts


def aggregate_costs(receipts: list[CostReceipt]) -> CostSummary:
    estimated = Decimal(0)
    actual = Decimal(0)
    unknown = 0
    by_service: dict[str, Decimal] = {}
    for receipt in receipts:
        if receipt.pricing_status is PricingStatus.UNKNOWN:
            unknown += 1
        estimated_value = receipt.estimated_cost_usd or Decimal(0)
        actual_value = receipt.actual_cost_usd or Decimal(0)
        estimated += estimated_value
        actual += actual_value
        by_service[receipt.service] = by_service.get(receipt.service, Decimal(0)) + estimated_value
    return CostSummary(
        estimated_spend_usd=estimated,
        actual_spend_usd=actual,
        unknown_cost_receipts=unknown,
        receipt_count=len(receipts),
        by_service_estimated_usd=by_service,
    )


def validate_paid_authorization(
    *,
    authorization: PaidOperationAuthorization,
    operation: str,
    subject: object,
) -> None:
    if authorization.operation != operation:
        raise BudgetBlockedError("paid-operation authorization is for another operation")
    expected = operation_subject_sha256(operation, subject)
    if authorization.subject_sha256 != expected:
        raise BudgetBlockedError("paid-operation authorization does not match the request")
