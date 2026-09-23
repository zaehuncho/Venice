// [SERVER-SHARD blocker #5] Contract tests for the OrionActivate broker.
//
//  1. DPAPI round-trip: the exact JSON the broker writes, CryptProtectData'd and
//     CryptUnprotectData'd through the real Win32 DPAPI, is parsed by a VERBATIM
//     copy of Lethe/bootstrap/shard_bootstrap.c's json_string_field /
//     is_hex_string / read_session_credential logic. This is the write->read
//     interop contract with the shipped bootstrap.
//  2. Activate request shape: field names/values mirror LicenseClient.cpp, and
//     the wire host/path match NetworkSecurity.cpp / postActivate.
//  3. Error->customer-text mapping (design D6).
//  4. Fail-closed guards in buildSessionJson.

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <wincrypt.h>

#include <cctype>
#include <cstdio>
#include <cstring>
#include <string>

#include <QtTest/QtTest>

#include "../broker/BrokerContract.h"

#pragma comment(lib, "crypt32.lib")

// ---------------------------------------------------------------------------
// VERBATIM from Lethe/bootstrap/shard_bootstrap.c (2026-09-19). Do not "clean
// up": the point is that the broker's output survives THIS exact parser.
// ---------------------------------------------------------------------------
static int json_string_field(const char *json, const char *field,
                             char *out, size_t out_size)
{
    char needle[96] = {0};
    if (!json || !field || !out || out_size < 2) return -1;
    if (snprintf(needle, sizeof(needle), "\"%s\"", field) < 0) return -1;
    const char *pos = strstr(json, needle);
    if (!pos) return -1;
    pos += strlen(needle);
    while (*pos == ' ' || *pos == '\t' || *pos == ':') pos++;
    if (*pos++ != '"') return -1;
    const char *end = strchr(pos, '"');
    if (!end || end == pos || (size_t)(end - pos) >= out_size) return -1;
    for (const char *p = pos; p < end; ++p) {
        if ((unsigned char)*p < 0x21 || (unsigned char)*p > 0x7e ||
            *p == '\\') return -1;
    }
    memcpy(out, pos, (size_t)(end - pos));
    out[end - pos] = '\0';
    return 0;
}

static int is_hex_string(const char *value, size_t exact_length)
{
    if (!value || strlen(value) != exact_length) return 0;
    for (size_t i = 0; i < exact_length; ++i)
        if (!isxdigit((unsigned char)value[i])) return 0;
    return 1;
}

using namespace orion::broker;

class ActivateBrokerContractTests : public QObject
{
    Q_OBJECT

private slots:
    // The full write->protect->unprotect->parse path the shipped bootstrap runs.
    void dpapiRoundTripMatchesBootstrapParser()
    {
        const std::string token = "yD3kQwErTyUiOp-_1234567890abcDEF"; // base64url shape
        const std::string tokenId = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"; // uuid
        const std::string machineId(64, 'a'); // 64 hex

        const std::string sessionJson = buildSessionJson(token, tokenId, machineId);
        QVERIFY2(!sessionJson.empty(), "buildSessionJson returned empty for valid inputs");

        // Broker side: CryptProtectData (CurrentUser, NULL entropy).
        DATA_BLOB in{};
        in.pbData = reinterpret_cast<BYTE*>(const_cast<char*>(sessionJson.data()));
        in.cbData = static_cast<DWORD>(sessionJson.size());
        DATA_BLOB prot{};
        QVERIFY2(CryptProtectData(&in, nullptr, nullptr, nullptr, nullptr,
                                  CRYPTPROTECT_UI_FORBIDDEN, &prot),
                 "CryptProtectData failed");

        // Bootstrap side: CryptUnprotectData with the exact arg pattern.
        DATA_BLOB out{};
        const bool unprot = CryptUnprotectData(&prot, nullptr, nullptr, nullptr,
                                               nullptr, CRYPTPROTECT_UI_FORBIDDEN, &out);
        QVERIFY2(unprot, "CryptUnprotectData failed");
        QVERIFY(out.pbData && out.cbData > 0 && out.cbData <= 4096);

        char json[4097] = {0};
        memcpy(json, out.pbData, out.cbData);

        char pToken[512] = {0}, pTokenId[128] = {0}, pMachine[128] = {0};
        QCOMPARE(json_string_field(json, "token", pToken, sizeof(pToken)), 0);
        QCOMPARE(json_string_field(json, "token_id", pTokenId, sizeof(pTokenId)), 0);
        QCOMPARE(json_string_field(json, "machine_id", pMachine, sizeof(pMachine)), 0);
        QCOMPARE(is_hex_string(pMachine, 64), 1);

        QCOMPARE(std::string(pToken), token);
        QCOMPARE(std::string(pTokenId), tokenId);
        QCOMPARE(std::string(pMachine), machineId);

        if (prot.pbData) LocalFree(prot.pbData);
        if (out.pbData) { SecureZeroMemory(out.pbData, out.cbData); LocalFree(out.pbData); }
    }

