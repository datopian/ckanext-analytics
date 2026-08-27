"""The request lifecycle, through the real ``request_finished`` signal.

CKAN's ``request_finished`` is a re-broadcast of Flask's, so connecting the
listener to Flask's signal exercises the same path CKAN drives - without needing
CKAN, a database or Solr::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -o addopts='' \
      ckanext/analytics/tests/test_event.py
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from flask import Blueprint, Flask, g, request, signals

from ckanext.analytics import event

FIELDS = {
    "timestamp",
    "request_id",
    "service",
    "method",
    "endpoint",
    "query_string",
    "action_type",
    "status_code",
    "user_agent",
    "request_ip",
    "user",
    "dataset",
    "resource",
    "organization",
    "group",
}


def action(logic_function: str):
    """Stands in for CKAN's single action view.

    Named ``action`` and registered on a blueprint named ``api``, which is what
    makes ``request.endpoint`` come out as ``api.action`` - the same as CKAN.
    """
    if logic_function == "explodes":
        raise RuntimeError("action exploded")
    if logic_function == "forbidden":
        return {"success": False}, 403
    if logic_function == "as_user":
        g.userobj = SimpleNamespace(id="0e5f1c34-user-uuid", name="analyst_jane")
    return {"success": True}


@pytest.fixture
def recorded(monkeypatch) -> list[dict]:
    """Capture what would be emitted, instead of logging it."""
    events: list[dict] = []
    monkeypatch.setattr(event, "emit_event", events.append)
    # The database is not part of these tests: entity extraction and the resolver
    # have their own, in test_entity.py. Here the resolver just echoes the refs.
    monkeypatch.setattr(
        event,
        "resolve_entities",
        lambda refs: {
            "dataset": refs.get("dataset"),
            "resource": refs.get("resource"),
            "organization": "neso" if any(refs.values()) else None,
        },
    )
    return events


def download(package_type: str, id: str, resource_id: str, filename: str | None = None):
    """Stands in for CKAN's resource download view.

    CKAN 2.11 registers it on two blueprints - ``dataset_resource`` for
    ``/dataset/...`` and ``resource`` for prefixed dataset types - so both
    endpoint names appear here, exactly as ``request.endpoint`` would report
    them.
    """
    if resource_id == "missing":
        return "not found", 404
    if resource_id == "linked":
        return "", 302, {"Location": "https://elsewhere.example/data.csv"}
    return "file bytes"


def download_blueprint(name: str, url_prefix: str) -> Blueprint:
    blueprint = Blueprint(
        name, __name__, url_prefix=url_prefix, url_defaults={"package_type": "dataset"}
    )
    blueprint.add_url_rule("/<resource_id>/download", view_func=download)
    blueprint.add_url_rule("/<resource_id>/download/<filename>", view_func=download)
    return blueprint


@pytest.fixture
def client(recorded):
    # static_folder=None: Flask would otherwise own the "static" endpoint.
    app = Flask(__name__, static_folder=None)
    app.testing = False  # let Flask turn a raising view into a 500, as CKAN does

    api = Blueprint("api", __name__, url_prefix="/api")
    api.add_url_rule("/3/action/<logic_function>", view_func=action, methods=["GET", "POST"])
    app.register_blueprint(api)

    # First registered wins in CKAN, and plugins register before core - so an
    # extension that overrides downloads (ckanext-s3filestore here) is what
    # actually serves /dataset/.../download on a real site. Core's blueprints
    # still exist below it, and prefixed dataset types keep their own.
    s3 = download_blueprint("s3_resource", "/dataset/<id>/resource")
    s3.add_url_rule("/<resource_id>/fs_download/<filename>", view_func=download,
                    endpoint="filesystem_resource_download")
    app.register_blueprint(s3)
    app.register_blueprint(download_blueprint("dataset_resource", "/dataset/<id>/resource"))
    app.register_blueprint(download_blueprint("resource", "/custom/<id>/resource"))

    @app.route("/dataset/costs-2026")
    def page():
        return "a web page"

    signals.request_finished.connect(event.record_request, app)
    try:
        yield app.test_client()
    finally:
        signals.request_finished.disconnect(event.record_request, app)


def test_one_event_per_api_call_with_the_whole_field_set(client, recorded):
    client.get("/api/3/action/package_show?id=costs-2026")

    assert len(recorded) == 1
    recorded_event = recorded[0]
    assert set(recorded_event) == FIELDS
    assert recorded_event["service"] == "ckan"
    assert recorded_event["method"] == "GET"
    assert recorded_event["action_type"] == "package_show"
    assert recorded_event["status_code"] == 200
    assert recorded_event["user"] is None


def test_the_endpoint_is_the_action_not_the_flask_endpoint(client, recorded):
    """Flask's endpoint is "api.action" for every action, so it cannot be used."""
    client.get("/api/3/action/package_show")
    client.post("/api/3/action/package_create", json={"name": "costs-2026"})

    assert [e["action_type"] for e in recorded] == ["package_show", "package_create"]


