from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
import requests

from resumo_meteo.sources.catalog import (
    EC_PRODUCT_SLUG,
    ECENS_PRODUCT_SLUG,
    GEFS_MEMBER_IDS,
    GFS_PRODUCT_SLUG,
    get_model_spec,
    normalize_gefs_member_id,
)
from resumo_meteo.sources.ecmwf import (
    ECMWFOpenDataPrecipitationClient,
    parse_ecmwf_index,
    select_ecmwf_index_records,
)
from resumo_meteo.sources.http import request_with_status_retry
from resumo_meteo.sources.nasa import (
    DEFAULT_ALLOWED_HOSTS,
    GPM_IMERG_HALF_HOURLY_PRODUCT_SLUG,
    NASAGranule,
    NASAObservedWeatherClient,
    NASAObservedWeatherUnavailable,
)
from resumo_meteo.sources.noaa import (
    NOAAPrecipitationClient,
    find_apcp_interval_record,
    gefs_member_s3_url,
    gfs_s3_url,
    parse_noaa_index,
)


class FakeResponse:
    def __init__(self, *, status_code=200, text="", body=b"", json_body=None, headers=None):
        self.status_code = status_code
        self.text = text
        self.body = body
        self.json_body = json_body
        self.headers = headers or {}
        self.closed = False
        self.url = "https://example.test/result"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status={self.status_code}", response=self)

    def iter_content(self, chunk_size):
        del chunk_size
        yield self.body

    def json(self):
        return self.json_body

    def close(self):
        self.closed = True


class QueueSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_catalog_contains_only_authorized_models_and_complete_member_ids():
    assert {get_model_spec(key).model for key in ("gfs", "gefs", "ec", "ecens")} == {
        "gfs",
        "gefs",
        "ec",
        "ecens",
    }
    assert len(GEFS_MEMBER_IDS) == 31
    assert normalize_gefs_member_id(0) == "gec00"
    assert normalize_gefs_member_id("p30") == "gep30"
    with pytest.raises(ValueError, match="nao autorizado"):
        get_model_spec("icon")


def test_noaa_urls_and_index_interval_selection():
    reference = date(2026, 7, 13)
    assert gfs_s3_url(reference, forecast_hour=6).endswith("gfs.t00z.pgrb2.0p25.f006")
    assert "/pgrb2sp25/" in gefs_member_s3_url(
        reference, member_id="gep01", forecast_hour=240
    )
    assert "/pgrb2ap5/" in gefs_member_s3_url(
        reference, member_id="gep01", forecast_hour=246
    )
    records = parse_noaa_index(
        "1:0:d=2026071300:TMP:2 m above ground:6 hour fcst:\n"
        "2:123:d=2026071300:APCP:surface:0-6 hour acc fcst:\n"
        "3:456:d=2026071300:UGRD:10 m above ground:6 hour fcst:\n"
    )
    record = find_apcp_interval_record(records, interval_start_hour=0, interval_end_hour=6)
    assert record is not None
    assert record.offset == 123
    assert record.length == 333


def test_noaa_download_uses_exact_range_and_removes_partial_on_failure(tmp_path):
    index = FakeResponse(
        text=(
            "1:100:d=2026071300:APCP:surface:0-6 hour acc fcst:\n"
            "2:150:d=2026071300:TMP:2 m above ground:6 hour fcst:\n"
        )
    )
    body = FakeResponse(status_code=206, body=b"GRIB")
    session = QueueSession([index, body])
    client = NOAAPrecipitationClient(session=session, max_attempts=1)
    selection = client.select_apcp_interval(
        GFS_PRODUCT_SLUG,
        date(2026, 7, 13),
        cycle="00",
        interval_start_hour=0,
        interval_end_hour=6,
    )
    output = tmp_path / "field.grib2"
    client.download_selection(selection, output)
    assert output.read_bytes() == b"GRIB"
    assert session.calls[-1][1]["headers"] == {"Range": "bytes=100-149"}


def test_ecmwf_index_selection_and_urls_are_post_50r1_shape():
    text = "\n".join(
        (
            json.dumps(
                {
                    "_offset": 0,
                    "_length": 42,
                    "param": "tp",
                    "levtype": "sfc",
                    "stream": "enfo",
                    "type": "pf",
                    "number": 1,
                }
            ),
            json.dumps(
                {
                    "_offset": 42,
                    "_length": 50,
                    "param": "tp",
                    "levtype": "sfc",
                    "stream": "enfo",
                    "type": "pf",
                    "number": 2,
                }
            ),
        )
    )
    records = parse_ecmwf_index(text)
    selected = select_ecmwf_index_records(
        records,
        param="tp",
        level_type="sfc",
        stream="enfo",
        data_type="pf",
        member_ids=(2,),
    )
    assert len(selected) == 1
    assert selected[0].member_number == 2
    client = ECMWFOpenDataPrecipitationClient(base_url="https://example.test", max_attempts=1)
    assert "/ifs/0p25/oper/" in client.grib_url(
        EC_PRODUCT_SLUG, date(2026, 7, 13), cycle="00", forecast_hour=240
    )
    assert "/ifs/0p25/enfo/" in client.grib_url(
        ECENS_PRODUCT_SLUG, date(2026, 7, 13), cycle="00", forecast_hour=336
    )


