from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests

from resumo_meteo.sources.catalog import EC_PRODUCT_SLUG, ECENS_PRODUCT_SLUG
from resumo_meteo.sources.http import (
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_DELAY_SECONDS,
    request_with_status_retry,
)

# Range downloads are compact.  A short idle timeout prevents one unavailable
# mirror from blocking all ensemble members for minutes.
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_STREAM_DEADLINE_SECONDS = 90
ECMWF_OPEN_DATA_BASE_URL = "https://data.ecmwf.int/forecasts"


def configured_ecmwf_open_data_base_url(explicit_base_url: str | None = None) -> str:
    """Resolve the ECMWF host from an explicit value, environment, or default."""

    if explicit_base_url is not None and str(explicit_base_url).strip():
        return str(explicit_base_url).rstrip("/")
    value = os.environ.get("ECMWF_OPEN_DATA_BASE_URL", "")
    return str(value or ECMWF_OPEN_DATA_BASE_URL).rstrip("/")


class ECMWFPrecipitationError(RuntimeError):
    pass


class ECMWFPrecipitationUnavailable(ECMWFPrecipitationError):
    pass


@dataclass(frozen=True, slots=True)
class ECMWFIndexRecord:
    offset: int
    length: int
    metadata: dict

    @property
    def range_header(self) -> str:
        return f"bytes={self.offset}-{self.offset + self.length - 1}"

    @property
    def member_number(self) -> int | None:
        value = self.metadata.get("number")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


def parse_ecmwf_index(text: str) -> tuple[ECMWFIndexRecord, ...]:
    records = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            offset = int(item["_offset"])
            length = int(item["_length"])
        except (KeyError, TypeError, ValueError):
            continue
        records.append(ECMWFIndexRecord(offset=offset, length=length, metadata=item))
    return tuple(records)


def _normalized_metadata_values(value) -> set[str] | None:
    if value is None:
        return None
    if isinstance(value, (str, int, float)):
        values = (value,)
    else:
        values = value
    return {str(item).strip() for item in values}


def select_ecmwf_index_records(
    records: Iterable[ECMWFIndexRecord],
    *,
    param=None,
    level=None,
    level_type=None,
    data_type=None,
    stream=None,
    member_ids: Iterable[int | str] | int | str | None = None,
) -> tuple[ECMWFIndexRecord, ...]:
    expected_param = _normalized_metadata_values(param)
    expected_level = _normalized_metadata_values(level)
    expected_level_type = _normalized_metadata_values(level_type)
    expected_data_type = _normalized_metadata_values(data_type)
    expected_stream = _normalized_metadata_values(stream)
    expected_members = _normalized_metadata_values(member_ids)

    selected = []
    for record in records:
        metadata = record.metadata
        record_level = metadata.get("levelist", metadata.get("level"))
        if expected_param is not None and str(metadata.get("param")) not in expected_param:
            continue
        if expected_level is not None and str(record_level) not in expected_level:
            continue
        if expected_level_type is not None and str(metadata.get("levtype")) not in expected_level_type:
            continue
        if expected_data_type is not None and str(metadata.get("type")) not in expected_data_type:
            continue
        if expected_stream is not None and str(metadata.get("stream")) not in expected_stream:
            continue
        if expected_members is not None and str(metadata.get("number")) not in expected_members:
            continue
        selected.append(record)

    return tuple(
        sorted(
            selected,
            key=lambda item: (
                item.member_number is None,
                item.member_number or 0,
                item.offset,
            ),
        )
    )
class ECMWFOpenDataPrecipitationClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        session=None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        max_retry_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS,
        stream_deadline_seconds: int = DEFAULT_STREAM_DEADLINE_SECONDS,
        sleep_fn=time.sleep,
    ):
        self.base_url = configured_ecmwf_open_data_base_url(base_url)
        self.timeout = int(timeout or DEFAULT_TIMEOUT_SECONDS)
        self.session = session or requests.Session()
        self.max_attempts = int(max_attempts)
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self.max_retry_delay_seconds = float(max_retry_delay_seconds)
        self.stream_deadline_seconds = int(stream_deadline_seconds)
        if self.stream_deadline_seconds <= 0:
            raise ValueError("stream_deadline_seconds ECMWF deve ser positivo")
        self.sleep_fn = sleep_fn

    def _get_with_retry(self, url: str, **kwargs):
        last_error: requests.RequestException | None = None
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
            raise RuntimeError("request ECMWF terminou sem resposta")
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
                            f"download ECMWF excedeu {self.stream_deadline_seconds}s"
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
            raise RuntimeError("download ECMWF terminou sem resposta")
        raise last_error

    def _stream_type(self, product_slug: str) -> tuple[str, str]:
        if product_slug == EC_PRODUCT_SLUG:
            return "oper", "fc"
        if product_slug == ECENS_PRODUCT_SLUG:
            return "enfo", "ef"
        raise ValueError(f"Produto ECMWF sem builder de URL: {product_slug}")

    def _file_stem(self, product_slug: str, reference_date: date, *, cycle: str, forecast_hour: int) -> str:
        stream, data_type = self._stream_type(product_slug)
        yyyymmdd = reference_date.strftime("%Y%m%d")
        cycle = str(cycle).zfill(2)
        return (
            f"{self.base_url}/{yyyymmdd}/{cycle}z/ifs/0p25/{stream}/"
            f"{yyyymmdd}{cycle}0000-{int(forecast_hour)}h-{stream}-{data_type}"
        )

    def index_url(self, product_slug: str, reference_date: date, *, cycle: str, forecast_hour: int) -> str:
        return f"{self._file_stem(product_slug, reference_date, cycle=cycle, forecast_hour=forecast_hour)}.index"

    def grib_url(self, product_slug: str, reference_date: date, *, cycle: str, forecast_hour: int) -> str:
        return f"{self._file_stem(product_slug, reference_date, cycle=cycle, forecast_hour=forecast_hour)}.grib2"

    def fetch_index(
        self,
        product_slug: str,
        reference_date: date,
        *,
        cycle: str,
        forecast_hour: int,
    ) -> tuple[ECMWFIndexRecord, ...]:
        url = self.index_url(product_slug, reference_date, cycle=cycle, forecast_hour=forecast_hour)
        try:
            response = self._get_with_retry(url, timeout=self.timeout)
            if response.status_code == 404:
                raise ECMWFPrecipitationUnavailable(f"Indice ECMWF nao disponivel: {url}")
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ECMWFPrecipitationError(f"Falha consultando indice ECMWF: {url}") from exc
        return parse_ecmwf_index(response.text)

    def select_tp_messages(
        self,
        product_slug: str,
        reference_date: date,
        *,
        cycle: str,
        forecast_hour: int,
        member_ids: Iterable[int | str] | int | str | None = None,
    ) -> tuple[ECMWFIndexRecord, ...]:
        records = self.fetch_index(product_slug, reference_date, cycle=cycle, forecast_hour=forecast_hour)
        stream, data_type = self._stream_type(product_slug)
        requested_members = None
        if member_ids is not None:
            member_values = (
                (member_ids,) if isinstance(member_ids, (str, int)) else tuple(member_ids)
            )
            requested_members = {int(value) for value in member_values}
        selected = select_ecmwf_index_records(
            records,
            param="tp",
            stream=stream,
            data_type={data_type, "pf"},
            member_ids=requested_members,
        )
        if product_slug == EC_PRODUCT_SLUG:
            selected = tuple(
                record for record in selected if record.metadata.get("type") == data_type
            )
        if not selected:
            raise ECMWFPrecipitationUnavailable(
                f"Mensagem tp ECMWF ausente: {product_slug} {reference_date:%Y-%m-%d} {cycle}z {forecast_hour}h"
            )
        if requested_members is not None:
            selected_members = {
                record.member_number
                for record in selected
                if record.member_number is not None
            }
            missing_members = sorted(requested_members - selected_members)
            if missing_members:
                raise ECMWFPrecipitationUnavailable(
                    "Membros tp ECMWF ausentes: "
                    f"{product_slug} {reference_date:%Y-%m-%d} {cycle}z {forecast_hour}h "
                    f"members={missing_members}"
                )
        return tuple(selected)

    def download_messages(
        self,
        records: tuple[ECMWFIndexRecord, ...],
        *,
        grib_url: str,
        output_path: str | Path,
    ) -> dict:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        total_bytes = 0
        try:
            with open(path, "wb") as file_obj:
                for record in records:
                    content = self._stream_bytes_with_retry(
                        grib_url,
                        headers={"Range": record.range_header},
                    )
                    total_bytes += len(content)
                    file_obj.write(content)
        except requests.RequestException as exc:
            path.unlink(missing_ok=True)
            raise ECMWFPrecipitationError(f"Falha baixando mensagens ECMWF: {grib_url}") from exc
        return {
            "path": str(path),
            "grib_url": grib_url,
            "message_count": len(records),
            "size_bytes": total_bytes,
            "ranges": [record.range_header for record in records],
        }