def test_the_endpoint_is_the_bare_path_and_the_query_is_its_own_field(client, recorded):
    """Split so a report can group by endpoint without ``?id=a`` and ``?id=b``
    counting apart."""
    client.get("/api/3/action/package_show?id=costs-2026&include_tracking=true")

    assert recorded[0]["endpoint"] == "/api/3/action/package_show"
    assert recorded[0]["query_string"] == "id=costs-2026&include_tracking=true"


def test_a_call_without_a_query_records_none_not_an_empty_string(client, recorded):
    client.get("/api/3/action/package_list")

    assert recorded[0]["query_string"] is None


def test_a_post_body_is_not_a_query_string(client, recorded):
    """Entity parameters sent in JSON only appear as the resolved entity fields."""
    client.post("/api/3/action/package_create", json={"name": "costs-2026"})

    assert recorded[0]["endpoint"] == "/api/3/action/package_create"
    assert recorded[0]["query_string"] is None


def test_a_download_endpoint_is_recorded_too(client, recorded):
    client.get("/dataset/costs-2026/resource/res-uuid/download/data.csv")

    assert recorded[0]["endpoint"] == "/dataset/costs-2026/resource/res-uuid/download/data.csv"


def test_request_id_comes_from_the_ingress_header(client, recorded):
    client.get("/api/3/action/package_show", headers={"X-Request-ID": "abc123-from-nginx"})

    assert recorded[0]["request_id"] == "abc123-from-nginx"


def test_request_id_is_generated_when_nothing_upstream_set_one(client, recorded):
    client.get("/api/3/action/package_show")

    assert recorded[0]["request_id"]


def test_user_agent_is_recorded(client, recorded):
    client.get("/api/3/action/package_show", headers={"User-Agent": "curl/8.4.0"})

    assert recorded[0]["user_agent"] == "curl/8.4.0"


def test_the_ip_comes_from_the_header_the_ingress_sets(client, recorded):
    client.get("/api/3/action/package_show", headers={"X-Real-IP": "203.0.113.7"})

    assert recorded[0]["request_ip"] == "203.0.113.7"


def test_the_ip_falls_back_to_the_last_forwarded_for_entry(client, recorded):
    """compute-full-forwarded-for appends, so the last hop is the ingress's own."""
    client.get(
        "/api/3/action/package_show",
        headers={"X-Forwarded-For": "10.0.0.9, 203.0.113.7"},
    )

    assert recorded[0]["request_ip"] == "203.0.113.7"


def test_the_ip_falls_back_to_the_peer_for_a_direct_call(client, recorded):
    client.get("/api/3/action/package_show")

    assert recorded[0]["request_ip"] == "127.0.0.1"


def test_a_request_with_the_ignore_header_is_not_recorded(client, recorded, monkeypatch):
    monkeypatch.setattr(event, "IGNORE_HEADER", "Request-Source")
    monkeypatch.setattr(event, "IGNORE_VALUES", frozenset({"data-explorer"}))

    client.get("/api/3/action/package_show", headers={"Request-Source": "data-explorer"})

    assert recorded == []


def test_the_ignore_header_match_is_case_insensitive(client, recorded, monkeypatch):
    monkeypatch.setattr(event, "IGNORE_HEADER", "Request-Source")
    monkeypatch.setattr(event, "IGNORE_VALUES", frozenset({"data-explorer"}))

    client.get("/api/3/action/package_show", headers={"request-source": "Data-Explorer"})

    assert recorded == []


