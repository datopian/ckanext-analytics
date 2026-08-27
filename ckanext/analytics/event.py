"""One structured event per Action API call or resource download.

Only ``/api/{ver}/action/<name>`` requests and resource downloads are recorded. Web
pages and static files are not: the ingress log already covers URL-level traffic, and
what it cannot see is what this records - the action name for a POST, who called it,
and which dataset, resource, organization or group it acted on. Downloads are
recorded as ``resource_download``, attributed to the resource and dataset in the URL.

Both attempted and successful calls are recorded. ``status_code`` tells them apart,
which is why this hangs off ``request_finished`` and not ``action_succeeded`` - the
latter fires only on success, so every 403, 409 and 500 would be missing.

A request carrying the configured ignore header/value (``CKANEXT_ANALYTICS_IGNORE_HEADER``
/ ``CKANEXT_ANALYTICS_IGNORE_VALUES``) is skipped entirely - not logged with a
distinguishing field, just never recorded.

``Attribution`` holds the rules, ``RequestEvent`` turns a request into a dict, and
``record_request`` is the listener CKAN calls. Flask only, no CKAN import, so the
whole lifecycle can be driven against a stub app and the real signal.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from flask import g, request as current_request

from ckanext.analytics.entity import ENTITIES, resolve_entities

log = logging.getLogger(__name__)

#: A logger of its own, so the event stream can be selected by name without scraping
#: the rest of CKAN's output.
api = logging.getLogger("analytics.event")

#: Named in every event, so one stream can carry more than one service.
SERVICE = os.environ.get("CKANEXT_ANALYTICS_SERVICE", "ckan")

#: A request whose ``IGNORE_HEADER`` value is in ``IGNORE_VALUES`` skips
#: analytics entirely - not logged with a distinguishing field, just never
#: recorded. Known internal callers (e.g. the data explorer) set this so
#: their UI-driven traffic never counts as API usage. Either empty disables
#: the check. Comma-separated, matched case-insensitively.
IGNORE_HEADER = os.environ.get("CKANEXT_ANALYTICS_IGNORE_HEADER", "")
IGNORE_VALUES = frozenset(
    v.strip().lower()
    for v in os.environ.get("CKANEXT_ANALYTICS_IGNORE_VALUES", "").split(",")
    if v.strip()
)


class Attribution:
    """Which entity an action refers to, and in which parameter.

    By rule rather than by a list of actions, because CKAN has around two hundred and
    extensions add more. Two layers, in order:

    1. **Parameters that name an entity outright**, read whatever the action is. This
       is what covers actions nobody here has heard of, including from other
       extensions.
    2. **What ``id`` means for the action's family** - a dataset for ``package_*``, a
       resource for ``resource_*``, and so on.

    Users are deliberately not an entity here: the event's ``user`` field is who made
    the call, and who an action *acted on* is not recorded.
    """

    #: Read first, so an explicit ``resource_id`` always beats a guess from ``id``.
    NAMED_PARAMS: tuple[tuple[str, str], ...] = (
        ("resource_id", "resource"),
        ("package_id", "dataset"),
        ("dataset_id", "dataset"),
        ("organization_id", "organization"),
        ("owner_org", "organization"),
        ("group_id", "group"),
    )

    #: Longest prefix wins, so ``organization_`` is matched before ``group_`` could be.
    ID_MEANS_PREFIX: tuple[tuple[str, str], ...] = (
        ("organization_", "organization"),
        ("package_", "dataset"),
        ("dataset_", "dataset"),
        ("resource_", "resource"),
        ("group_", "group"),
        ("member_", "group"),
    )

    #: Actions whose name does not carry its family. ``resource_download`` is the
    #: download endpoint, where ``id`` in the URL is the dataset - checked before the
    #: prefixes, or ``resource_`` would claim it.
    ID_MEANS_EXACT: dict[str, str] = {
        "resource_download": "dataset",
        "follow_dataset": "dataset",
        "unfollow_dataset": "dataset",
        "am_following_dataset": "dataset",
        "follow_group": "group",
        "unfollow_group": "group",
        "am_following_group": "group",
    }

    def refs(self, action: str | None, params: dict[str, Any]) -> dict[str, Any]:
        """Entity kind to the id or name the caller sent for it.

        A search or a list comes back empty - not by a rule about their names, but
        because they carry no entity parameter to read. ``datastore_search`` does carry
        ``resource_id``, so it is attributed despite being a search, and
        ``member_list`` names one group despite being a list.
        """
        refs: dict[str, Any] = {}

        for param, kind in self.NAMED_PARAMS:
            if params.get(param):
                refs.setdefault(kind, params[param])

        kind = self.id_means(action)
        if kind and params.get("id"):
            refs.setdefault(kind, params["id"])

        return refs

    def id_means(self, action: str | None) -> str | None:
        """Which entity this action's ``id`` parameter refers to, if any."""
        if not action:
            return None
        if action in self.ID_MEANS_EXACT:
            return self.ID_MEANS_EXACT[action]
        for prefix, kind in self.ID_MEANS_PREFIX:
            if action.startswith(prefix):
                return kind
        return None


