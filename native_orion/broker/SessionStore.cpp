#include "SessionStore.h"

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef UNICODE
#define UNICODE
#endif

#include <windows.h>
#include <wincrypt.h>
#include <aclapi.h>

#include <vector>

#pragma comment(lib, "crypt32.lib")
#pragma comment(lib, "advapi32.lib")

namespace orion {
namespace broker {

namespace {

const wchar_t* kSessionRelPathW = L"NexusVision\\Orion Native\\.vault\\shard_session.dat";

// RAII: build a current-user-only protected security descriptor. LocalFree the
// returned PACL via the Holder.
struct OwnerOnlySd {
    SECURITY_DESCRIPTOR sd{};
    PACL acl = nullptr;
    std::vector<unsigned char> tokenBuf;
    bool ok = false;

    OwnerOnlySd()
    {
#ifndef ORION_PRODUCTION_BUILD
        // Test-only fault injection (compiled OUT of production builds): force the
        // owner-only security-descriptor build to fail so BrokerSessionStoreTests can
        // prove ensureVaultDir()/writeProtectedFileAtomic() FAIL CLOSED (no vault
        // dir, no session write) when the SD/DACL cannot be established.
        {
            wchar_t v[8] = {};
            if (GetEnvironmentVariableW(L"ORION_BROKER_TEST_FORCE_SD_FAIL", v, 8) > 0) {
                return; // ok stays false
            }
        }
#endif
        HANDLE token = nullptr;
        if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
            return;
        }
        DWORD len = 0;
        GetTokenInformation(token, TokenUser, nullptr, 0, &len);
        if (len == 0) { CloseHandle(token); return; }
        tokenBuf.resize(len);
        if (!GetTokenInformation(token, TokenUser, tokenBuf.data(), len, &len)) {
            CloseHandle(token); return;
        }
        CloseHandle(token);
        PSID sid = reinterpret_cast<TOKEN_USER*>(tokenBuf.data())->User.Sid;

        EXPLICIT_ACCESSW ea = {};
        ea.grfAccessPermissions = GENERIC_ALL;
        ea.grfAccessMode = SET_ACCESS;
        ea.grfInheritance = NO_INHERITANCE;
        ea.Trustee.TrusteeForm = TRUSTEE_IS_SID;
        ea.Trustee.TrusteeType = TRUSTEE_IS_USER;
        ea.Trustee.ptstrName = reinterpret_cast<LPWSTR>(sid);
        if (SetEntriesInAclW(1, &ea, nullptr, &acl) != ERROR_SUCCESS) { acl = nullptr; return; }
        if (!InitializeSecurityDescriptor(&sd, SECURITY_DESCRIPTOR_REVISION)) return;
        // SE_DACL_PROTECTED stops inheritable parent ACEs from widening access.
        if (!SetSecurityDescriptorControl(&sd, SE_DACL_PROTECTED, SE_DACL_PROTECTED)) return;
        if (!SetSecurityDescriptorDacl(&sd, TRUE, acl, FALSE)) return;
        ok = true;
    }
    ~OwnerOnlySd() { if (acl) LocalFree(acl); }
    OwnerOnlySd(const OwnerOnlySd&) = delete;
    OwnerOnlySd& operator=(const OwnerOnlySd&) = delete;

