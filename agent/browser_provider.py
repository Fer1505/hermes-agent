"""
Browser Provider ABC
====================

Defines the pluggable-backend interface for cloud browser providers
(Browserbase, Browser Use, Firecrawl, …). Providers register instances via
:meth:`PluginContext.register_browser_provider`; the active one (selected via
``browser.cloud_provider`` in ``config.yaml``) services every cloud-mode
``browser_*`` tool call.

Providers live in ``<repo>/plugins/browser/<name>/`` (built-in, auto-loaded as
``kind: backend``) or ``~/.hermes/plugins/browser/<name>/`` (user, opt-in via
``plugins.enabled``).

This ABC mirrors :class:`agent.web_search_provider.WebSearchProvider` (PR
#25182) — same shape, same registration flow, same picker integration. The
legacy in-tree ``tools.browser_providers.base.CloudBrowserProvider`` ABC was
deleted in PR #25214 (this work) along with the per-vendor inline modules in
``tools/browser_providers/``. The legacy lifecycle fields documented below
remain intact; the explicit egress contract adds network-boundary metadata
without changing their meaning.

Session metadata contract (preserved from the legacy ``CloudBrowserProvider``)::

    {
        "session_name": str,        # unique name for agent-browser --session
        "bb_session_id": str,       # provider session ID (for close/cleanup)
        "cdp_url": str,             # CDP websocket URL
        "features": dict,           # feature flags that were enabled
        "external_call_id": str,    # optional, managed-gateway billing key
    }

``bb_session_id`` is a legacy key name kept verbatim for backward compat with
:mod:`tools.browser_tool` — it holds the provider's session ID regardless of
which provider is in use.
"""

from __future__ import annotations

import abc
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict


class BrowserExecutionLocation(str, Enum):
    PROVIDER_REMOTE = "provider-remote"


class BrowserNetworkBoundary(str, Enum):
    PROVIDER_MANAGED_UNVERIFIED = "provider-managed-unverified"


class BrowserControlTransport(str, Enum):
    CDP = "cdp"


@dataclass(frozen=True)
class BrowserEgressCapability:
    """Truthful network-boundary contract for a browser provider.

    ``execution_location`` describes where page JavaScript and navigation run,
    while ``network_boundary`` describes who enforces the page's outbound
    network policy.  The control transport is recorded separately because a
    CDP connection from Hermes to a remote browser is not itself an egress
    isolation guarantee.
    """

    execution_location: BrowserExecutionLocation
    network_boundary: BrowserNetworkBoundary
    control_transport: BrowserControlTransport
    requires_cdp_url: bool
    allows_cross_authority_cdp_discovery: bool

    def __post_init__(self) -> None:
        for field_name, enum_type in (
            ("execution_location", BrowserExecutionLocation),
            ("network_boundary", BrowserNetworkBoundary),
            ("control_transport", BrowserControlTransport),
        ):
            if not isinstance(getattr(self, field_name), enum_type):
                raise TypeError(f"{field_name} must be a {enum_type.__name__}")
        if not isinstance(self.requires_cdp_url, bool):
            raise TypeError("requires_cdp_url must be a bool")
        if not isinstance(self.allows_cross_authority_cdp_discovery, bool):
            raise TypeError("allows_cross_authority_cdp_discovery must be a bool")

    def as_session_metadata(self) -> Dict[str, object]:
        """Return a JSON-serializable copy for session observability."""
        metadata = asdict(self)
        return {
            key: value.value if isinstance(value, Enum) else value
            for key, value in metadata.items()
        }


REMOTE_PROVIDER_EGRESS = BrowserEgressCapability(
    execution_location=BrowserExecutionLocation.PROVIDER_REMOTE,
    network_boundary=BrowserNetworkBoundary.PROVIDER_MANAGED_UNVERIFIED,
    control_transport=BrowserControlTransport.CDP,
    requires_cdp_url=True,
    allows_cross_authority_cdp_discovery=False,
)

# Browser Use can return an HTTPS discovery URL whose JSON names a websocket
# relay on a different authority. That provider behavior is explicit here so
# the dispatcher can reject cross-authority discovery for every other source.
REMOTE_PROVIDER_EGRESS_WITH_CROSS_AUTHORITY_DISCOVERY = BrowserEgressCapability(
    execution_location=BrowserExecutionLocation.PROVIDER_REMOTE,
    network_boundary=BrowserNetworkBoundary.PROVIDER_MANAGED_UNVERIFIED,
    control_transport=BrowserControlTransport.CDP,
    requires_cdp_url=True,
    allows_cross_authority_cdp_discovery=True,
)


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


