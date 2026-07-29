from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests

from resumo_meteo.sources.catalog import (
    GEFS_PRODUCT_SLUG,
    GFS_PRODUCT_SLUG,
    normalize_gefs_member_id,
)
from resumo_meteo.sources.http import (
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_DELAY_SECONDS,
    request_with_status_retry,
)

logger = logging.getLogger(__name__)

# ``requests`` treats its timeout as an idle socket timeout, not as a global
# deadline.  Keep it deliberately short: an individual range request is small
# and a stalled public object store must not hold the daily pipeline hostage.
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_STREAM_DEADLINE_SECONDS = 90
APCP_INTERVAL_RE = re.compile(r"(?P<start>\d+)-(?P<end>\d+) hour acc fcst")
GEFS_0P25_MAX_FORECAST_HOUR = 240


def gfs_s3_url(
    reference_date: date,
    *,
    cycle: str = "00",
    forecast_hour: int = 24,
) -> str:
    yyyymmdd = reference_date.strftime("%Y%m%d")
    canonical_cycle = str(cycle).zfill(2)
    return (
        "https://noaa-gfs-bdp-pds.s3.amazonaws.com/"
        f"gfs.{yyyymmdd}/{canonical_cycle}/atmos/"
        f"gfs.t{canonical_cycle}z.pgrb2.0p25.f{int(forecast_hour):03d}"
    )


def _gefs_s3_url(
    reference_date: date,
    *,
    file_id: str,
    cycle: str,
    forecast_hour: int,
) -> str:
    yyyymmdd = reference_date.strftime("%Y%m%d")
    canonical_cycle = str(cycle).zfill(2)
    lead = int(forecast_hour)
    if lead < 0:
        raise ValueError("forecast_hour GEFS nao pode ser negativo")
    if lead <= GEFS_0P25_MAX_FORECAST_HOUR:
        directory, product, resolution = "pgrb2sp25", "pgrb2s", "0p25"
    else:
        directory, product, resolution = "pgrb2ap5", "pgrb2a", "0p50"
    return (
        "https://noaa-gefs-pds.s3.amazonaws.com/"
        f"gefs.{yyyymmdd}/{canonical_cycle}/atmos/{directory}/"
        f"{file_id}.t{canonical_cycle}z.{product}.{resolution}.f{lead:03d}"
    )


def gefs_member_s3_url(
    reference_date: date,
    *,
    member_id: str | int,
    cycle: str = "00",
    forecast_hour: int = 24,
) -> str:
    return _gefs_s3_url(
        reference_date,
        file_id=normalize_gefs_member_id(member_id),
        cycle=cycle,
        forecast_hour=forecast_hour,
    )


def gefs_mean_s3_url(
    reference_date: date,
    *,
    cycle: str = "00",
    forecast_hour: int = 24,
) -> str:
    return _gefs_s3_url(
        reference_date,
        file_id="geavg",
        cycle=cycle,
        forecast_hour=forecast_hour,
    )


class NOAAPrecipitationError(RuntimeError):
    pass


class NOAAPrecipitationUnavailable(NOAAPrecipitationError):
    pass


@dataclass(frozen=True, slots=True)
class NOAAIndexRecord:
    message_number: int
    offset: int
    line: str
    variable: str
    level: str
    forecast_label: str
    interval_start_hour: int | None = None
    interval_end_hour: int | None = None
    next_offset: int | None = None

    @property
    def length(self) -> int | None:
        if self.next_offset is None:
            return None
        return max(int(self.next_offset) - int(self.offset), 0)


@dataclass(frozen=True, slots=True)
class NOAADownloadSelection:
    grib_url: str
    index_url: str
    record: NOAAIndexRecord
    byte_length: int | None = None
    member_id: str | None = None

    @property
    def range_header(self) -> str:
        if self.byte_length is None:
            return f"bytes={self.record.offset}-"
        return f"bytes={self.record.offset}-{self.record.offset + self.byte_length - 1}"


def parse_noaa_index(text: str) -> tuple[NOAAIndexRecord, ...]:
    raw_records = []
    for line in str(text or "").splitlines():
        parts = line.split(":")
        if len(parts) < 6:
            continue
        try:
            message_number = int(parts[0])
            offset = int(parts[1])
        except ValueError:
            continue

        forecast_label = parts[5]
        interval_start = None
        interval_end = None
        match = APCP_INTERVAL_RE.search(forecast_label)
        if match:
            interval_start = int(match.group("start"))
            interval_end = int(match.group("end"))

        raw_records.append(
            NOAAIndexRecord(
                message_number=message_number,
                offset=offset,
                line=line,
                variable=parts[3],
                level=parts[4],
                forecast_label=forecast_label,
                interval_start_hour=interval_start,
                interval_end_hour=interval_end,
            )
        )

    records = []
    for index, record in enumerate(raw_records):
        next_offset = raw_records[index + 1].offset if index + 1 < len(raw_records) else None
        records.append(
            NOAAIndexRecord(
                message_number=record.message_number,
                offset=record.offset,
                line=record.line,
                variable=record.variable,
                level=record.level,
                forecast_label=record.forecast_label,
                interval_start_hour=record.interval_start_hour,
                interval_end_hour=record.interval_end_hour,
                next_offset=next_offset,
            )
        )
    return tuple(records)