def test_a_request_without_the_ignore_header_is_still_recorded(client, recorded, monkeypatch):
    monkeypatch.setattr(event, "IGNORE_HEADER", "Request-Source")
    monkeypatch.setattr(event, "IGNORE_VALUES", frozenset({"data-explorer"}))

    client.get("/api/3/action/package_show")

    assert len(recorded) == 1


def test_a_request_with_a_different_header_value_is_still_recorded(client, recorded, monkeypatch):
    monkeypatch.setattr(event, "IGNORE_HEADER", "Request-Source")
    monkeypatch.setattr(event, "IGNORE_VALUES", frozenset({"data-explorer"}))

    client.get("/api/3/action/package_show", headers={"Request-Source": "some-other-tool"})

    assert len(recorded) == 1


def test_the_ignore_check_is_disabled_when_unconfigured(client, recorded):
    """Default (module-level IGNORE_HEADER/IGNORE_VALUES empty) never skips."""
    client.get("/api/3/action/package_show", headers={"Request-Source": "data-explorer"})

    assert len(recorded) == 1


def test_user_agent_is_none_when_the_client_sends_none(client, recorded):
    client.get("/api/3/action/package_show", headers={"User-Agent": ""})

    assert recorded[0]["user_agent"] is None


def test_the_signed_in_user_is_recorded_by_name(client, recorded):
    client.get("/api/3/action/as_user")

    assert recorded[0]["user"] == "analyst_jane"


def test_an_anonymous_call_has_no_user(client, recorded):
    client.get("/api/3/action/package_show")

    assert recorded[0]["user"] is None


def test_the_dataset_comes_from_the_query_string_on_a_get(client, recorded):
    client.get("/api/3/action/package_show?id=costs-2026")

    assert recorded[0]["dataset"] == "costs-2026"
    assert recorded[0]["organization"] == "neso"
    assert recorded[0]["resource"] is None


def test_the_resource_comes_from_the_json_body_on_a_post(client, recorded):
    """The whole point of doing this inside CKAN: nginx cannot see a POST body."""
    client.post("/api/3/action/datastore_upsert", json={"resource_id": "res-uuid"})

    assert recorded[0]["resource"] == "res-uuid"
    assert recorded[0]["organization"] == "neso"


def test_a_refused_call_is_still_attributed(client, recorded):
    """Entities come from the request, so they survive a failure."""
    client.post("/api/3/action/forbidden", json={"id": "costs-2026"})

    assert recorded[0]["status_code"] == 403
    # "forbidden" is not a known action, so nothing is attributed to it
    assert recorded[0]["dataset"] is None


def test_a_search_is_recorded_without_an_entity(client, recorded):
    client.get("/api/3/action/package_search?q=costs")

    assert recorded[0]["action_type"] == "package_search"
    assert recorded[0]["dataset"] is None
    assert recorded[0]["resource"] is None
    assert recorded[0]["organization"] is None


def test_a_failing_resolver_does_not_lose_the_event(client, recorded, monkeypatch):
    def explode(refs):
        raise RuntimeError("no database here")

    monkeypatch.setattr(event, "resolve_entities", explode)

    client.get("/api/3/action/package_show?id=costs-2026")

    assert len(recorded) == 1
    assert recorded[0]["action_type"] == "package_show"
    assert recorded[0]["dataset"] is None


def test_an_attempt_that_is_refused_is_still_recorded(client, recorded):
    """The point of using request_finished: failures are half the data."""
    response = client.post("/api/3/action/forbidden", json={})

    assert response.status_code == 403
    assert recorded[0]["status_code"] == 403
    assert recorded[0]["action_type"] == "forbidden"


def test_an_action_that_crashes_is_recorded_as_a_500(client, recorded):
    response = client.get("/api/3/action/explodes")

    assert response.status_code == 500
    assert recorded[0]["status_code"] == 500


def test_web_pages_are_not_recorded(client, recorded):
    response = client.get("/dataset/costs-2026")

    assert response.status_code == 200
    assert recorded == []


