import { describe, expect, it, vi } from "vitest";
import { ApiError, createControlApiClient, type FetchLike } from "../../src/api";
import { parseCostBreakdown, parseOperationList } from "../../src/contracts/diagnosticsApi";
import { ContractError } from "../../src/contracts/validate";

const BASE = "http://127.0.0.1:8000/api/v1";
const SESSION_ID = "11111111-1111-4111-8111-111111111111";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const OPERATION = {
  operation_id: "op-1",
  turn_id: null,
  component: "stt",
  provider: "deepgram",
  label: "Deepgram",
  attempt_number: 1,
  status: "succeeded",
  usage: {
    reporting_status: "provider_reported",
    items: [
      { unit: "transcribed_audio_seconds", quantity: "12.5", source: "provider_reported", estimated: false },
      { unit: "requests", quantity: "not-a-number", source: "measured", estimated: false },
    ],
  },
  estimated_cost: "0.001",
};

describe("parseOperationList", () => {
  it("keeps the fields the evidence panel needs and skips unparsable usage quantities", () => {
    const [operation] = parseOperationList({ items: [OPERATION], next_cursor: null, request_id: "r" });
    expect(operation).toEqual({
      operationId: "op-1",
      component: "stt",
      provider: "deepgram",
      status: "succeeded",
      usage: [{ unit: "transcribed_audio_seconds", quantity: 12.5 }],
    });
  });

  it("rejects a malformed list without echoing values", () => {
    expect(() => parseOperationList({ items: [{ component: 4 }] })).toThrow(ContractError);
  });
});

describe("parseCostBreakdown", () => {
  it("parses totals and components", () => {
    const result = parseCostBreakdown({
      data: {
        calculation_status: "final",
        total_usd: "0.0100",
        components: [{ component: "stt", label: "Deepgram", amount_usd: "0.0040", retry_or_failure_related: false }],
      },
      request_id: "r",
    });
    expect(result.totalUsd).toBe("0.0100");
    expect(result.components[0]?.amountUsd).toBe("0.0040");
  });
});

describe("control API diagnostics calls", () => {
  it("requests STT operations with a component filter", async () => {
    const fetchMock = vi.fn<FetchLike>().mockResolvedValue(json(200, { items: [OPERATION], next_cursor: null, request_id: "r" }));
    const client = createControlApiClient({ baseUrl: BASE, fetchImpl: fetchMock });

    const operations = await client.listOperations(SESSION_ID, { component: "stt", limit: 20 });

    expect(operations).toHaveLength(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(`${BASE}/sessions/${SESSION_ID}/operations?limit=20&component=stt`);
  });

  it("uses the default limit and no filter when none is given", async () => {
    const fetchMock = vi.fn<FetchLike>().mockResolvedValue(json(200, { items: [], next_cursor: null, request_id: "r" }));
    await createControlApiClient({ baseUrl: BASE, fetchImpl: fetchMock }).listOperations(SESSION_ID);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(`${BASE}/sessions/${SESSION_ID}/operations?limit=100`);
  });

  it("surfaces a 503 not-ready costs response as a non-retryable ApiError", async () => {
    const fetchMock = vi.fn<FetchLike>().mockResolvedValue(
      json(503, { error: { code: "DEPENDENCY_UNAVAILABLE", message: "Cost breakdowns are not available in this build.", retryable: false }, request_id: "r" }),
    );
    const client = createControlApiClient({ baseUrl: BASE, fetchImpl: fetchMock });

    const failure = await client.getCosts(SESSION_ID).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).status).toBe(503);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(`${BASE}/sessions/${SESSION_ID}/costs`);
  });
});