def find_apcp_interval_record(
    records: tuple[NOAAIndexRecord, ...],
    *,
    interval_start_hour: int,
    interval_end_hour: int,
) -> NOAAIndexRecord | None:
    for record in records:
        if (
            record.variable == "APCP"
            and record.level == "surface"
            and record.interval_start_hour == int(interval_start_hour)
            and record.interval_end_hour == int(interval_end_hour)
        ):
            return record
    return None


def _normalized_index_values(value) -> set[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        values = (value,)
    else:
        values = value
    return {str(item).strip() for item in values}


def select_noaa_index_records(
    records: Iterable[NOAAIndexRecord],
    *,
    variable=None,
    level=None,
) -> tuple[NOAAIndexRecord, ...]:
    expected_variables = _normalized_index_values(variable)
    expected_levels = _normalized_index_values(level)
    return tuple(
        record
        for record in records
        if (expected_variables is None or record.variable in expected_variables)
        and (expected_levels is None or record.level in expected_levels)
    )


class NOAAPrecipitationClient:
    def __init__(
        self,
        *,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        session=None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        max_retry_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS,
        stream_deadline_seconds: int = DEFAULT_STREAM_DEADLINE_SECONDS,
        sleep_fn=time.sleep,
    ):
        self.timeout = int(timeout or DEFAULT_TIMEOUT_SECONDS)
        self.session = session or requests.Session()
        self.max_attempts = int(max_attempts)
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self.max_retry_delay_seconds = float(max_retry_delay_seconds)
        self.stream_deadline_seconds = int(stream_deadline_seconds)
        if self.stream_deadline_seconds <= 0:
            raise ValueError("stream_deadline_seconds NOAA deve ser positivo")
        self.sleep_fn = sleep_fn

    def _get_with_retry(self, url: str, **kwargs):
        last_error: requests.RequestException | None = None
        # Status retries are handled by ``request_with_status_retry``.  This
        # loop is exclusively for connection/read exceptions; nesting both
        # policies multiplied one nominal three-attempt policy into nine.
        for attempt_index in range(self.max_attempts):
            try:
                return request_with_status_retry(
                    lambda: self.session.get(url, **kwargs),
                    max_attempts=1,
                    backoff_seconds=self.retry_backoff_seconds,
                    max_delay_seconds=self.max_retry_delay_seconds,
                    sleep_fn=self.sleep_fn,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt_index + 1 >= self.max_attempts:
                    break
                self.sleep_fn(
                    min(
                        self.retry_backoff_seconds * (2**attempt_index),
                        self.max_retry_delay_seconds,
                    )
                )
        if last_error is None:  # pragma: no cover - loop always attempts once.
            raise RuntimeError("request NOAA terminou sem resposta")
        raise last_error

    def _stream_bytes_with_retry(self, url: str, *, headers: Mapping[str, str]) -> bytes:
        last_error: requests.RequestException | None = None
        for attempt_index in range(self.max_attempts):
            response = None
            try:
                response = request_with_status_retry(
                    lambda: self.session.get(
                    url,
                    headers=dict(headers),
                    timeout=self.timeout,
                    stream=True,
                    ),
                    max_attempts=1,
                    backoff_seconds=self.retry_backoff_seconds,
                    max_delay_seconds=self.max_retry_delay_seconds,
                    sleep_fn=self.sleep_fn,
                )
                if response.status_code not in (200, 206):
                    response.raise_for_status()
                started = time.monotonic()
                chunks: list[bytes] = []
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if time.monotonic() - started > self.stream_deadline_seconds:
                        raise requests.Timeout(
                            f"download NOAA excedeu {self.stream_deadline_seconds}s"
                        )
                    if chunk:
                        chunks.append(chunk)
                return b"".join(chunks)
            except requests.RequestException as exc:
                last_error = exc
                if attempt_index + 1 >= self.max_attempts:
                    break
                self.sleep_fn(
                    min(
                        self.retry_backoff_seconds * (2**attempt_index),
                        self.max_retry_delay_seconds,
                    )
                )
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        if last_error is None:  # pragma: no cover - loop always attempts once.
            raise RuntimeError("download NOAA terminou sem resposta")
        raise last_error

    def grib_url(
        self,
        product_slug: str,
        reference_date: date,
        *,
        cycle: str,
        forecast_hour: int,
        member_id: str | int | None = None,
    ) -> str:
        if product_slug == GFS_PRODUCT_SLUG:
            if member_id is not None:
                raise ValueError("GFS deterministico nao aceita member_id")
            return gfs_s3_url(reference_date, cycle=cycle, forecast_hour=forecast_hour)
        if product_slug == GEFS_PRODUCT_SLUG:
            if member_id is not None:
                return gefs_member_s3_url(
                    reference_date,
                    member_id=member_id,
                    cycle=cycle,
                    forecast_hour=forecast_hour,
                )
            return gefs_mean_s3_url(reference_date, cycle=cycle, forecast_hour=forecast_hour)
        raise ValueError(f"Produto NOAA sem builder de URL: {product_slug}")

    def index_url(
        self,
        product_slug: str,
        reference_date: date,
        *,
        cycle: str,
        forecast_hour: int,
        member_id: str | int | None = None,
    ) -> str:
        grib_url = self.grib_url(
            product_slug,
            reference_date,
            cycle=cycle,
            forecast_hour=forecast_hour,
            member_id=member_id,
        )
        return f"{grib_url}.idx"

    def fetch_index(
        self,
        product_slug: str,
        reference_date: date,
        *,
        cycle: str,
        forecast_hour: int,
        member_id: str | int | None = None,
    ) -> tuple[NOAAIndexRecord, ...]:
        url = self.index_url(
            product_slug,
            reference_date,
            cycle=cycle,
            forecast_hour=forecast_hour,
            member_id=member_id,
        )
        try:
            response = self._get_with_retry(url, timeout=self.timeout)
            if response.status_code == 404:
                raise NOAAPrecipitationUnavailable(f"Indice NOAA nao disponivel: {url}")
            response.raise_for_status()
        except requests.RequestException as exc:
            raise NOAAPrecipitationError(f"Falha consultando indice NOAA: {url}") from exc
        return parse_noaa_index(response.text)

    def select_apcp_interval(
        self,
        product_slug: str,
        reference_date: date,
        *,
        cycle: str,
        interval_start_hour: int,
        interval_end_hour: int,
        member_id: str | int | None = None,
    ) -> NOAADownloadSelection:
        records = self.fetch_index(
            product_slug,
            reference_date,
            cycle=cycle,
            forecast_hour=interval_end_hour,
            member_id=member_id,
        )
        record = find_apcp_interval_record(
            records,
            interval_start_hour=interval_start_hour,
            interval_end_hour=interval_end_hour,
        )
        if record is not None:
            canonical_member_id = (
                normalize_gefs_member_id(member_id) if member_id is not None else None
            )
            return NOAADownloadSelection(
                grib_url=self.grib_url(
                    product_slug,
                    reference_date,
                    cycle=cycle,
                    forecast_hour=interval_end_hour,
                    member_id=member_id,
                ),
                index_url=self.index_url(
                    product_slug,
                    reference_date,
                    cycle=cycle,
                    forecast_hour=interval_end_hour,
                    member_id=member_id,
                ),
                record=record,
                byte_length=record.length,
                member_id=canonical_member_id,
            )
        raise NOAAPrecipitationUnavailable(
            "Mensagem APCP NOAA ausente: "
            f"{product_slug} {reference_date:%Y-%m-%d} {cycle}z f{interval_end_hour:03d} "
            f"{interval_start_hour}-{interval_end_hour}h"
            + (f" membro={normalize_gefs_member_id(member_id)}" if member_id is not None else "")
        )

    def select_gefs_member_apcp_interval(
        self,
        reference_date: date,
        *,
        cycle: str,
        interval_start_hour: int,
        interval_end_hour: int,
        member_id: str | int,
    ) -> NOAADownloadSelection:
        return self.select_apcp_interval(
            GEFS_PRODUCT_SLUG,
            reference_date,
            cycle=cycle,
            interval_start_hour=interval_start_hour,
            interval_end_hour=interval_end_hour,
            member_id=member_id,
        )

    def download_selection(self, selection: NOAADownloadSelection, output_path: str | Path) -> dict:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        headers = {"Range": selection.range_header}
        try:
            content = self._stream_bytes_with_retry(
                selection.grib_url,
                headers=headers,
            )
            path.write_bytes(content)
        except requests.RequestException as exc:
            path.unlink(missing_ok=True)
            raise NOAAPrecipitationError(f"Falha baixando mensagem NOAA: {selection.grib_url}") from exc
        result = {
            "path": str(path),
            "grib_url": selection.grib_url,
            "index_url": selection.index_url,
            "range": selection.range_header,
            "size_bytes": path.stat().st_size,
            "message_number": selection.record.message_number,
            "forecast_label": selection.record.forecast_label,
        }
        if selection.member_id is not None:
            result["member_id"] = selection.member_id
        return result
