import { afterEach, describe, expect, it, vi } from "vitest";
import { revealResourceCredential } from "./resourceCredentialApi";

describe("revealResourceCredential", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("uses an authenticated no-store POST instead of the generic resource reader", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => "token" });
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ username: "admin", password: "secret" }), { status: 200 },
    ));
    vi.stubGlobal("fetch", fetchMock);

    await expect(revealResourceCredential("resource/id")).resolves.toEqual({ username: "admin", password: "secret" });
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/resources/resource%2Fid/credential", expect.objectContaining({
      method: "POST", cache: "no-store", credentials: "same-origin",
      headers: { Authorization: "Bearer token" },
    }));
  });
});