class RequestEvent:
    """One Action API call or resource download, as the dict that gets recorded."""

    #: The one API endpoint. CKAN routes every ``/api/{ver}/action/<name>`` call
    #: through one view, so this covers every action that exists and every one added
    #: later - the event is named after the action in the URL. Downloads are
    #: recognised by :meth:`is_download_endpoint` instead of by name; everything
    #: else - pages, static files, health checks - is excluded by definition.
    API_ENDPOINT = "api.action"

    #: What every download event is called, whichever endpoint served it.
    DOWNLOAD_ACTION = "resource_download"

    #: nginx-ingress logs this as ``requestID`` and passes the same value upstream, so
    #: a CKAN event and its nginx line share an id. Also the natural dedup key.
    REQUEST_ID_HEADER = "X-Request-ID"

    #: Set by the ingress to its own ``$remote_addr``, so unlike ``X-Forwarded-For``
    #: it is one address rather than a list. nginx logs it as ``remoteIp``.
    REAL_IP_HEADER = "X-Real-IP"
    FORWARDED_FOR_HEADER = "X-Forwarded-For"

    def __init__(self, request: Any, response: Any, attribution: Any = None) -> None:
        self.request = request
        self.response = response
        self.attribution = attribution if attribution is not None else Attribution()

    @classmethod
    def from_request(cls, request: Any, response: Any) -> RequestEvent | None:
        """The event for this request, or None if it is not one we record."""
        if request.endpoint != cls.API_ENDPOINT and not cls.is_download_endpoint(
            request.endpoint
        ):
            return None
        if cls.is_ignored(request):
            return None
        return cls(request, response)

    @staticmethod
    def is_ignored(request: Any) -> bool:
        """Whether ``IGNORE_HEADER`` / ``IGNORE_VALUES`` say to skip this request."""
        if not IGNORE_HEADER or not IGNORE_VALUES:
            return False
        value = request.headers.get(IGNORE_HEADER)
        return value is not None and value.strip().lower() in IGNORE_VALUES

    @staticmethod
    def is_download_endpoint(endpoint: str | None) -> bool:
        """A resource blueprint's download view, whoever provides it.
        """
        if not endpoint or "." not in endpoint:
            return False
        blueprint, _, view = endpoint.rpartition(".")
        return blueprint.endswith("resource") and view.endswith("download")


    def as_dict(self) -> dict[str, Any]:
        """Eleven fields about the request, four about what it touched."""
        entities = self.entities()
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request_id": self.request_id,
            "service": SERVICE,
            "method": self.request.method,
            "endpoint": self.endpoint,
            "query_string": self.query_string,
            "action_type": self.action_type,
            "status_code": self.response.status_code,
            "user_agent": self.request.user_agent.string or None,
            "request_ip": self.request_ip,
            "user": self.user,
            **{kind: entities.get(kind) for kind in ENTITIES},
        }

    @property
    def action_type(self) -> str | None:
        """The action name, or ``resource_download`` for a download.

        For ``api.action`` the endpoint is the same constant for every action, so the
        name has to come from the URL. ``view_args`` is None on some error paths.
        """
        if self.is_download_endpoint(self.request.endpoint):
            return self.DOWNLOAD_ACTION

        return (self.request.view_args or {}).get("logic_function")

    @property
    def endpoint(self) -> str:
        """The path alone, with the query in its own field - so a report can
        group by endpoint without ``package_show?id=a`` and ``?id=b`` counting
        apart."""
        return self.request.path

    @property
    def query_string(self) -> str | None:
        """The raw query as the caller sent it - nginx's ``$args``. A POST body
        is not part of it: entity parameters sent as JSON appear only as the
        resolved entity fields."""
        return self.request.query_string.decode("utf-8", "replace") or None

    @property
    def request_id(self) -> str:
        return self.request.headers.get(self.REQUEST_ID_HEADER) or uuid.uuid4().hex

    @property
    def user(self) -> str | None:
        """The username. ``g.userobj`` is the CKAN user, absent when anonymous."""
        return getattr(getattr(g, "userobj", None), "name", None)

    @property
    def request_ip(self) -> str | None:
        """The caller's address, as the ingress reports it.

        ``X-Real-IP`` first: the ingress sets it to its own ``$remote_addr``, so it is
        one address. ``X-Forwarded-For`` is a list, and with
        ``compute-full-forwarded-for`` the ingress *appends* rather than replaces, so
        the last entry is the one it added itself. ``REMOTE_ADDR`` last - behind a
        proxy that is only the proxy, but right for a direct call.

        CKAN's own ``g.remote_addr`` is deliberately not used: it is the raw
        ``X-Forwarded-For`` header, which may be a comma-separated list.

        Not authoritative for security purposes. The ingress runs with
        ``use-forwarded-headers: true``, so a client can present its own
        ``X-Forwarded-For`` and be believed. Fine for analytics, not for access
        control.
        """
        real_ip = self.request.headers.get(self.REAL_IP_HEADER)
        if real_ip:
            return real_ip.strip() or None

        forwarded = self.request.headers.get(self.FORWARDED_FOR_HEADER)
        if forwarded:
            return forwarded.rsplit(",", 1)[-1].strip() or None

        return self.request.remote_addr

    def params(self) -> dict[str, Any]:
        """Entity references the caller sent, wherever they put them.

        A POST puts them in the JSON body, a GET in the query string, and the URL holds
        the action name. Only the parameters in ``Attribution`` are ever read out of
        this; the body itself is never recorded.
        """
        params: dict[str, Any] = {}
        if self.request.is_json:
            body = self.request.get_json(silent=True)
            if isinstance(body, dict):
                params.update(body)
        params.update(self.request.args.to_dict())
        params.update(self.request.view_args or {})
        return params

    def entities(self) -> dict[str, Any]:
        """Names for whatever this call touched. Never raises, may be empty."""
        try:
            refs = self.attribution.refs(self.action_type, self.params())
            return resolve_entities(refs) if refs else {}
        except Exception:
            log.exception("analytics: could not resolve entities for %s", self.action_type)
            return {}


def record_request(sender: Any, **kwargs: Any) -> None:
    """``request_finished`` listener.

    A module-level function on purpose - see the note in ``plugin.py``.

    ``response`` is read out of ``kwargs`` rather than taken as a parameter because
    CKAN's own documentation warns that signal arguments may change or disappear. The
    body is guarded because a listener that raises becomes the request's problem:
    Flask only swallows it when already handling an error.
    """
    response = kwargs.get("response")
    if response is None:
        return

    try:
        event = RequestEvent.from_request(current_request, response)
        if event is not None:
            emit_event(event.as_dict())
    except Exception:
        log.exception("analytics: could not record request")


def emit_event(event: dict[str, Any]) -> None:
    """Where an event goes: one bare JSON line on the event logger.

    The line is the complete record - whatever ships the logs (Loki, a GCP log
    sink, a file) is what carries events downstream. ``bigquery.sql`` documents
    the table they are meant to land in.
    """
    log.info(json.dumps(event, separators=(",", ":"), default=str))
