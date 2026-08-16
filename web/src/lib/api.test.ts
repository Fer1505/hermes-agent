import { afterEach, describe, expect, it, vi } from "vitest";

import { api, buildWsAuthParam, buildWsUrl } from "./api";

const SESSION_HEADER = "X-Hermes-Session-Token";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function jsonFetchMock(body: unknown = { ok: true }) {
  return vi.fn<typeof fetch>(
    async () =>
      new Response(JSON.stringify(body), {
        headers: { "Content-Type": "application/json" },
        status: 200,
      }),
  );
}

describe("api.getModelOptions", () => {
  it("requests a live model refresh when asked", async () => {
    vi.stubGlobal("window", {});

    const fetchMock = jsonFetchMock({ providers: [] });
    vi.stubGlobal("fetch", fetchMock);

    await api.getModelOptions({ refresh: true });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/model/options?refresh=1&include_unconfigured=1",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("keeps explicit profile scoping when refreshing", async () => {
    vi.stubGlobal("window", {});

    const fetchMock = jsonFetchMock({ providers: [] });
    vi.stubGlobal("fetch", fetchMock);

    await api.getModelOptions({ profile: "default", refresh: true });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/model/options?profile=default&refresh=1&include_unconfigured=1",
      expect.objectContaining({ credentials: "include" }),
    );
  });
});

describe("dashboard WebSocket auth boundary", () => {
  it("builds a tokenless URL only when dashboard auth is disabled", async () => {
    vi.stubGlobal("window", {
      __HERMES_AUTH_REQUIRED__: false,
      location: { host: "127.0.0.1:9119", protocol: "http:" },
    });

    await expect(buildWsAuthParam()).resolves.toBeUndefined();
    await expect(buildWsUrl("/api/ws")).resolves.toBe(
      "ws://127.0.0.1:9119/api/ws",
    );
  });

  it("requires a non-empty single-use ticket when auth is enabled", async () => {
    vi.stubGlobal("window", {
      __HERMES_AUTH_REQUIRED__: true,
      location: { host: "dashboard.example", protocol: "https:" },
    });
    const fetchMock = jsonFetchMock({ ticket: "", ttl_seconds: 30 });
    vi.stubGlobal("fetch", fetchMock);

    await expect(buildWsAuthParam()).rejects.toThrow(
      "Dashboard WebSocket ticket was empty",
    );
  });

  it("uses a ticket, never a token, when auth is enabled", async () => {
    vi.stubGlobal("window", {
      __HERMES_AUTH_REQUIRED__: true,
      __HERMES_SESSION_TOKEN__: "retired-token",
      location: { host: "dashboard.example", protocol: "https:" },
    });
    const fetchMock = jsonFetchMock({ ticket: "ticket-1", ttl_seconds: 30 });
    vi.stubGlobal("fetch", fetchMock);

    await expect(buildWsUrl("/api/ws")).resolves.toBe(
      "wss://dashboard.example/api/ws?ticket=ticket-1",
    );
  });
});

describe("api OAuth helpers", () => {
  it("starts OAuth login in gated mode without requiring an injected session token", async () => {
    vi.stubGlobal("window", { __HERMES_AUTH_REQUIRED__: true });
    const fetchMock = jsonFetchMock({
      flow: "device_code",
      session_id: "oauth-session",
    });
    vi.stubGlobal("fetch", fetchMock);

    await api.startOAuthLogin("openai-codex");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/providers/oauth/openai-codex/start",
      expect.objectContaining({
        body: "{}",
        credentials: "include",
        method: "POST",
      }),
    );
    const headers = fetchMock.mock.calls[0][1]?.headers as Headers;
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.has(SESSION_HEADER)).toBe(false);
  });

  it("still sends the injected session token for OAuth login in loopback mode", async () => {
    vi.stubGlobal("window", { __HERMES_SESSION_TOKEN__: "loopback-token" });
    const fetchMock = jsonFetchMock({
      flow: "device_code",
      session_id: "oauth-session",
    });
    vi.stubGlobal("fetch", fetchMock);

    await api.startOAuthLogin("openai-codex");

    const headers = fetchMock.mock.calls[0][1]?.headers as Headers;
    expect(headers.get(SESSION_HEADER)).toBe("loopback-token");
  });

  it("runs provider auth mutations in gated mode via cookie auth", async () => {
    vi.stubGlobal("window", { __HERMES_AUTH_REQUIRED__: true });
    const fetchMock = jsonFetchMock({ ok: true });
    vi.stubGlobal("fetch", fetchMock);

    await api.disconnectOAuthProvider("anthropic");
    await api.submitOAuthCode("anthropic", "oauth-session", "code-123");
    await api.cancelOAuthSession("oauth-session");
    await api.revealEnvVar("OPENAI_API_KEY");

    for (const call of fetchMock.mock.calls) {
      const init = call[1] as RequestInit;
      expect(init.credentials).toBe("include");
      expect((init.headers as Headers).has(SESSION_HEADER)).toBe(false);
    }
  });
});