    // The response spells it "tid"; the bootstrap reads "token_id". The broker
    // must translate. Prove the field name in the written JSON is "token_id".
    void sessionJsonUsesTokenIdNotTid()
    {
        const std::string j = buildSessionJson("tok", "tid-value",
                                               std::string(64, 'b'));
        QVERIFY(j.find("\"token_id\"") != std::string::npos);
        QVERIFY(j.find("\"tid\"") == std::string::npos);
    }

    void sessionJsonFailsClosedOnUnsafeValues()
    {
        // backslash / quote in the token would break json_string_field / fetch_shard.
        QVERIFY(buildSessionJson("bad\\slash", "id", std::string(64, 'a')).empty());
        QVERIFY(buildSessionJson("bad\"quote", "id", std::string(64, 'a')).empty());
        // machine_id must be exactly 64 hex.
        QVERIFY(buildSessionJson("tok", "id", "tooshort").empty());
        QVERIFY(buildSessionJson("tok", "id", std::string(64, 'g')).empty());
        QVERIFY(buildSessionJson("", "id", std::string(64, 'a')).empty());
    }

    // Body mirrors licenseActivateRequestBody(): license_key + key = PAIR code,
    // machine_id, client_version, fingerprint_version:2, request_nonce/timestamp.
    void activateBodyMirrorsLicenseClient()
    {
        const std::string code = "PAIR-ABCDEFGHJKLMNPQRSTUVWXYZ2345678";
        const std::string mid(64, 'c');
        const std::string body = buildActivateBody(code, mid, "1.0.0",
                                                   "nonce-123", 1758300000LL);
        QVERIFY(body.find("\"license_key\":\"" + code + "\"") != std::string::npos);
        QVERIFY(body.find("\"key\":\"" + code + "\"") != std::string::npos);
        QVERIFY(body.find("\"machine_id\":\"" + mid + "\"") != std::string::npos);
        QVERIFY(body.find("\"client_version\":\"1.0.0\"") != std::string::npos);
        QVERIFY(body.find("\"fingerprint_version\":2") != std::string::npos);
        QVERIFY(body.find("\"request_nonce\":\"nonce-123\"") != std::string::npos);
        QVERIFY(body.find("\"request_timestamp\":1758300000") != std::string::npos);
        // timestamp must be a bare number, never quoted.
        QVERIFY(body.find("\"request_timestamp\":\"") == std::string::npos);
    }

    void activateHeadersCarryReplayFields()
    {
        const std::string h = buildActivateHeaders("1.0.0", "nonce-123",
                                                   1758300000LL, "req-abc");
        QVERIFY(h.find("Content-Type: application/json\r\n") != std::string::npos);
        QVERIFY(h.find("Accept: application/json\r\n") != std::string::npos);
        QVERIFY(h.find("User-Agent: OrionLauncher/1.0.0 (Windows NT 10.0; Win64; x64)\r\n") != std::string::npos);
        QVERIFY(h.find("X-Orion-Request-Nonce: nonce-123\r\n") != std::string::npos);
        QVERIFY(h.find("X-Orion-Request-Timestamp: 1758300000\r\n") != std::string::npos);
        QVERIFY(h.find("X-Orion-Request-Id: req-abc\r\n") != std::string::npos);
    }

    void wireHostAndPathMatchLiveContract()
    {
        QCOMPARE(std::string(kApiHostAscii), std::string("api.zaeorion.com"));
        // NOT /api/activate - Cloudflare 403s "activate" in the path; the Lambda
        // aliases redeem -> handle_activate().
        QCOMPARE(std::wstring(kActivatePathW), std::wstring(L"/api/license/redeem"));
        QCOMPARE(std::wstring(kApiHostW), std::wstring(L"api.zaeorion.com"));
    }

    void parseActivateResponseReadsTokenAndTid()
    {
        const std::string ok = "{\"ok\":true,\"token\":\"abc-_123\",\"tid\":"
                               "\"11111111-2222-3333-4444-555555555555\",\"expires\":123}";
        const ActivateVerdict v = parseActivateResponse(ok);
        QVERIFY(v.ok);
        QCOMPARE(v.token, std::string("abc-_123"));
        QCOMPARE(v.tokenId, std::string("11111111-2222-3333-4444-555555555555"));

        const std::string bad = "{\"ok\":false,\"error\":\"device_mismatch\"}";
        const ActivateVerdict d = parseActivateResponse(bad);
        QVERIFY(!d.ok);
        QCOMPARE(d.error, std::string("device_mismatch"));
    }

