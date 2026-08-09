// OrionPack Phase 2A -- Compile-Time String Obfuscation Configuration
//
// Usage:
//   #include "obfs_string.h"
//
//   void example() {
//       // Narrow strings: returns a stack-scoped object convertible to const char*
//       auto msg = OBFS("secret API key");
//       printf("%s\n", static_cast<const char*>(msg));
//       // msg is SecureZeroMemory-wiped when it leaves scope
//
//       // Wide strings: for registry paths and Win32 APIs
//       auto reg = OBFS_W(L"SOFTWARE\\Orion\\License");
//       RegOpenKeyExW(HKEY_LOCAL_MACHINE, reg, ...);
//
//       // Qt integration (automatic when QT_CORE_LIB is defined):
//       //   QString s = OBFS("encrypted for Qt");
//       //   QByteArray b = OBFS("encrypted bytes");
//       //   QLatin1String l = OBFS("latin1 view");
//
//   How it works:
//     1. OBFS("literal") encrypts the string at compile time via constexpr XOR.
//     2. The encrypted bytes live in .rdata -- no plaintext in the binary.
//     3. At runtime a stack-local DecryptedString XORs the bytes back.
//     4. The destructor calls SecureZeroMemory to wipe the plaintext.
//
//   The per-string key is derived from OBFS_GLOBAL_SEED, __LINE__, and
//   __COUNTER__, so identical literals at different call sites produce
//   different ciphertext.
//
//   To re-key every obfuscated string in the build, change OBFS_GLOBAL_SEED
//   and do a full rebuild.

#pragma once
#ifndef ORION_OBFS_CONFIG_H
#define ORION_OBFS_CONFIG_H

// Global seed mixed into every per-string key.
// Change this value to re-key all obfuscated strings in the build.
static constexpr unsigned int OBFS_GLOBAL_SEED = 0x4F52494F; // "ORIO"

#endif // ORION_OBFS_CONFIG_H
