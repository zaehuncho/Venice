#pragma once

#include <string>

// [SERVER-SHARD blocker #5, Codex finding #3/#4] DPAPI session persistence for the
// OrionActivate broker, factored out of OrionActivate.cpp so BrokerSessionStoreTests
// can exercise the security-sensitive file behavior headlessly (owner-only DACL,
// atomic replace, rollback) against a TEMP path — never the real user vault.
//
// The plaintext is the {token,token_id,machine_id} JSON that
// Lethe/bootstrap/shard_bootstrap.c reads. It is CryptProtectData'd (CurrentUser,
// NULL entropy) and written atomically with a current-user-only DACL.
namespace orion {
namespace broker {

// %LOCALAPPDATA%\NexusVision\Orion Native\.vault\shard_session.dat (wide). Empty
// if %LOCALAPPDATA% is unavailable.
std::wstring sessionFilePathW();

// Create NexusVision\Orion Native\.vault under %LOCALAPPDATA% and (re-)assert the
// owner-only protected DACL on the .vault directory. False on failure.
bool ensureVaultDir();

// DPAPI-protect `plaintext` and write it to `finalPathW` atomically (write .tmp,
// FlushFileBuffers, MoveFileEx REPLACE|WRITE_THROUGH). Applies an owner-only
// protected DACL to the file when `applyOwnerDacl`. FAILS CLOSED: on ANY failed
// prerequisite it writes NO final file and removes the temp. Parent dir must
// exist. `err` (optional) gets a short non-secret reason.
bool writeProtectedFileAtomic(const std::wstring& finalPathW,
                              const std::string& plaintext,
                              bool applyOwnerDacl,
                              std::string* err);

// ensureVaultDir() + writeProtectedFileAtomic(sessionFilePathW(), ...).
bool writeSession(const std::string& plaintext, std::string* err);

// Delete a session file AND its sibling .tmp (rollback). Returns true if neither
// remains afterwards (already-absent counts as success).
bool removeSessionFile(const std::wstring& finalPathW);
bool removeSession();

// True iff the session file exists (not a directory).
bool sessionExists();

// Test/inspection helper: true iff `pathW`'s DACL grants access to EXACTLY the
// current user's SID and no one else (one ACE, current-user SID). `detail`
// (optional) gets a short reason on false.
bool pathDaclIsOwnerOnly(const std::wstring& pathW, std::string* detail);

} // namespace broker
} // namespace orion
