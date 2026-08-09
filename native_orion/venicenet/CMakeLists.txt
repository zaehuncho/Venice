# ---------------------------------------------------------------------------
# VeniceNet.dll — in-process network meter-delay engine (C ABI)
# ---------------------------------------------------------------------------
# Replaces the NexusVisionSvc Python packet bridge: OrionNative.exe loads this
# DLL via LoadLibrary and drives it through the extern "C" surface declared in
# VeniceNet.h (contract: docs/VENICENET_API.md).  Deliberately Qt-free — the
# ABI must stay loadable by a plain C client and dynamic-linkable against
# WinDivert64.dll (LGPL-clean) in wave 2.
#
# WAVE 1 ships a deterministic stub (no driver, never armed) so the app side
# and its tests have a concrete surface to build against.

add_library(VeniceNet SHARED
    VeniceNet.cpp
    VeniceNet.h
    VeniceNet_internal.h
    venicenet_service_route.cpp
    venicenet_service_route.h
)
# Reuses the project-wide hardening flags (CFG, CETCOMPAT, /W4...).  The
# function also adds the app's src/ include dir, which VeniceNet deliberately
# never includes from — the DLL depends on nothing but the CRT + Win32.
orion_apply_target_defaults(VeniceNet)
target_compile_definitions(VeniceNet PRIVATE VENICENET_BUILD_DLL)
target_include_directories(VeniceNet PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}")
set_target_properties(VeniceNet PROPERTIES PREFIX "" OUTPUT_NAME "VeniceNet")
if(WIN32)
    # WAVE 2B: the DLL is a loopback-TCP IPC client to the packet-bridge service.
    target_link_libraries(VeniceNet PRIVATE ws2_32)
endif()

if(WIN32)
    # Stage the DLL next to OrionNative.exe so wave 2's LoadLibrary finds it in
    # the app dir without PATH games (same idiom as ViGEmClient.dll).
    if(TARGET OrionNative)
        add_custom_command(TARGET VeniceNet POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different
                    "$<TARGET_FILE:VeniceNet>"
                    "$<TARGET_FILE_DIR:OrionNative>/VeniceNet.dll"
            COMMENT "Staging VeniceNet.dll next to OrionNative.exe"
            VERBATIM
        )
    endif()
endif()

if(ORION_BUILD_TESTS)
    # Plain C++ test client (no Qt): proves the ABI is drivable with nothing on
    # PATH but the DLL, which CMake colocates in this target's output dir.
    # Registered in scripts/verify_orion.ps1 on BOTH build lines (dev + prod).
    add_executable(OrionVeniceNetTests
        tests/VeniceNetStubTests.cpp
    )
    orion_apply_target_defaults(OrionVeniceNetTests)
    target_link_libraries(OrionVeniceNetTests PRIVATE VeniceNet)
    add_test(NAME OrionVeniceNetTests COMMAND OrionVeniceNetTests)

    # WAVE 2B: the IPC client against a mock loopback-bridge server. Proves the
    # wire-protocol translation, snapshot inference, reconnect replay and the
    # service-absent degrade path without the real WinDivert service.
    # Registered in scripts/verify_orion.ps1 on BOTH build lines (dev + prod).
    add_executable(OrionVeniceNetIpcClientTests
        tests/VeniceNetIpcClientTests.cpp
    )
    orion_apply_target_defaults(OrionVeniceNetIpcClientTests)
    target_link_libraries(OrionVeniceNetIpcClientTests PRIVATE VeniceNet)
    if(WIN32)
        target_link_libraries(OrionVeniceNetIpcClientTests PRIVATE ws2_32)
    endif()
    add_test(NAME OrionVeniceNetIpcClientTests COMMAND OrionVeniceNetIpcClientTests)
endif()