def test_a_resource_download_is_recorded_with_the_whole_field_set(client, recorded):
    client.get("/dataset/costs-2026/resource/res-uuid/download")

    assert len(recorded) == 1
    recorded_event = recorded[0]
    assert set(recorded_event) == FIELDS
    assert recorded_event["action_type"] == "resource_download"
    assert recorded_event["method"] == "GET"
    assert recorded_event["status_code"] == 200


def test_a_download_is_attributed_to_the_resource_and_its_dataset(client, recorded):
    """The URL names both, so neither needs a lookup to be attributed."""
    client.get("/dataset/costs-2026/resource/res-uuid/download")

    assert recorded[0]["resource"] == "res-uuid"
    assert recorded[0]["dataset"] == "costs-2026"


def test_a_download_with_a_filename_is_recorded(client, recorded):
    client.get("/dataset/costs-2026/resource/res-uuid/download/data.csv")

    assert recorded[0]["action_type"] == "resource_download"
    assert recorded[0]["resource"] == "res-uuid"


def test_a_download_on_a_prefixed_dataset_type_is_recorded(client, recorded):
    """CKAN registers the same view twice; the ``resource`` blueprint counts too."""
    client.get("/custom/costs-2026/resource/res-uuid/download")

    assert recorded[0]["action_type"] == "resource_download"
    assert recorded[0]["resource"] == "res-uuid"


def test_a_download_redirect_to_an_external_url_is_recorded(client, recorded):
    """Linked (not uploaded) resources answer 302; the download still counts."""
    response = client.get("/dataset/costs-2026/resource/linked/download")

    assert response.status_code == 302
    assert recorded[0]["status_code"] == 302
    assert recorded[0]["resource"] == "linked"


def test_a_refused_download_is_recorded_with_its_status(client, recorded):
    response = client.get("/dataset/costs-2026/resource/missing/download")

    assert response.status_code == 404
    assert recorded[0]["status_code"] == 404


def test_a_filesystem_fallback_download_is_recorded(client, recorded):
    """s3filestore's second download view, for files still on disk."""
    client.get("/dataset/costs-2026/resource/res-uuid/fs_download/data.csv")

    assert recorded[0]["action_type"] == "resource_download"
    assert recorded[0]["resource"] == "res-uuid"


@pytest.mark.parametrize(
    "endpoint",
    [
        "dataset_resource.download",  # CKAN core
        "resource.download",  # CKAN core, prefixed dataset types
        "showcase_resource.download",  # a custom IDatasetForm package type
        "s3_resource.download",  # ckanext-s3filestore override
        "s3_resource.filesystem_resource_download",  # its on-disk fallback
    ],
)
def test_download_endpoints_are_recognised_wherever_they_come_from(endpoint):
    """Extensions override the download route under their own blueprint name,
    so downloads are recognised by shape - a resource blueprint's download
    view - not by a list of names."""
    assert event.RequestEvent.is_download_endpoint(endpoint)


@pytest.mark.parametrize(
    "endpoint",
    ["api.action", "dataset.read", "resource.read", "resource.views", "static", None],
)
def test_other_endpoints_are_not_downloads(endpoint):
    assert not event.RequestEvent.is_download_endpoint(endpoint)


def test_a_broken_emit_does_not_break_the_request(monkeypatch):
    def explode(_event):
        raise RuntimeError("the stream is down")

    monkeypatch.setattr(event, "emit_event", explode)

    app = Flask(__name__, static_folder=None)
    app.testing = False
    api = Blueprint("api", __name__, url_prefix="/api")
    api.add_url_rule("/3/action/<logic_function>", view_func=action)
    app.register_blueprint(api)
    signals.request_finished.connect(event.record_request, app)

    try:
        response = app.test_client().get("/api/3/action/package_show")
    finally:
        signals.request_finished.disconnect(event.record_request, app)

    assert response.status_code == 200


def test_the_emitted_line_is_bare_json(caplog):
    """Nothing downstream can parse the event unless the line is only the object."""
    import json

    with caplog.at_level("INFO", logger="ckanext.analytics.event"):
        event.emit_event({"action_type": "package_show", "status_code": 200})

    assert json.loads(caplog.records[0].getMessage()) == {
        "action_type": "package_show",
        "status_code": 200,
    }
