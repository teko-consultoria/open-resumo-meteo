from __future__ import annotations

import time as time_module
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from resumo_meteo.sources.http import (
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_DELAY_SECONDS,
    request_with_status_retry,
)

GPM_IMERG_DAILY_PRODUCT_SLUG = "gpm-imerg-precipitacao-observada-brasil"
GPM_IMERG_HALF_HOURLY_PRODUCT_SLUG = "gpm-imerg-early-30min-precipitacao-observada-brasil"

# IMERG granules are fetched one at a time and normalized immediately.  A
# bounded idle timeout lets the orchestration mark NASA unavailable instead of
# making the forecast wait indefinitely.
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_STREAM_DEADLINE_SECONDS = 90
CMR_GRANULES_URL = "https://cmr.earthdata.nasa.gov/search/granules.json"
DEFAULT_ALLOWED_HOSTS = (
    "cmr.earthdata.nasa.gov",
    "data.gesdisc.earthdata.nasa.gov",
    # GES DISC currently redirects authenticated downloads to this exact,
    # signed CloudFront distribution. Do not broaden this to *.cloudfront.net.
    "d2b3c3wh8s6en5.cloudfront.net",
    "gpm1.gesdisc.eosdis.nasa.gov",
    "urs.earthdata.nasa.gov",
)
SIGNED_PUBLIC_REDIRECT_HOSTS = frozenset({"d2b3c3wh8s6en5.cloudfront.net"})
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 5


class NASAObservedWeatherError(RuntimeError):
    pass


class NASAObservedWeatherUnavailable(NASAObservedWeatherError):
    pass


NASA_COLLECTIONS = {
    GPM_IMERG_DAILY_PRODUCT_SLUG: {
        "short_name": "GPM_3IMERGDL",
        "version": "07",
    },
    GPM_IMERG_HALF_HOURLY_PRODUCT_SLUG: {
        "short_name": "GPM_3IMERGHHE",
        "version": "07",
    },
}


@dataclass(frozen=True, slots=True)
class NASAGranule:
    product_slug: str
    product_date: date
    title: str
    concept_id: str
    url: str
    size_mb: float | None = None
    start_utc: datetime | None = None
    end_utc: datetime | None = None

    def as_dict(self) -> dict:
        return {
            "product_slug": self.product_slug,
            "product_date": self.product_date.isoformat(),
            "title": self.title,
            "concept_id": self.concept_id,
            "url": self.url,
            "size_mb": self.size_mb,
            "start_utc": (
                self.start_utc.astimezone(UTC).isoformat().replace("+00:00", "Z")
                if self.start_utc is not None
                else None
            ),
            "end_utc": (
                self.end_utc.astimezone(UTC).isoformat().replace("+00:00", "Z")
                if self.end_utc is not None
                else None
            ),
        }


def _date_range_temporal(product_date: date) -> str:
    start = datetime.combine(product_date, time.min).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = datetime.combine(product_date, time.max).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{start},{end}"


def _parse_date(value) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text[:10], text):
        for pattern in ("%Y-%m-%d", "%Y%m%d", "%Y.%m.%d"):
            try:
                return datetime.strptime(candidate, pattern).date()
            except ValueError:
                continue
    return None


