// OrionPack Phase 2A -- Compile-Time String Encryption
// C++20 constexpr XOR obfuscation for MSVC cl.exe (/W4 /WX clean)
//
// Encrypts string literals at compile time so they never appear as
// plaintext in the binary.  At runtime, strings decrypt to a stack
// buffer and are wiped (SecureZeroMemory) on scope exit.
//
// See obfs_config.h for usage examples and the global seed.

#pragma once
#ifndef ORION_OBFS_STRING_H
#define ORION_OBFS_STRING_H

// ---- Standard includes ----
#include <cstddef>
#include <cstdint>

// ---- Global seed (include obfs_config.h unless caller pre-defined it) ----
#ifndef OBFS_GLOBAL_SEED
#include "obfs_config.h"
#endif

// ---- Windows (SecureZeroMemory) ----
#ifndef _WINDOWS_
#  ifndef WIN32_LEAN_AND_MEAN
#    define WIN32_LEAN_AND_MEAN
#    define OBFS_UNDEF_LEAN_AND_MEAN_
#  endif
#  ifndef NOMINMAX
#    define NOMINMAX
#    define OBFS_UNDEF_NOMINMAX_
#  endif
#  include <windows.h>
#  ifdef OBFS_UNDEF_LEAN_AND_MEAN_
#    undef WIN32_LEAN_AND_MEAN
#    undef OBFS_UNDEF_LEAN_AND_MEAN_
#  endif
#  ifdef OBFS_UNDEF_NOMINMAX_
#    undef NOMINMAX
#    undef OBFS_UNDEF_NOMINMAX_
#  endif
#endif

// ---- Conditional Qt includes ----
#ifdef QT_CORE_LIB
#  include <QString>
#  include <QByteArray>
#  include <QLatin1String>
#endif

namespace orion::obfs {

// -----------------------------------------------------------------------
// key_byte  --  LCG-based per-byte key derivation (O(1) per call)
//
// Mixes the per-string seed with the byte position through a single
// round of a linear congruential generator (Numerical Recipes constants).
// The position is first scattered with Knuth's multiplicative hash to
// ensure neighbouring bytes produce unrelated keys.
// -----------------------------------------------------------------------
constexpr uint8_t key_byte(uint32_t seed, size_t i) noexcept {
    uint32_t state = seed ^ (static_cast<uint32_t>(i) * 2654435761u);
    state = state * 1664525u + 1013904223u;
    return static_cast<uint8_t>((state >> 16) & 0xFFu);
}

// -----------------------------------------------------------------------
// Encrypted<N, Seed>  --  compile-time encrypted narrow string
//
// The constexpr constructor XORs each byte of the source literal with a
// per-position key.  Because the object is static constexpr at every
// call site, the encrypted payload lives in .rdata with no runtime
// initialisation.
// -----------------------------------------------------------------------
template<size_t N, uint32_t Seed>
struct Encrypted {
    uint8_t data[N]{};

    constexpr Encrypted(const char (&str)[N]) noexcept : data{} {
        for (size_t i = 0; i < N; ++i) {
            data[i] = static_cast<uint8_t>(str[i]) ^ key_byte(Seed, i);
        }
    }
};

// -----------------------------------------------------------------------
// DecryptedString<N>  --  runtime-decrypted narrow string, auto-wipe
//
// Decrypts into a fixed-size char buffer on the stack.  The destructor
// calls SecureZeroMemory so plaintext never outlives the enclosing scope.
// -----------------------------------------------------------------------
template<size_t N>
struct [[nodiscard]] DecryptedString {
    char buf[N]{};

    template<uint32_t Seed>
    explicit DecryptedString(const Encrypted<N, Seed>& enc) noexcept {
        for (size_t i = 0; i < N; ++i) {
            buf[i] = static_cast<char>(enc.data[i] ^ key_byte(Seed, i));
        }
    }

    ~DecryptedString() noexcept {
        SecureZeroMemory(buf, N);
    }

    // Secure move: copies the plaintext then wipes the source.
    DecryptedString(DecryptedString&& other) noexcept {
        for (size_t i = 0; i < N; ++i) {
            buf[i] = other.buf[i];
        }
        SecureZeroMemory(other.buf, N);
    }

    DecryptedString(const DecryptedString&)            = delete;
    DecryptedString& operator=(const DecryptedString&) = delete;
    DecryptedString& operator=(DecryptedString&&)      = delete;

    // Accessors
    const char* c_str() const noexcept { return buf; }
    size_t      size()  const noexcept { return N - 1; }

