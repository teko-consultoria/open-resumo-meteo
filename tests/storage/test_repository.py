from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pytest

from resumo_meteo.storage import (
    IdempotencyConflict,
    InvalidStatusTransition,
    WeatherStorageRepository,
)

CUTOFF = datetime(2026, 7, 13, 12, tzinfo=UTC)


def add_forecast(
    repository: WeatherStorageRepository,
    *,
    model: str,
    reference_date: date = date(2026, 7, 13),
    source_slug: str | None = None,
    cycle: str = "00",
    max_lead_h: int = 336,
    member_count: int | None = None,
):
    source = source_slug or ("noaa" if model in {"gfs", "gefs"} else "ecmwf")
    family = "ncep" if model in {"gfs", "gefs"} else "ecmwf"
    run_time = datetime.combine(reference_date, datetime.min.time(), tzinfo=UTC)
    metadata = {}
    if model in {"gefs", "ecens"}:
        metadata["weather_summary_ensemble"] = {
            "json_path": f"normalized/{model}/{reference_date}/ensemble.json",
            "manifest_path": f"normalized/{model}/{reference_date}/manifest.json",
        }
    run, _ = repository.register_source_run(
        source_id=model,
        source_slug=source,
        product_slug=f"{model}-precipitation",
        data_kind="forecast",
        family=family,
        cycle=cycle,
        reference_date=reference_date,
        run_datetime_utc=run_time,
        available_at=CUTOFF - timedelta(hours=1),
        status="processed",
        max_lead_h=max_lead_h,
        member_count=member_count,
        normalized_metadata=metadata,
        idempotency_key=f"{source}-{model}-{reference_date}-{cycle}",
    )
    if model in {"gfs", "ec"}:
        repository.record_artifact(
            source_run_id=run.id,
            kind="grid_forecast",
            storage_path=(f"normalized/{source}/{model}/{reference_date}/{cycle}/day-001.csv.gz"),
            metadata={"forecast_date": reference_date.isoformat(), "model_prefix": model.upper()},
        )
    return run


def add_imerg(
    repository: WeatherStorageRepository,
    observation_date: date,
    *,
    temporal_resolution: str = "daily",
    period_complete: bool = True,
    end_hour: int = 24,
    id_suffix: str = "daily",
):
    start = datetime.combine(observation_date, datetime.min.time(), tzinfo=UTC)
    end = start + timedelta(hours=end_hour)
    run, _ = repository.register_source_run(
        source_id="gpm_imerg",
        source_slug="nasa",
        product_slug=f"gpm-imerg-{id_suffix}",
        data_kind="observed",
        reference_date=observation_date,
        valid_start_utc=start,
        valid_end_utc=end,
        available_at=min(CUTOFF - timedelta(minutes=30), end + timedelta(hours=6)),
        status="processed",
        temporal_resolution=temporal_resolution,
        period_complete=period_complete,
        coverage_pct=end_hour / 24 * 100,
        idempotency_key=f"imerg-{observation_date}-{id_suffix}",
    )
    repository.record_artifact(
        source_run_id=run.id,
        kind="grid_observed",
        storage_path=f"normalized/imerg/{observation_date}/{id_suffix}.csv.gz",
        metadata={"forecast_date": observation_date.isoformat()},
    )
    return run


def test_source_registration_is_idempotent_and_detects_identity_conflict(
    repository: WeatherStorageRepository,
) -> None:
    args = dict(
        source_id="gfs",
        source_slug="noaa",
        product_slug="gfs-precipitation",
        data_kind="forecast",
        reference_date=date(2026, 7, 13),
        cycle="00",
        idempotency_key="same-key",
    )

    first, created = repository.register_source_run(**args)
    second, created_again = repository.register_source_run(**args)

    assert created is True
    assert created_again is False
    assert first.id == second.id
    with pytest.raises(IdempotencyConflict):
        repository.register_source_run(**{**args, "source_id": "gefs"})


