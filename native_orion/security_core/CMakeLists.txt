# ============================================================================
# SecurityCore -- standalone build for OLLVM-obfuscated release
# ============================================================================
# This project is consumed as a CMake ExternalProject by the parent OrionNative
# build when ORION_OBFUSCATE_SECURITY=ON.  It builds SecurityCore.dll using
# clang-cl + OLLVM obfuscation passes (fla, bcf, sub) via toolchain_obfs.cmake,
# then applies Phase 2C symbol/RTTI stripping for additional hardening.
#
# When ORION_OBFUSCATE_SECURITY is OFF (the default), the parent project builds
# SecurityCore inline with its normal MSVC toolchain and this file is unused.
# ============================================================================

cmake_minimum_required(VERSION 3.24)
project(SecurityCore VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_AUTOMOC ON)

# ---------------------------------------------------------------------------
# Paths & version (forwarded by the parent ExternalProject_Add)
# ---------------------------------------------------------------------------
set(ORION_SRC_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../src" CACHE PATH
    "Path to the parent project's src/ directory containing SecurityCore sources")
set(ORION_NATIVE_VERSION "1.0.0" CACHE STRING
    "Orion version string (forwarded from parent project)")

# ---------------------------------------------------------------------------
# Optional production flags (forwarded by parent when applicable)
# ---------------------------------------------------------------------------
option(ORION_PRODUCTION "Enable production compile definitions" OFF)
set(ORION_LIBCRYPTO_SHA256 "" CACHE STRING
    "SHA-256 of production libcrypto-3-x64.dll")
set(ORION_LIBCRYPTO_PATH "" CACHE FILEPATH
    "Path to libcrypto-3-x64.dll to stage next to the built DLL")

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
find_package(Qt6 6.8 REQUIRED COMPONENTS Core Network)

# OrionCommon -- pre-built by the parent project and linked as an imported
# target.  The parent passes the include dir and import library via cache vars.
set(ORION_COMMON_INCLUDE_DIR "" CACHE PATH
    "Include directory for OrionCommon headers (parent's src/)")
set(ORION_COMMON_LIBRARY "" CACHE FILEPATH
    "OrionCommon import library (.lib)")

if(ORION_COMMON_LIBRARY)
    add_library(OrionCommon SHARED IMPORTED)
    set_target_properties(OrionCommon PROPERTIES
        IMPORTED_IMPLIB "${ORION_COMMON_LIBRARY}"
    )
    if(ORION_COMMON_INCLUDE_DIR)
        set_target_properties(OrionCommon PROPERTIES
            INTERFACE_INCLUDE_DIRECTORIES "${ORION_COMMON_INCLUDE_DIR}"
        )
    endif()
endif()

# ---------------------------------------------------------------------------
# SecurityCore shared library
# ---------------------------------------------------------------------------
add_library(SecurityCore SHARED
    "${ORION_SRC_DIR}/BrokerInstallTrust.cpp"
    "${ORION_SRC_DIR}/BrokerInstallTrust.h"
    "${ORION_SRC_DIR}/Ed25519.cpp"
    "${ORION_SRC_DIR}/Ed25519.h"
    "${ORION_SRC_DIR}/LeaseGate.cpp"
    "${ORION_SRC_DIR}/LeaseGate.h"
    "${ORION_SRC_DIR}/LicenseClient.cpp"
    "${ORION_SRC_DIR}/LicenseClient.h"
    "${ORION_SRC_DIR}/MachineIdentity.cpp"
    "${ORION_SRC_DIR}/MachineIdentity.h"
    "${ORION_SRC_DIR}/NetworkSecurity.cpp"
    "${ORION_SRC_DIR}/NetworkSecurity.h"
    "${ORION_SRC_DIR}/PinnedSpki.h"
    "${ORION_SRC_DIR}/ReleaseManifestTrust.h"
    "${ORION_SRC_DIR}/SecurityManager.cpp"
    "${ORION_SRC_DIR}/SecurityManager.h"
    "${ORION_SRC_DIR}/UpdateManifest.cpp"
    "${ORION_SRC_DIR}/UpdateManifest.h"
)

target_include_directories(SecurityCore PUBLIC "${ORION_SRC_DIR}")

target_compile_definitions(SecurityCore PUBLIC
    NOMINMAX
    WIN32_LEAN_AND_MEAN
    ORION_NATIVE_VERSION="${ORION_NATIVE_VERSION}"
)
target_compile_definitions(SecurityCore PRIVATE ORION_SECURITY_BUILD)

if(ORION_PRODUCTION)
    target_compile_definitions(SecurityCore PRIVATE
        ORION_LIBCRYPTO_SHA256="${ORION_LIBCRYPTO_SHA256}"
    )
endif()

if(ORION_LIBCRYPTO_PATH AND EXISTS "${ORION_LIBCRYPTO_PATH}")
    add_custom_command(TARGET SecurityCore POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
                "${ORION_LIBCRYPTO_PATH}"
                "$<TARGET_FILE_DIR:SecurityCore>/libcrypto-3-x64.dll"
        COMMENT "Staging libcrypto-3-x64.dll next to obfuscated SecurityCore"
        VERBATIM
    )
endif()

target_link_libraries(SecurityCore PUBLIC
    Qt6::Core
    Qt6::Network
)
if(TARGET OrionCommon)
    target_link_libraries(SecurityCore PUBLIC OrionCommon)
endif()
if(WIN32)
    # wintrust: BrokerInstallTrust.cpp's Authenticode gate (WinVerifyTrust).
    target_link_libraries(SecurityCore PUBLIC advapi32 crypt32 user32 wintrust)
endif()

# ---------------------------------------------------------------------------
# Base compiler / linker hardening (clang-cl compatible subset)
# ---------------------------------------------------------------------------
# OLLVM obfuscation flags (-mllvm -fla/-bcf/-sub) come from the toolchain
# file; here we add the security-hardening flags compatible with clang-cl.
target_compile_options(SecurityCore PRIVATE
    /W4 /permissive- /EHsc /Zc:__cplusplus /sdl
)
target_link_options(SecurityCore PRIVATE
    /DYNAMICBASE /NXCOMPAT
)

# ---------------------------------------------------------------------------
# Phase 2C: Symbol & RTTI stripping
# ---------------------------------------------------------------------------
# /GR-             Disable RTTI -- removes type_info class name strings that
#                  leak internal class names into the binary.
# /EMITPOGODB:NO   Suppress PGO database emission.  lld-link ignores unknown
#                  flags with a warning, so this is harmless when the OLLVM
#                  toolchain uses lld-link instead of MSVC link.exe.
# PDB:  Release builds already omit /Zi and /DEBUG, so no PDB is generated.
target_compile_options(SecurityCore PRIVATE /GR-)
target_link_options(SecurityCore PRIVATE /EMITPOGODB:NO)