    // Implicit conversions
    operator const char*() const noexcept { return buf; } // NOLINT

#ifdef QT_CORE_LIB
    operator QString()       const { return QString::fromUtf8(buf, static_cast<int>(N - 1)); }
    operator QByteArray()    const { return QByteArray(buf, static_cast<int>(N - 1)); }
    operator QLatin1String() const { return QLatin1String(buf, static_cast<int>(N - 1)); }
#endif
};

// -----------------------------------------------------------------------
// Wide-string (wchar_t) variants for registry paths / Win32 APIs
// -----------------------------------------------------------------------

static_assert(sizeof(wchar_t) == 2,
    "obfs_string.h requires 16-bit wchar_t (Windows/MSVC)");

// EncryptedW<N, Seed>  --  compile-time encrypted wide string
template<size_t N, uint32_t Seed>
struct EncryptedW {
    uint8_t data[N * 2]{};

    constexpr EncryptedW(const wchar_t (&str)[N]) noexcept : data{} {
        for (size_t i = 0; i < N; ++i) {
            auto ch = static_cast<uint16_t>(str[i]);
            data[i * 2]     = static_cast<uint8_t>(ch & 0xFFu)
                              ^ key_byte(Seed, i * 2);
            data[i * 2 + 1] = static_cast<uint8_t>((ch >> 8) & 0xFFu)
                              ^ key_byte(Seed, i * 2 + 1);
        }
    }
};

// DecryptedWString<N>  --  runtime-decrypted wide string, auto-wipe
template<size_t N>
struct [[nodiscard]] DecryptedWString {
    wchar_t buf[N]{};

    template<uint32_t Seed>
    explicit DecryptedWString(const EncryptedW<N, Seed>& enc) noexcept {
        for (size_t i = 0; i < N; ++i) {
            auto lo = static_cast<uint16_t>(
                enc.data[i * 2]     ^ key_byte(Seed, i * 2));
            auto hi = static_cast<uint16_t>(
                enc.data[i * 2 + 1] ^ key_byte(Seed, i * 2 + 1));
            buf[i] = static_cast<wchar_t>((hi << 8) | lo);
        }
    }

    ~DecryptedWString() noexcept {
        SecureZeroMemory(buf, N * sizeof(wchar_t));
    }

    DecryptedWString(DecryptedWString&& other) noexcept {
        for (size_t i = 0; i < N; ++i) {
            buf[i] = other.buf[i];
        }
        SecureZeroMemory(other.buf, N * sizeof(wchar_t));
    }

    DecryptedWString(const DecryptedWString&)            = delete;
    DecryptedWString& operator=(const DecryptedWString&) = delete;
    DecryptedWString& operator=(DecryptedWString&&)      = delete;

    const wchar_t* c_str() const noexcept { return buf; }
    size_t         size()  const noexcept { return N - 1; }

    operator const wchar_t*() const noexcept { return buf; } // NOLINT
};

} // namespace orion::obfs

// =======================================================================
// Public macros
// =======================================================================

// OBFS("literal")
//   Compile-time encrypts a narrow string literal.  Returns a scoped
//   DecryptedString that converts to const char* (and QString / QByteArray
//   / QLatin1String when Qt is available).  Plaintext is wiped when the
//   returned object destructs.
//
//   auto s = OBFS("hello");          // s lives until scope exit
//   printf("%s", OBFS("inline"));    // temporary lives for the statement

#define OBFS(s)                                                             \
    ([]() noexcept {                                                        \
        constexpr auto _obfs_seed_ =                                        \
            (__LINE__ * 65521u) ^ (__COUNTER__ * 2654435761u)               \
            ^ OBFS_GLOBAL_SEED;                                             \
        static constexpr                                                    \
            ::orion::obfs::Encrypted<sizeof(s), _obfs_seed_> _enc(s);       \
        return ::orion::obfs::DecryptedString<sizeof(s)>(_enc);             \
    }())

// OBFS_W(L"literal")
//   Wide-char variant for registry paths and Win32 W-suffixed APIs.

#define OBFS_W(s)                                                           \
    ([]() noexcept {                                                        \
        constexpr auto _obfs_seed_ =                                        \
            (__LINE__ * 65521u) ^ (__COUNTER__ * 2654435761u)               \
            ^ OBFS_GLOBAL_SEED;                                             \
        static constexpr                                                    \
            ::orion::obfs::EncryptedW<sizeof(s) / sizeof(wchar_t),          \
                                      _obfs_seed_> _enc(s);                 \
        return ::orion::obfs::DecryptedWString<                             \
                   sizeof(s) / sizeof(wchar_t)>(_enc);                      \
    }())

#endif // ORION_OBFS_STRING_H