def test_source_status_transitions_are_audited(repository: WeatherStorageRepository) -> None:
    run, _ = repository.register_source_run(
        source_id="gfs",
        source_slug="noaa",
        product_slug="gfs",
        data_kind="forecast",
        reference_date=date(2026, 7, 13),
        status="pending",
    )

    run = repository.transition_source_run(run.id, "downloading", increment_attempt=True)
    run = repository.transition_source_run(run.id, "processing")
    run = repository.transition_source_run(run.id, "processed", max_lead_h=336)

    assert run.status == "processed"
    assert run.attempt_count == 1
    assert [
        event.to_status
        for event in repository.audit_events(entity_type="source_run", entity_id=run.id)
    ] == [
        "pending",
        "downloading",
        "processing",
        "processed",
    ]
    with pytest.raises(InvalidStatusTransition):
        repository.transition_source_run(run.id, "pending")


def test_discard_source_artifacts_removes_registered_files_after_failure(
    repository: WeatherStorageRepository,
) -> None:
    run, _ = repository.register_source_run(
        source_id="gfs",
        source_slug="noaa",
        product_slug="gfs-precipitation",
        data_kind="forecast",
        reference_date=date(2026, 7, 13),
        cycle="00",
        status="downloading",
    )
    relative_path = "processed_weather/noaa/gfs/attempt/grid.csv.gz"
    path = repository.artifacts.resolve(relative_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"partial normalized output")
    artifact, _ = repository.record_artifact(
        source_run_id=run.id,
        kind="grid_forecast",
        storage_path=relative_path,
        size_bytes=path.stat().st_size,
    )

    result = repository.discard_source_artifacts(run.id)

    assert result[0].status == "deleted"
    assert result[0].bytes_reclaimed == len(b"partial normalized output")
    assert not path.exists()
    assert repository.source_artifacts(run.id)[0].status == "deleted"
    assert [
        event.event_type
        for event in repository.audit_events(entity_type="artifact", entity_id=artifact.id)
    ] == ["registered", "discarded_after_source_failure"]


def test_model_selection_uses_only_authorized_00z_sources_and_keeps_two_families(
    repository: WeatherStorageRepository,
) -> None:
    wanted = date(2026, 7, 13)
    add_forecast(repository, model="gfs")
    add_forecast(repository, model="gefs", member_count=31)
    add_forecast(repository, model="ec", max_lead_h=240)
    add_forecast(repository, model="ecens", member_count=50)
    add_forecast(repository, model="gfs", source_slug="nasa", cycle="06")

    records, manifest = repository.select_model_inputs(
        cutoff_utc=CUTOFF, target_reference_date=wanted
    )

    assert set(records) == {"gfs", "gefs", "ec", "ecens"}
    assert records["gfs"].family == records["gefs"].family == "ncep"
    assert records["ec"].family == records["ecens"].family == "ecmwf"
    assert records["ec"].max_lead_h == 240
    assert records["ec"].product_data.processed_data["complete_storage"]["files"]
    assert manifest["required_cycle"] == "00"
    assert manifest["selected_reference_date"] == wanted.isoformat()


def test_ec_above_240_hours_is_retained_but_explicitly_partial(
    repository: WeatherStorageRepository,
) -> None:
    add_forecast(repository, model="ec", max_lead_h=336)

    records, _ = repository.select_model_inputs(
        cutoff_utc=CUTOFF, target_reference_date=date(2026, 7, 13)
    )

    assert records["ec"].status == "partial"
    assert "ec_lead_above_240h" in records["ec"].issues


def test_model_selection_prefers_complete_usable_run_over_newer_partial_attempt(
    repository: WeatherStorageRepository,
) -> None:
    complete = add_forecast(repository, model="gfs")
    partial, _ = repository.register_source_run(
        source_id="gfs",
        source_slug="noaa",
        product_slug="gfs-precipitation",
        data_kind="forecast",
        family="ncep",
        cycle="00",
        reference_date=date(2026, 7, 13),
        run_datetime_utc=datetime(2026, 7, 13, tzinfo=UTC),
        status="partial",
        max_lead_h=120,
        idempotency_key="gfs-newer-partial-attempt",
    )
    repository.record_artifact(
        source_run_id=partial.id,
        kind="grid_forecast",
        storage_path="normalized/gfs/partial/day-001.csv.gz",
    )

    records, _manifest = repository.select_model_inputs(
        cutoff_utc=datetime.now(UTC), target_reference_date=date(2026, 7, 13)
    )

    assert records["gfs"].product_data.id == complete.id
    assert records["gfs"].status == "complete"