class BrowserProvider(abc.ABC):
    """Abstract base class for a cloud browser backend.

    Subclasses must implement :meth:`name`, :attr:`egress_capability`,
    :meth:`is_available`, and the three lifecycle methods:
    :meth:`create_session`, :meth:`close_session`, :meth:`emergency_cleanup`.

    The lifecycle shape preserves the legacy ``CloudBrowserProvider`` fields
    so the dispatcher in :mod:`tools.browser_tool` remains a pure registry
    lookup — no per-provider conditionals or shape translation.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Stable short identifier used in the ``browser.cloud_provider``
        config key.

        Lowercase, hyphens permitted to preserve existing user-visible names.
        Examples: ``browserbase``, ``browser-use``, ``firecrawl``.
        """

    @property
    def display_name(self) -> str:
        """Human-readable label shown in ``hermes tools``. Defaults to ``name``."""
        return self.name

    @property
    @abc.abstractmethod
    def egress_capability(self) -> BrowserEgressCapability:
        """Declare where page traffic originates and who controls it.

        A provider must declare this explicitly. A backend offering a
        different boundary must return a different truthful contract instead
        of relying on a backend name or feature flag.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Return True when this provider can service calls.

        Typically a cheap check (env var present, managed-gateway token
        readable, optional Python dep importable). Must NOT make network
        calls — this runs at tool-registration time and on every
        ``hermes tools`` paint.

        Mirrors the legacy ``CloudBrowserProvider.is_configured()`` method;
        renamed for parity with :class:`agent.web_search_provider.WebSearchProvider`.
        """

    @abc.abstractmethod
    def create_session(self, task_id: str) -> Dict[str, object]:
        """Create a cloud browser session and return session metadata.

        Must return a dict with at least::

            {
                "session_name": str,    # unique name for agent-browser --session
                "bb_session_id": str,   # provider session ID (for close/cleanup)
                "cdp_url": str,         # CDP websocket URL
                "features": dict,       # feature flags that were enabled
            }

        The dispatcher validates this metadata against
        :attr:`egress_capability`. A remote provider whose contract requires
        CDP must return a concrete ``ws://`` or ``wss://`` endpoint; otherwise
        the session fails instead of silently launching a local browser.
        Provider-returned discovery and websocket endpoints receive a public-
        network preflight, but connections are not IP-pinned: the HTTP client,
        supervisor, and agent-browser can re-resolve DNS. Cross-authority
        discovery is rejected unless the provider's capability contract
        explicitly declares that behavior.

        ``bb_session_id`` is a legacy key name kept for backward compat with
        the rest of :mod:`tools.browser_tool` — it holds the provider's
        session ID regardless of which provider is in use.

        May raise ``ValueError`` (missing credentials) or ``RuntimeError``
        (network / API failure); the dispatcher surfaces these to the user.
        """

    @abc.abstractmethod
    def close_session(self, session_id: str) -> bool:
        """Release / terminate a cloud session by its provider session ID.

        Returns True on success, False on failure. Should not raise — log and
        return False on any exception so the dispatcher's cleanup loop keeps
        moving across sessions.
        """

    @abc.abstractmethod
    def emergency_cleanup(self, session_id: str) -> None:
        """Best-effort session teardown during process exit.

        Called from atexit / signal handlers. Must tolerate missing
        credentials, network errors, etc. — log and move on. Must not raise.
        """

    def get_setup_schema(self) -> Dict[str, Any]:
        """Return provider metadata for the ``hermes tools`` picker.

        Used by :mod:`hermes_cli.tools_config` to inject this provider as a
        row in the Browser Automation picker. Shape mirrors the existing
        hardcoded entries in ``TOOL_CATEGORIES["browser"]``::

            {
                "name": "Browserbase",
                "badge": "paid",
                "tag": "Cloud browser with stealth and proxies",
                "env_vars": [
                    {"key": "BROWSERBASE_API_KEY",
                     "prompt": "Browserbase API key",
                     "url": "https://browserbase.com"},
                ],
                "post_setup": "agent_browser",
            }

        Default: minimal entry derived from :attr:`display_name`. Override to
        expose API key prompts, badges, managed-Nous gating, and the
        ``post_setup`` install hook.
        """
        return {
            "name": self.display_name,
            "badge": "",
            "tag": "",
            "env_vars": [],
        }

    # ------------------------------------------------------------------
    # Backward-compat shims for the legacy CloudBrowserProvider API
    # ------------------------------------------------------------------
    #
    # The pre-PR-#25214 ABC exposed ``is_configured()`` and ``provider_name()``;
    # ``tools.browser_tool`` has ~6 callers that still use those names. Rather
    # than churn every callsite (and break out-of-tree downstream code that
    # subclassed CloudBrowserProvider), we expose the old names as thin
    # delegations to the new API. Subclasses MUST implement :meth:`is_available`
    # and :attr:`name`; they may override ``is_configured`` / ``provider_name``
    # for compatibility with the legacy ABC but it is not required.

    def is_configured(self) -> bool:
        """Backward-compat alias for :meth:`is_available`."""
        return self.is_available()

    def provider_name(self) -> str:
        """Backward-compat alias returning :attr:`display_name`."""
        return self.display_name