def test_status_retry_is_bounded_and_honors_retry_after():
    responses = [
        FakeResponse(status_code=503, headers={"Retry-After": "0"}),
        FakeResponse(status_code=200),
    ]
    sleeps = []
    response = request_with_status_retry(
        lambda: responses.pop(0),
        max_attempts=2,
        sleep_fn=sleeps.append,
    )
    assert response.status_code == 200
    assert sleeps == [0.0]


def test_nasa_discovers_ordered_granules_and_sends_bearer_token(tmp_path):
    payload = {
        "feed": {
            "entry": [
                {
                    "id": "G2",
                    "title": "second",
                    "time_start": "2026-07-13T00:30:00Z",
                    "time_end": "2026-07-13T00:59:59Z",
                    "links": [{"href": "https://example.test/g2.HDF5", "rel": "data"}],
                },
                {
                    "id": "G1",
                    "title": "first",
                    "time_start": "2026-07-13T00:00:00Z",
                    "time_end": "2026-07-13T00:29:59Z",
                    "links": [{"href": "https://example.test/g1.HDF5", "rel": "data"}],
                },
            ]
        }
    }
    session = QueueSession([FakeResponse(json_body=payload), FakeResponse(body=b"HDF")])
    client = NASAObservedWeatherClient(
        earthdata_token="secret",
        session=session,
        allowed_hosts=(*DEFAULT_ALLOWED_HOSTS, "example.test"),
    )
    granules = client.find_granules(
        GPM_IMERG_HALF_HOURLY_PRODUCT_SLUG, date(2026, 7, 13)
    )
    assert [item.concept_id for item in granules] == ["G1", "G2"]
    output = tmp_path / "granule.h5"
    client.download_granule(granules[0], output)
    assert output.read_bytes() == b"HDF"
    assert session.calls[0][1]["headers"] == {}
    assert session.calls[-1][1]["headers"] == {"Authorization": "Bearer secret"}
    assert session.calls[-1][1]["stream"] is True


def test_nasa_rejects_unlisted_href_before_bearer_request(tmp_path):
    session = QueueSession([])
    client = NASAObservedWeatherClient(earthdata_token="top-secret", session=session)
    granule = NASAGranule(
        product_slug=GPM_IMERG_HALF_HOURLY_PRODUCT_SLUG,
        product_date=date(2026, 7, 13),
        title="malicious",
        concept_id="evil",
        url="https://attacker.example/granule.HDF5",
    )
    with pytest.raises(NASAObservedWeatherUnavailable, match="allowlist") as exc_info:
        client.download_granule(granule, tmp_path / "evil.h5")
    assert session.calls == []
    assert "top-secret" not in str(exc_info.value)
    assert not (tmp_path / "evil.h5").exists()


def test_nasa_rejects_unlisted_redirect_before_forwarding_bearer(tmp_path):
    redirect = FakeResponse(
        status_code=302,
        headers={"Location": "https://attacker.example/capture.HDF5"},
    )
    session = QueueSession([redirect])
    client = NASAObservedWeatherClient(
        earthdata_token="top-secret",
        session=session,
        allowed_hosts=(*DEFAULT_ALLOWED_HOSTS, "example.test"),
    )
    granule = NASAGranule(
        product_slug=GPM_IMERG_HALF_HOURLY_PRODUCT_SLUG,
        product_date=date(2026, 7, 13),
        title="redirect",
        concept_id="redirect",
        url="https://example.test/granule.HDF5",
    )
    with pytest.raises(NASAObservedWeatherUnavailable, match="allowlist"):
        client.download_granule(granule, tmp_path / "redirect.h5")
    assert len(session.calls) == 1
    assert session.calls[0][0] == "https://example.test/granule.HDF5"
    assert session.calls[0][1]["headers"] == {"Authorization": "Bearer top-secret"}
    assert not (tmp_path / "redirect.h5").exists()


def test_nasa_allows_exact_signed_cloudfront_redirect_without_forwarding_bearer(
    tmp_path,
):
    redirect = FakeResponse(
        status_code=303,
        headers={
            "Location": "https://d2b3c3wh8s6en5.cloudfront.net/signed/granule.HDF5"
        },
    )
    payload = FakeResponse(body=b"HDF")
    session = QueueSession([redirect, payload])
    client = NASAObservedWeatherClient(earthdata_token="top-secret", session=session)
    granule = NASAGranule(
        product_slug=GPM_IMERG_HALF_HOURLY_PRODUCT_SLUG,
        product_date=date(2026, 7, 13),
        title="signed redirect",
        concept_id="signed",
        url="https://data.gesdisc.earthdata.nasa.gov/granule.HDF5",
    )

    output = tmp_path / "redirect.h5"
    client.download_granule(granule, output)

    assert output.read_bytes() == b"HDF"
    assert session.calls[0][1]["headers"] == {"Authorization": "Bearer top-secret"}
    assert session.calls[1][1]["headers"] == {}


def test_nasa_granule_contract_serializes_utc():
    granule = NASAGranule(
        product_slug=GPM_IMERG_HALF_HOURLY_PRODUCT_SLUG,
        product_date=date(2026, 7, 13),
        title="g",
        concept_id="id",
        url="https://example.test/g.HDF5",
        start_utc=datetime(2026, 7, 13, tzinfo=UTC),
        end_utc=datetime(2026, 7, 13, 0, 30, tzinfo=UTC),
    )
    assert granule.as_dict()["start_utc"] == "2026-07-13T00:00:00Z"