def test_historical_selection_accepts_a_late_ingested_explicit_date(
    repository: WeatherStorageRepository,
) -> None:
    historical = date(2026, 1, 19)
    add_forecast(repository, model="gfs", reference_date=historical)
    add_forecast(repository, model="gfs", reference_date=date(2026, 7, 13))

    records, manifest = repository.select_model_inputs(
        cutoff_utc=CUTOFF, target_reference_date=historical
    )

    assert records["gfs"].reference_date == historical
    assert manifest["selected_reference_date"] == historical.isoformat()


def test_imerg_current_snapshot_and_history_obey_cutoff_and_end_date(
    repository: WeatherStorageRepository,
) -> None:
    older = add_imerg(repository, date(2026, 7, 12), id_suffix="late")
    preferred = add_imerg(
        repository,
        date(2026, 7, 12),
        temporal_resolution="30_minutes",
        id_suffix="early",
    )
    partial = add_imerg(
        repository,
        date(2026, 7, 13),
        temporal_resolution="30_minutes",
        period_complete=False,
        end_hour=7,
        id_suffix="snapshot",
    )

    current, source = repository.select_imerg_input(cutoff_utc=CUTOFF)
    history, history_source = repository.select_imerg_history(
        cutoff_utc=CUTOFF, days=1, end_date=date(2026, 7, 12)
    )

    assert current is not None and current.product_data.id == partial.id
    assert current.period_complete is False
    assert source.valid_end == "2026-07-13T07:00:00Z"
    assert [record.product_data.id for record in history] == [preferred.id]
    assert older.id != preferred.id
    assert history_source.details["requested_end_date"] == "2026-07-12"


def test_summary_run_identity_output_and_status_are_audited(
    repository: WeatherStorageRepository,
) -> None:
    kwargs = dict(
        issue_date=date(2026, 7, 13),
        issue_time=CUTOFF,
        cutoff_utc=CUTOFF,
        input_fingerprint="a" * 64,
        schema_version="1.0",
        prompt_version="v16",
        llm_provider="maritaca",
        llm_model="sabia-4",
    )
    first, created = repository.create_or_get_summary_run(**kwargs)
    second, created_again = repository.create_or_get_summary_run(**kwargs)

    assert created is True and created_again is False and first.id == second.id
    generating = repository.transition_summary_run(first.id, "generating", increment_attempt=True)
    output = "1ª pêntada...\n\n2ª pêntada...\n\n3ª pêntada..."
    completed = repository.transition_summary_run(
        generating.id,
        "completed",
        output_text=output,
        output_path="summaries/2026-07-13/summary.txt",
        audit_manifest_path="summaries/2026-07-13/audit.json",
        output_sha256=hashlib.sha256(output.encode()).hexdigest(),
        published_at=CUTOFF,
    )

    assert completed.generation_attempts == 1
    assert completed.output_text == output
    assert repository.completed_summary_for_date(date(2026, 7, 13)).id == completed.id
    assert [item.id for item in repository.summary_runs_for_date(date(2026, 7, 13))] == [
        completed.id
    ]
    assert [
        event.to_status
        for event in repository.audit_events(entity_type="summary_run", entity_id=first.id)
    ] == [
        "pending",
        "generating",
        "completed",
    ]