    // ── Codex finding #1: broker/session-only request ──────────────────────
    void bodyRequestsSessionOnlyByDefault()
    {
        const std::string code = "PAIR-ABCDEFGHJKLMNPQRSTUVWXYZ2345678";
        const std::string mid(64, 'c');
        const std::string body = buildActivateBody(code, mid, "1.0.0", "n", 1758300000LL);
        QVERIFY(body.find("\"session_only\":true") != std::string::npos);
        // Legacy/full mode omits the flag entirely (never sent as false).
        const std::string full =
            buildActivateBody(code, mid, "1.0.0", "n", 1758300000LL, /*sessionOnly=*/false);
        QVERIFY(full.find("session_only") == std::string::npos);
    }

    void sessionJsonNeverCarriesLicenseMaterial()
    {
        // The DPAPI record the broker writes carries ONLY token/token_id/machine_id.
        const std::string j = buildSessionJson("tok", "tid", std::string(64, 'a'));
        QVERIFY(j.find("canonical") == std::string::npos);
        QVERIFY(j.find("license_key") == std::string::npos);
    }

    // ── Codex finding #4: strict JSON + fail-closed verdict ─────────────────
    void strictParserAcceptsFlatAndIgnoresUnknownNested()
    {
        ActivateVerdict v;
        const std::string j = "{\"ok\":true,\"token\":\"T\",\"tid\":\"I\","
                              "\"expires\":123,\"profile\":{\"plan\":\"month\"}}";
        QVERIFY(parseActivateResponseStrict(j, v));
        QVERIFY(v.ok);
        QCOMPARE(v.token, std::string("T"));
        QCOMPARE(v.tokenId, std::string("I"));
    }

    void strictParserRejectsNestedShadowDuplicateAndMalformed()
    {
        ActivateVerdict v;
        // A nested object shadowing "token" is not read; a top-level "token" that
        // is itself an object is rejected outright (not a string).
        QVERIFY(!parseActivateResponseStrict("{\"token\":{\"token\":\"evil\"}}", v));
        // Duplicate top-level key.
        QVERIFY(!parseActivateResponseStrict("{\"ok\":false,\"ok\":true}", v));
        // Not an object / malformed / trailing garbage / unterminated.
        QVERIFY(!parseActivateResponseStrict("", v));
        QVERIFY(!parseActivateResponseStrict("not json", v));
        QVERIFY(!parseActivateResponseStrict("[1,2,3]", v));
        QVERIFY(!parseActivateResponseStrict("{\"ok\":true} trailing", v));
        QVERIFY(!parseActivateResponseStrict("{\"ok\":true", v));
        // Expected field present but wrong scalar type.
        QVERIFY(!parseActivateResponseStrict("{\"ok\":\"true\"}", v));
    }

    void decideRequires200AndTokenPair()
    {
        const ActivateVerdict good =
            decideActivateVerdict(200, "{\"ok\":true,\"token\":\"T\",\"tid\":\"I\"}");
        QVERIFY(good.ok);
        QCOMPARE(good.token, std::string("T"));
        QCOMPARE(good.tokenId, std::string("I"));

        // ok:true from a NON-200 must never be accepted.
        const ActivateVerdict spoof =
            decideActivateVerdict(403, "{\"ok\":true,\"token\":\"T\",\"tid\":\"I\"}");
        QVERIFY(!spoof.ok);

        // A declined non-200 keeps its error code.
        const ActivateVerdict declined =
            decideActivateVerdict(403, "{\"ok\":false,\"error\":\"device_mismatch\"}");
        QVERIFY(!declined.ok);
        QCOMPARE(declined.error, std::string("device_mismatch"));

        // 200 + ok but no token/tid -> not ok.
        const ActivateVerdict noTok = decideActivateVerdict(200, "{\"ok\":true}");
        QVERIFY(!noTok.ok);

        // Malformed body -> malformed_response, never ok.
        const ActivateVerdict bad = decideActivateVerdict(200, "{ not json");
        QVERIFY(!bad.ok);
        QCOMPARE(bad.error, std::string("malformed_response"));
    }