def _parse_datetime_utc(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _granule_date(entry: dict, fallback: date) -> date:
    temporal = entry.get("time_start") or entry.get("updated") or ""
    parsed = _parse_date(temporal)
    return parsed or fallback


def _is_data_link(link: dict) -> bool:
    href = str(link.get("href") or "")
    if not href.startswith("http"):
        return False
    rel = str(link.get("rel") or "").lower()
    title = str(link.get("title") or "").lower()
    inherited = bool(link.get("inherited"))
    if inherited:
        return False
    if "opendap" in href.lower() or "documentation" in rel or "browse" in rel:
        return False
    if any(token in title for token in ("browse", "documentation", "metadata")):
        return False
    return any(href.lower().endswith(suffix) for suffix in (".h5", ".hdf5", ".he5", ".nc", ".nc4"))


def _normalized_allowed_hosts(hosts) -> frozenset[str]:
    values = frozenset(str(item or "").strip().lower().rstrip(".") for item in hosts)
    if not values or "" in values:
        raise ValueError("allowed_hosts NASA deve conter hosts explicitos")
    return values


def validate_nasa_https_url(url: str, *, allowed_hosts: frozenset[str]) -> str:
    """Validate a URL before any Earthdata credential can be attached."""

    text = str(url or "").strip()
    parsed = urlparse(text)
    host = str(parsed.hostname or "").lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError as exc:
        raise NASAObservedWeatherUnavailable("URL NASA possui porta invalida") from exc
    if (
        parsed.scheme.lower() != "https"
        or not host
        or host not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise NASAObservedWeatherUnavailable(
            "URL NASA recusada pela politica HTTPS/allowlist"
        )
    return text


class NASAObservedWeatherClient:
    def __init__(
        self,
        *,
        cmr_url: str = CMR_GRANULES_URL,
        earthdata_token: str = "",
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        session=None,
        allowed_hosts=DEFAULT_ALLOWED_HOSTS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        max_retry_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS,
        stream_deadline_seconds: int = DEFAULT_STREAM_DEADLINE_SECONDS,
        sleep_fn=time_module.sleep,
    ):
        self.allowed_hosts = _normalized_allowed_hosts(allowed_hosts)
        self.cmr_url = validate_nasa_https_url(
            str(cmr_url or CMR_GRANULES_URL),
            allowed_hosts=self.allowed_hosts,
        )
        self.earthdata_token = str(earthdata_token or "").strip()
        self.timeout = int(timeout or DEFAULT_TIMEOUT_SECONDS)
        self.session = session or requests.Session()
        self.max_attempts = int(max_attempts)
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self.max_retry_delay_seconds = float(max_retry_delay_seconds)
        self.stream_deadline_seconds = int(stream_deadline_seconds)
        if self.stream_deadline_seconds <= 0:
            raise ValueError("stream_deadline_seconds NASA deve ser positivo")
        self.sleep_fn = sleep_fn

    def is_configured(self) -> bool:
        return bool(self.earthdata_token)

    def _headers(self) -> dict:
        if not self.earthdata_token:
            return {}
        return {"Authorization": f"Bearer {self.earthdata_token}"}

    def _get_with_safe_redirects(
        self,
        url: str,
        *,
        headers: dict,
        params=None,
        stream: bool = False,
    ):
        current_url = validate_nasa_https_url(url, allowed_hosts=self.allowed_hosts)
        for redirect_index in range(MAX_REDIRECTS + 1):
            request_headers = dict(headers)
            current_host = str(urlparse(current_url).hostname or "").lower().rstrip(".")
            # The CloudFront target has a signed URL and does not need the
            # Earthdata bearer. Keeping it off the CDN request limits credential
            # exposure while still following only an exact allowlisted host.
            if current_host in SIGNED_PUBLIC_REDIRECT_HOSTS:
                request_headers.pop("Authorization", None)
            try:
                response = request_with_status_retry(
                    lambda current_url=current_url, request_headers=request_headers, first_redirect=(redirect_index == 0): self.session.get(
                        current_url,
                        params=params if first_redirect else None,
                        headers=request_headers,
                        timeout=self.timeout,
                        stream=stream,
                        allow_redirects=False,
                    ),
                    max_attempts=self.max_attempts,
                    backoff_seconds=self.retry_backoff_seconds,
                    max_delay_seconds=self.max_retry_delay_seconds,
                    sleep_fn=self.sleep_fn,
                )
            except requests.RequestException as exc:
                raise NASAObservedWeatherError(
                    f"Falha de rede NASA: {current_url}"
                ) from exc
            if response.status_code not in REDIRECT_STATUS_CODES:
                return response
            location = str((response.headers or {}).get("Location") or "").strip()
            if not location:
                response.close()
                raise NASAObservedWeatherUnavailable(
                    "Redirect NASA sem destino explicito"
                )
            target = urljoin(current_url, location)
            try:
                current_url = validate_nasa_https_url(
                    target, allowed_hosts=self.allowed_hosts
                )
            except NASAObservedWeatherUnavailable:
                response.close()
                raise
            response.close()
        raise NASAObservedWeatherUnavailable("Quantidade maxima de redirects NASA excedida")

    def find_granules(self, product_slug: str, product_date: date) -> tuple[NASAGranule, ...]:
        """Return every data granule intersecting one UTC calendar day.

        Daily products normally return one item.  The IMERG Early collection
        returns up to 48 ordered half-hourly granules.  CMR metadata is public;
        the Earthdata token is used only when the selected bytes are downloaded.
        """

        collection = NASA_COLLECTIONS.get(product_slug)
        if not collection:
            raise ValueError(f"Produto NASA observado desconhecido: {product_slug}")
        params = {
            "short_name": collection["short_name"],
            "version": collection["version"],
            "temporal": _date_range_temporal(product_date),
            "page_size": 2000,
            "sort_key": "start_date",
        }
        response = None
        try:
            # CMR discovery is public; never attach the bearer while parsing
            # third-party-controlled metadata links.
            response = self._get_with_safe_redirects(
                self.cmr_url,
                params=params,
                headers={},
            )
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise NASAObservedWeatherError(f"Falha consultando CMR NASA: {collection['short_name']}") from exc
        except ValueError as exc:
            raise NASAObservedWeatherError("Resposta CMR NASA nao e JSON valido") from exc
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        entries = ((body.get("feed") or {}).get("entry") or [])
        granules: list[NASAGranule] = []
        seen_concept_ids: set[str] = set()
        for entry in entries:
            links = entry.get("links") if isinstance(entry.get("links"), list) else []
            data_links = [link for link in links if isinstance(link, dict) and _is_data_link(link)]
            if not data_links:
                continue
            raw_url = str(data_links[0].get("href") or "")
            if not raw_url:
                continue
            try:
                url = validate_nasa_https_url(
                    raw_url, allowed_hosts=self.allowed_hosts
                )
            except NASAObservedWeatherUnavailable:
                continue
            concept_id = str(entry.get("id") or "")
            if concept_id and concept_id in seen_concept_ids:
                continue
            size_mb = None
            try:
                size_mb = float(entry.get("granule_size")) if entry.get("granule_size") else None
            except (TypeError, ValueError):
                size_mb = None
            granules.append(
                NASAGranule(
                    product_slug=product_slug,
                    product_date=_granule_date(entry, product_date),
                    title=str(entry.get("title") or ""),
                    concept_id=concept_id,
                    url=url,
                    size_mb=size_mb,
                    start_utc=_parse_datetime_utc(entry.get("time_start")),
                    end_utc=_parse_datetime_utc(entry.get("time_end")),
                )
            )
            if concept_id:
                seen_concept_ids.add(concept_id)
        return tuple(
            sorted(
                granules,
                key=lambda item: (
                    item.start_utc or datetime.combine(item.product_date, time.min, tzinfo=UTC),
                    item.concept_id,
                ),
            )
        )

    def download_granule(self, granule: NASAGranule, output_path: str | Path) -> dict:
        if not self.is_configured():
            raise NASAObservedWeatherUnavailable("NASA_EARTHDATA_TOKEN nao configurado")
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        response = None
        try:
            response = self._get_with_safe_redirects(
                granule.url,
                headers=self._headers(),
                stream=True,
            )
            if response.status_code in (401, 403):
                raise NASAObservedWeatherUnavailable(f"Credencial Earthdata recusada para {granule.url}")
            if response.status_code == 404:
                raise NASAObservedWeatherUnavailable(f"Granulo NASA nao encontrado: {granule.url}")
            response.raise_for_status()
            started = time_module.monotonic()
            with open(path, "wb") as file_obj:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if time_module.monotonic() - started > self.stream_deadline_seconds:
                        raise requests.Timeout(
                            f"download NASA excedeu {self.stream_deadline_seconds}s"
                        )
                    if chunk:
                        file_obj.write(chunk)
        except NASAObservedWeatherUnavailable:
            path.unlink(missing_ok=True)
            raise
        except (OSError, requests.RequestException) as exc:
            path.unlink(missing_ok=True)
            raise NASAObservedWeatherError(f"Falha baixando granulo NASA: {granule.url}") from exc
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        return {
            "url": granule.url,
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "concept_id": granule.concept_id,
            "title": granule.title,
        }