def test_summary_generation_claim_is_atomic_on_sqlite(
    repository: WeatherStorageRepository,
) -> None:
    run, _ = repository.create_or_get_summary_run(
        issue_date=date(2026, 7, 13),
        issue_time=CUTOFF,
        cutoff_utc=CUTOFF,
        input_fingerprint="b" * 64,
        schema_version="1.0",
        prompt_version="v17",
        llm_provider="maritaca",
        llm_model="sabia-4",
    )

    def claim() -> bool:
        _row, claimed = repository.claim_summary_run_for_generation(
            run.id,
            analysis_packet={"packet": 1},
            source_manifest={"sources": []},
            quality_manifest={"generation_allowed": True},
            now_utc=CUTOFF,
        )
        return claimed

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda _index: claim(), range(2)))

    assert sorted(claims) == [False, True]
    assert repository.get_summary_run(run.id).status == "generating"


def test_publication_cleanup_does_not_expire_active_source_artifacts(
    repository: WeatherStorageRepository,
) -> None:
    active, _ = repository.register_source_run(
        source_id="gfs",
        source_slug="noaa",
        product_slug="gfs-precipitation",
        data_kind="forecast",
        family="ncep",
        cycle="00",
        reference_date=date(2026, 7, 13),
        status="downloading",
        idempotency_key="active-gfs-download",
    )
    artifact, _ = repository.record_artifact(
        source_run_id=active.id,
        kind="raw_grib",
        storage_path="raw/gfs/active-download.grib2",
        retention_class="transient",
        delete_after=CUTOFF + timedelta(days=1),
    )

    assert repository.expire_transient_artifacts_through(date(2026, 7, 13)) == 0
    assert repository.source_artifacts(active.id)[0].id == artifact.id
    assert repository.source_artifacts(active.id)[0].delete_after == CUTOFF + timedelta(days=1)


def test_cleanup_removes_only_expired_transient_files_and_records_metadata(
    repository: WeatherStorageRepository,
) -> None:
    run = add_forecast(repository, model="gfs")
    transient_path = repository.artifacts.resolve("raw/gfs/download.grib2")
    transient_path.parent.mkdir(parents=True, exist_ok=True)
    transient_path.write_bytes(b"large raw payload")
    normalized_path = repository.artifacts.resolve("normalized/gfs/keep.csv.gz")
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_path.write_bytes(b"normalized")
    transient, _ = repository.record_artifact(
        source_run_id=run.id,
        kind="raw_grib",
        storage_path="raw/gfs/download.grib2",
        retention_class="transient",
        size_bytes=transient_path.stat().st_size,
        delete_after=CUTOFF - timedelta(minutes=1),
    )
    repository.record_artifact(
        source_run_id=run.id,
        kind="normalized_grid",
        storage_path="normalized/gfs/keep.csv.gz",
        retention_class="normalized",
    )

    preview = repository.cleanup_expired_artifacts(now_utc=CUTOFF, dry_run=True)
    assert preview[0].status == "would_delete"
    assert transient_path.exists()

    results = repository.cleanup_expired_artifacts(now_utc=CUTOFF)

    assert results[0].artifact_id == transient.id
    assert results[0].status == "deleted"
    assert not transient_path.exists()
    assert normalized_path.exists()
    assert repository.get_source_run(run.id).raw_cleaned_at == CUTOFF
    assert {item.status for item in repository.source_artifacts(run.id)} == {
        "available",
        "deleted",
    }
    cleanup_events = repository.audit_events(entity_type="artifact", entity_id=transient.id)
    assert cleanup_events[-1].event_type == "cleaned"


def test_cli_status_queries_return_daily_sources_and_filter_model_cycle(
    repository: WeatherStorageRepository,
) -> None:
    wanted = add_forecast(repository, model="gfs", cycle="00")
    add_forecast(repository, model="ec", cycle="06", max_lead_h=240)
    observed = add_imerg(repository, date(2026, 7, 13), end_hour=7)

    default_runs = repository.source_runs_for_date(date(2026, 7, 13))
    all_runs = repository.source_runs_for_date(date(2026, 7, 13), cycle=None)

    assert {item.id for item in default_runs} == {wanted.id, observed.id}
    assert {item.source_id for item in all_runs} == {"gfs", "ec", "gpm_imerg"}
    assert all(item.artifacts for item in all_runs)