    void errorMappingFollowsD6()
    {
        QCOMPARE(customerMessageForError("", true),
                 std::string("Can't reach Venice servers. Check your connection and Retry."));
        QCOMPARE(customerMessageForError("machine_mismatch", false),
                 std::string("This subscription is linked to another PC. Use /hwid_reset, then Retry."));
        QCOMPARE(customerMessageForError("device_mismatch", false),
                 std::string("This subscription is linked to another PC. Use /hwid_reset, then Retry."));
        QCOMPARE(customerMessageForError("subscription_required", false),
                 std::string("Your Venice access is not active. Subscribe at zaeorion.com or open a ticket."));
        QCOMPARE(customerMessageForError("build_revoked", false),
                 std::string("This Venice build is no longer available. Update Venice."));
        QCOMPARE(customerMessageForError("version_blocked", false),
                 std::string("This Venice build is no longer available. Update Venice."));
        QCOMPARE(customerMessageForError("rate_limited", false),
                 std::string("Too many launch attempts. Wait one minute, then Retry."));
        QCOMPARE(customerMessageForError("some_unknown_code", false),
                 std::string("Venice could not validate this installation. Update Venice or open a ticket."));
    }

    // ── Codex finding #4 (round 2): strict RFC-8259 number grammar ──────────
    void strictParserRejectsMalformedNumbers()
    {
        ActivateVerdict v;
        // Each must be rejected: leading zeros, lone/doubled minus, empty/repeated
        // decimals, empty exponents, plus sign, hex, no-int fraction, trailing dot.
        const char* bad[] = {
            "{\"n\":01}",    "{\"n\":00}",   "{\"n\":-01}",  "{\"n\":-}",
            "{\"n\":1.}",    "{\"n\":1..2}", "{\"n\":1.2.3}", "{\"n\":.5}",
            "{\"n\":1e}",    "{\"n\":1e+}",  "{\"n\":1e-}",  "{\"n\":--5}",
            "{\"n\":+5}",    "{\"n\":0x1}",  "{\"n\":1e1.5}", "{\"n\":1 2}",
        };
        for (const char* j : bad) {
            QVERIFY2(!parseActivateResponseStrict(j, v), j);
        }
    }

    void strictParserAcceptsWellFormedNumbers()
    {
        ActivateVerdict v;
        // Well-formed numbers as unknown nested fields must NOT cause a reject
        // (forward-compat): the parser validates them and moves on.
        const char* good[] = {
            "{\"n\":0}",   "{\"n\":-0}",       "{\"n\":123}",  "{\"n\":1.5}",
            "{\"n\":1E-3}", "{\"n\":-0.5e+10}", "{\"n\":0.0}",  "{\"n\":9999999999}",
        };
        for (const char* j : good) {
            QVERIFY2(parseActivateResponseStrict(j, v), j);
        }
        // A well-formed unknown number field beside the real fields is ignored.
        QVERIFY(parseActivateResponseStrict(
            "{\"ok\":true,\"token\":\"T\",\"tid\":\"I\",\"expires\":-0.5e+10}", v));
        QVERIFY(v.ok);
        QCOMPARE(v.token, std::string("T"));
        QCOMPARE(v.tokenId, std::string("I"));
    }

    // ── Codex finding #2 (round 2): a failed read is a TRANSPORT failure and
    //    the partial/accumulated body is NEVER parsed (no session, no launch). ──
    void readFailureIsTransportFailureNeverParsesBody()
    {
        // A prefix that WOULD decide ok:true if a partial read were trusted.
        const std::string validLooking = "{\"ok\":true,\"token\":\"T\",\"tid\":\"I\"}";

        ActivateVerdict failed;
        // readOk=false -> transport failure; verdict left unusable; body ignored.
        QVERIFY(!finalizeActivateResponse(false, 200, validLooking, failed));
        QVERIFY(!failed.ok);
        QVERIFY(failed.token.empty());
        QVERIFY(failed.tokenId.empty());

        // A CLEAN read of the same body succeeds end to end.
        ActivateVerdict good;
        QVERIFY(finalizeActivateResponse(true, 200, validLooking, good));
        QVERIFY(good.ok);
        QCOMPARE(good.token, std::string("T"));
        QCOMPARE(good.tokenId, std::string("I"));

        // Clean read + non-200 -> transport ok, verdict declined (never a session).
        ActivateVerdict declined;
        QVERIFY(finalizeActivateResponse(
            true, 403, "{\"ok\":false,\"error\":\"device_mismatch\"}", declined));
        QVERIFY(!declined.ok);
        QCOMPARE(declined.error, std::string("device_mismatch"));

        // Clean read but a malformed body -> transport ok, malformed verdict.
        ActivateVerdict malformed;
        QVERIFY(finalizeActivateResponse(true, 200, "{ not json", malformed));
        QVERIFY(!malformed.ok);
        QCOMPARE(malformed.error, std::string("malformed_response"));
    }
};

QTEST_GUILESS_MAIN(ActivateBrokerContractTests)
#include "ActivateBrokerContractTests.moc"
