"""Interface operacional do pipeline standalone."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Settings


def _json(value: object) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def _settings() -> Settings:
    settings = Settings.from_env(project_root=Path.cwd())
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return settings


def _init_db(args: argparse.Namespace) -> int:
    from .storage.database import create_database_engine, run_migrations

    settings = _settings()
    settings.ensure_directories()
    engine = create_database_engine(settings.database_url)
    applied = run_migrations(engine)
    backend = engine.url.get_backend_name()
    engine.dispose()
    result = {"status": "ok", "database": backend, "applied_migrations": list(applied)}
    _json(result) if args.json else print(
        f"Banco pronto ({backend}); migrations aplicadas: {', '.join(applied) or 'nenhuma'}"
    )
    return 0


def _doctor(args: argparse.Namespace) -> int:
    from .health import run_health_checks

    settings = _settings()
    settings.ensure_directories()
    checks = run_health_checks(settings, include_database=not args.skip_database)
    result = {
        "status": "ok" if all(item.ok or not item.required for item in checks) else "failed",
        "checks": [item.to_dict() for item in checks],
    }
    if args.json:
        _json(result)
    else:
        for item in checks:
            marker = "OK" if item.ok else "FAIL"
            print(f"[{marker}] {item.name}: {item.detail}")
    return 0 if result["status"] == "ok" else 1


def _context(args: argparse.Namespace, settings: Settings) -> tuple[date, datetime]:
    if str(args.cycle).strip().lower().replace("z", "").zfill(2) != "00":
        raise ValueError("Somente a rodada 00z é autorizada")
    issue_date = (
        date.fromisoformat(args.issue_date)
        if args.issue_date
        else datetime.now(ZoneInfo(settings.timezone)).date()
    )
    cutoff = datetime.fromisoformat(args.cutoff.replace("Z", "+00:00")) if args.cutoff else datetime.now(UTC)
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("--cutoff deve incluir timezone, por exemplo 2026-07-13T15:00:00Z")
    return issue_date, cutoff.astimezone(UTC)


def _repository(settings: Settings):
    from .pipeline.runner import repository_from_settings

    return repository_from_settings(settings)


def _emit_result(result: dict, args: argparse.Namespace) -> None:
    if args.json:
        _json(result)
        return
    status = result.get("status", "unknown")
    print(f"status: {status}")
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else result
    if isinstance(summary, dict):
        if summary.get("run_id"):
            print(f"run_id: {summary['run_id']}")
        if summary.get("summary_path"):
            print(f"summary: {summary['summary_path']}")
        if summary.get("error_message"):
            print(f"erro: {summary['error_message']}")


def _ingest(args: argparse.Namespace) -> int:
    from .pipeline.ingest import ingest_required_inputs

    settings = _settings()
    issue_date, cutoff = _context(args, settings)
    report = ingest_required_inputs(
        _repository(settings),
        settings,
        issue_date=issue_date,
        cutoff_utc=cutoff,
        force=args.force,
        imerg_history_days=args.imerg_history_days,
    )
    result = {"status": "completed" if report.complete else "partial", **report.to_dict()}
    _emit_result(result, args)
    return 0 if report.complete else 2


def _build_packet(args: argparse.Namespace) -> int:
    from .pipeline.packet_builder import PacketBuildBlocked
    from .pipeline.runner import build_packet, write_packet_preview

    settings = _settings()
    issue_date, cutoff = _context(args, settings)
    try:
        packet = build_packet(
            _repository(settings), settings, issue_date=issue_date, cutoff_utc=cutoff
        )
    except PacketBuildBlocked as exc:
        result = {
            "status": "blocked",
            "message": str(exc),
            "assessment": exc.assessment,
        }
        _emit_result(result, args)
        return 2
    path = write_packet_preview(packet, settings)
    result = {
        "status": "completed",
        "issue_date": packet.issue.date.isoformat(),
        "quality": packet.quality.model_dump(mode="json"),
        "analysis_packet_path": path.relative_to(settings.project_root).as_posix(),
    }
    _emit_result(result, args)
    return 0


def _generate(args: argparse.Namespace) -> int:
    from .pipeline.runner import generate_daily_summary

    settings = _settings()
    issue_date, cutoff = _context(args, settings)
    result = generate_daily_summary(
        _repository(settings),
        settings,
        issue_date=issue_date,
        cutoff_utc=cutoff,
        force=args.force,
    )
    _emit_result(result, args)
    if args.show and result.get("summary_path"):
        print((settings.output_root / result["summary_path"]).read_text(encoding="utf-8"))
    return 0 if result.get("status") == "completed" else 2


def _run(args: argparse.Namespace) -> int:
    from .pipeline.runner import run_daily

    settings = _settings()
    issue_date, cutoff = _context(args, settings)
    result = run_daily(
        _repository(settings),
        settings,
        issue_date=issue_date,
        cutoff_utc=cutoff,
        force=args.force,
        imerg_history_days=args.imerg_history_days,
        freeze_cutoff=args.cutoff is not None,
    )
    _emit_result(result, args)
    summary = result.get("summary") or {}
    if args.show and summary.get("summary_path"):
        print((settings.output_root / summary["summary_path"]).read_text(encoding="utf-8"))
    return 0 if result.get("status") == "completed" else 2


def _status(args: argparse.Namespace) -> int:
    settings = _settings()
    issue_date, _cutoff = _context(args, settings)
    repository = _repository(settings)
    source_runs = repository.source_runs_for_date(issue_date, cycle=None)
    summary_runs = repository.summary_runs_for_date(issue_date)
    result = {
        "status": "found" if source_runs or summary_runs else "empty",
        "issue_date": issue_date.isoformat(),
        "sources": [
            {
                "id": row.id,
                "source_id": row.source_id,
                "reference_date": row.reference_date.isoformat(),
                "cycle": row.cycle,
                "status": row.status,
                "attempt_count": row.attempt_count,
                "max_lead_h": row.max_lead_h,
                "member_count": row.member_count,
                "artifact_count": len(row.artifacts),
                "available_artifact_bytes": sum(
                    int(item.size_bytes or 0)
                    for item in row.artifacts
                    if item.status == "available"
                ),
                "error_code": row.error_code or None,
                "error_message": row.error_message or None,
            }
            for row in source_runs
        ],
        "summaries": [
            {
                "id": row.id,
                "status": row.status,
                "model": row.llm_model,
                "generation_attempts": row.generation_attempts,
                "output_path": row.output_path or None,
                "error_code": row.error_code or None,
                "error_message": row.error_message or None,
            }
            for row in summary_runs
        ],
    }
    _emit_result(result, args)
    return 0


def _cleanup(args: argparse.Namespace) -> int:
    from .storage import ArtifactStore

    settings = _settings()
    repository = _repository(settings)
    values = repository.cleanup_expired_artifacts(
        dry_run=args.dry_run,
        limit=args.limit,
    )
    staging = (
        *repository.cleanup_stale_staging_directories(dry_run=args.dry_run),
        *ArtifactStore(settings.output_root).cleanup_stale_staging_directories(
            dry_run=args.dry_run
        ),
    )
    result = {
        "status": "completed",
        "dry_run": args.dry_run,
        "artifact_count": len(values),
        "bytes_reclaimed": sum(item.bytes_reclaimed for item in values),
        "errors": [item.error for item in values if item.error],
        "staging_directory_count": len(staging),
        "staging_errors": [item.error for item in staging if item.error],
        "artifacts": [
            {
                "id": item.artifact_id,
                "path": item.storage_path,
                "status": item.status,
                "bytes_reclaimed": item.bytes_reclaimed,
                "error": item.error or None,
            }
            for item in values
        ],
    }
    _emit_result(result, args)
    return 1 if result["errors"] or result["staging_errors"] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="resumo-meteo",
        description="Resumo meteorológico diário 00Z para o setor elétrico",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser("init-db", help="cria/atualiza o schema SQL")
    init_db.add_argument("--json", action="store_true")
    init_db.set_defaults(handler=_init_db)

    doctor = subparsers.add_parser("doctor", help="verifica configuração e dados estáticos")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--skip-database", action="store_true")
    doctor.set_defaults(handler=_doctor)

    handlers = {
        "ingest": _ingest,
        "build-packet": _build_packet,
        "generate": _generate,
        "run": _run,
        "status": _status,
        "cleanup": _cleanup,
    }
    for name, help_text in (
        ("ingest", "baixa e normaliza os inputs da rodada"),
        ("build-packet", "constrói e valida o analysis_packet"),
        ("generate", "redige a partir dos artefatos normalizados"),
        ("run", "executa ingestão, análise, redação e publicação"),
        ("status", "mostra o estado de uma execução diária"),
        ("cleanup", "apaga artefatos vencidos de modo seguro"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--date", dest="issue_date")
        command.add_argument("--cycle", default="00")
        command.add_argument("--cutoff", help="instante ISO-8601; default: agora UTC")
        command.add_argument("--json", action="store_true")
        if name in {"ingest", "generate", "run"}:
            command.add_argument("--force", action="store_true")
        if name in {"ingest", "run"}:
            command.add_argument("--imerg-history-days", type=int, default=30)
        if name in {"generate", "run"}:
            command.add_argument("--show", action="store_true", help="imprime o resumo final")
        if name == "cleanup":
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("--limit", type=int, default=1000)
        command.set_defaults(handler=handlers[name])
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        # Every repository command may bootstrap migrations, including status.
        # Hold one application-level lock for the complete CLI invocation when
        # SQLite is used, so a manual run cannot overlap a scheduler run.
        if args.command != "doctor" or not args.skip_database:
            from .storage.database import sqlite_process_lock

            settings = _settings()
            with sqlite_process_lock(settings.database_url):
                return int(args.handler(args))
        return int(args.handler(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        if getattr(args, "json", False):
            _json({"status": "error", "error_type": type(exc).__name__, "message": str(exc)})
        else:
            print(f"erro: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