    PSID currentUserSid() { return reinterpret_cast<TOKEN_USER*>(tokenBuf.data())->User.Sid; }
};

std::wstring localAppDataW()
{
    wchar_t buf[MAX_PATH] = {};
    DWORD n = GetEnvironmentVariableW(L"LOCALAPPDATA", buf, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return {};
    return std::wstring(buf, n);
}

} // namespace

std::wstring sessionFilePathW()
{
    const std::wstring lad = localAppDataW();
    if (lad.empty()) return {};
    return lad + L"\\" + kSessionRelPathW;
}

bool ensureVaultDir()
{
    const std::wstring lad = localAppDataW();
    if (lad.empty()) return false;

    // FAIL CLOSED (finding #3, round 2): if we cannot build the owner-only
    // protected security descriptor, we cannot guarantee the vault is private, so
    // refuse — never fall back to the inherited/default DACL, which could leave the
    // session blob readable by other principals.
    OwnerOnlySd sd;
    if (!sd.ok) {
        return false;
    }
    SECURITY_ATTRIBUTES sa = {};
    sa.nLength = sizeof(sa);
    sa.lpSecurityDescriptor = &sd.sd;
    sa.bInheritHandle = FALSE;

    const wchar_t* parts[] = {L"NexusVision", L"Orion Native", L".vault"};
    std::wstring cur = lad;
    for (int i = 0; i < 3; ++i) {
        cur += L"\\";
        cur += parts[i];
        // Only .vault (the leaf holding the session) gets the owner-only DACL; its
        // parents may legitimately be shared app dirs.
        const BOOL created = CreateDirectoryW(cur.c_str(), (i == 2) ? &sa : nullptr);
        if (!created && GetLastError() != ERROR_ALREADY_EXISTS) {
            return false;
        }
        if (i == 2) {
            // Assert (or re-assert, if .vault already existed) the owner-only
            // protected DACL. A failure here means the directory's ACL is unknown
            // or wider than owner-only, so FAIL CLOSED rather than write a session
            // into a possibly world-readable dir.
            const DWORD rc = SetNamedSecurityInfoW(
                &cur[0], SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                nullptr, nullptr, sd.acl, nullptr);
            if (rc != ERROR_SUCCESS) {
                return false;
            }
        }
    }
    return true;
}

bool writeProtectedFileAtomic(const std::wstring& finalPathW,
                              const std::string& plaintext,
                              bool applyOwnerDacl,
                              std::string* err)
{
    if (err) err->clear();
    if (finalPathW.empty() || plaintext.empty()) {
        if (err) *err = "bad_args";
        return false;
    }

    OwnerOnlySd sd;
    if (applyOwnerDacl && !sd.ok) {
        if (err) *err = "sd_build_failed";
        return false;
    }
    SECURITY_ATTRIBUTES sa = {};
    sa.nLength = sizeof(sa);
    sa.lpSecurityDescriptor = (applyOwnerDacl && sd.ok) ? &sd.sd : nullptr;
    sa.bInheritHandle = FALSE;

    const std::wstring tmpW = finalPathW + L".tmp";
    bool ok = false;
    DATA_BLOB in = {}, out = {};
    HANDLE hFile = INVALID_HANDLE_VALUE;

    in.pbData = reinterpret_cast<BYTE*>(const_cast<char*>(plaintext.data()));
    in.cbData = static_cast<DWORD>(plaintext.size());
    // CurrentUser scope (no CRYPTPROTECT_LOCAL_MACHINE), NULL entropy — exactly
    // what shard_bootstrap.c:CryptUnprotectData(&in,NULL,NULL,NULL,NULL,...) reads.
    if (!CryptProtectData(&in, nullptr, nullptr, nullptr, nullptr,
                          CRYPTPROTECT_UI_FORBIDDEN, &out)) {
        if (err) *err = "dpapi_protect_failed";
        goto cleanup;
    }

    hFile = CreateFileW(tmpW.c_str(), GENERIC_WRITE, 0,
                        (applyOwnerDacl && sd.ok) ? &sa : nullptr,
                        CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (hFile == INVALID_HANDLE_VALUE) {
        if (err) *err = "tmp_create_failed";
        goto cleanup;
    }
    {
        DWORD written = 0;
        if (!WriteFile(hFile, out.pbData, out.cbData, &written, nullptr) ||
            written != out.cbData) {
            if (err) *err = "write_failed";
            goto cleanup;
        }
    }
    // Honor the flush result — a failed flush means the bytes may not be durable,
    // so do not promote the temp to the final session.
    if (!FlushFileBuffers(hFile)) {
        if (err) *err = "flush_failed";
        goto cleanup;
    }
    CloseHandle(hFile);
    hFile = INVALID_HANDLE_VALUE;

    if (!MoveFileExW(tmpW.c_str(), finalPathW.c_str(),
                     MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
        if (err) *err = "atomic_replace_failed";
        goto cleanup;
    }
    ok = true;

cleanup:
    if (hFile != INVALID_HANDLE_VALUE) CloseHandle(hFile);
    if (out.pbData) {
        SecureZeroMemory(out.pbData, out.cbData);
        LocalFree(out.pbData);
    }
    if (!ok) {
        DeleteFileW(tmpW.c_str());  // never leave a partial temp behind
    }
    return ok;
}

bool writeSession(const std::string& plaintext, std::string* err)
{
    if (!ensureVaultDir()) {
        if (err) *err = "vault_dir_failed";
        return false;
    }
    const std::wstring path = sessionFilePathW();
    if (path.empty()) {
        if (err) *err = "no_localappdata";
        return false;
    }
    return writeProtectedFileAtomic(path, plaintext, /*applyOwnerDacl=*/true, err);
}

bool removeSessionFile(const std::wstring& finalPathW)
{
    if (finalPathW.empty()) return false;
    const std::wstring tmpW = finalPathW + L".tmp";
    DeleteFileW(finalPathW.c_str());
    DeleteFileW(tmpW.c_str());
    const DWORD a1 = GetFileAttributesW(finalPathW.c_str());
    const DWORD a2 = GetFileAttributesW(tmpW.c_str());
    return a1 == INVALID_FILE_ATTRIBUTES && a2 == INVALID_FILE_ATTRIBUTES;
}

bool removeSession()
{
    const std::wstring path = sessionFilePathW();
    if (path.empty()) return false;
    return removeSessionFile(path);
}

bool sessionExists()
{
    const std::wstring path = sessionFilePathW();
    if (path.empty()) return false;
    const DWORD attr = GetFileAttributesW(path.c_str());
    return attr != INVALID_FILE_ATTRIBUTES && !(attr & FILE_ATTRIBUTE_DIRECTORY);
}

bool pathDaclIsOwnerOnly(const std::wstring& pathW, std::string* detail)
{
    if (detail) detail->clear();
    PACL dacl = nullptr;
    PSECURITY_DESCRIPTOR psd = nullptr;
    const DWORD rc = GetNamedSecurityInfoW(
        pathW.c_str(), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
        nullptr, nullptr, &dacl, nullptr, &psd);
    if (rc != ERROR_SUCCESS || !dacl) {
        if (detail) *detail = "get_dacl_failed";
        if (psd) LocalFree(psd);
        return false;
    }

    bool ownerOnly = false;
    do {
        if (dacl->AceCount != 1) {
            if (detail) *detail = "ace_count_not_1";
            break;
        }
        LPVOID aceRaw = nullptr;
        if (!GetAce(dacl, 0, &aceRaw)) {
            if (detail) *detail = "get_ace_failed";
            break;
        }
        auto* ace = reinterpret_cast<ACCESS_ALLOWED_ACE*>(aceRaw);
        if (ace->Header.AceType != ACCESS_ALLOWED_ACE_TYPE) {
            if (detail) *detail = "ace_not_allow";
            break;
        }
        PSID aceSid = reinterpret_cast<PSID>(&ace->SidStart);

        OwnerOnlySd me;  // reuse to read the current user's SID
        if (!me.ok && me.tokenBuf.empty()) {
            if (detail) *detail = "no_current_sid";
            break;
        }
        if (!EqualSid(aceSid, me.currentUserSid())) {
            if (detail) *detail = "ace_sid_not_current_user";
            break;
        }
        ownerOnly = true;
    } while (false);

    if (psd) LocalFree(psd);
    return ownerOnly;
}

} // namespace broker
} // namespace orion
