# ---------------------------------------------------------------------------
# VeniceNetSvc.exe — C++ Windows service port of nexus_svc.py's meter-delay path
# ---------------------------------------------------------------------------
# WAVE 2A. Option 3: keep the SYSTEM service, rewrite in C++, ship no Python
# runtime. This exe is a drop-in for the Nuitka-compiled NexusVisionSvc.exe: it
# registers under the same service name, listens on the same loopback endpoint
# (127.0.0.1:47291) with the same bearer-token file, and speaks the same
# JSON-lines wire protocol as nexus_svc.py — so both the shipped NetworkBridge
# client and wave-2B's thin DLL client drive it byte-for-byte unchanged.
#
# Deliberately Qt-FREE (a service, not a Qt app). Dynamic-links WinDivert64.dll
# at runtime via GetProcAddress (there is no WinDivert import lib in-tree; only
# the LGPL-clean vendor/windivert/ DLL+SYS pair), so no privileged action or
# kernel driver is required to BUILD or unit-test it.

set(VENICENET_SVC_SOURCES
    main.cpp
    IpcServer.cpp
    IpcServer.h
    MeterDelayIntercept.cpp
    MeterDelayIntercept.h
    CourtIpDetector.cpp
    CourtIpDetector.h
    ServiceState.cpp
    ServiceState.h
    ServiceArgs.h
    WinDivertApi.h
    Json.h
)

add_executable(VeniceNetSvc ${VENICENET_SVC_SOURCES})
# Reuses the project-wide hardening flags AND puts native_orion/src on the include
# path, which is how MeterDelayIntercept.cpp reaches PreciseWaitTimer.h (the
# reused high-resolution waitable-timer idiom for the flush loop — wave-1 hazard 5).
orion_apply_target_defaults(VeniceNetSvc)
# orion_apply_target_defaults adds "<caller-dir>/src" to the include path, which
# for this subdirectory is venicenet_service/src (nonexistent). Add the REAL app
# src/ so MeterDelayIntercept.cpp can reach PreciseWaitTimer.h (hazard 5), plus
# this directory for the local headers.
target_include_directories(VeniceNetSvc PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}"
    "${CMAKE_CURRENT_SOURCE_DIR}/../src")

if(WIN32)
    # Console subsystem so wmain() runs the SCM dispatcher / debug console.
    set_target_properties(VeniceNetSvc PROPERTIES WIN32_EXECUTABLE FALSE)
    target_link_libraries(VeniceNetSvc PRIVATE ws2_32 advapi32)

    # Stage the LGPL-clean WinDivert64.dll/.sys pair next to the exe so the
    # runtime GetProcAddress loader finds them in the service's own directory.
    set(_venice_windivert_dir "${CMAKE_CURRENT_SOURCE_DIR}/../../vendor/windivert")
    foreach(_wd_file WinDivert64.dll WinDivert64.sys)
        if(EXISTS "${_venice_windivert_dir}/${_wd_file}")
            add_custom_command(TARGET VeniceNetSvc POST_BUILD
                COMMAND ${CMAKE_COMMAND} -E copy_if_different
                        "${_venice_windivert_dir}/${_wd_file}"
                        "$<TARGET_FILE_DIR:VeniceNetSvc>/${_wd_file}"
                COMMENT "Staging ${_wd_file} next to VeniceNetSvc.exe"
                VERBATIM)
        else()
            message(WARNING "vendor/windivert/${_wd_file} missing; VeniceNetSvc will "
                            "have no WinDivert to dynamic-link at runtime.")
        endif()
    endforeach()
endif()

if(ORION_BUILD_TESTS)
    # Plain C++ (no Qt) test client, mirroring OrionVeniceNetTests. Exercises the
    # PURE logic only — slew accumulator, snap-to-target, buffer overflow policy,
    # protocol parsing, the state machine, court detection and the Remote-Play IP
    # exclusion. NEVER opens WinDivert (kernel driver — won't work in CI), so it
    # links the service TUs but only calls their pure entry points.
    add_executable(OrionVeniceNetServiceTests
        tests/VeniceNetServiceTests.cpp
        IpcServer.cpp
        MeterDelayIntercept.cpp
        CourtIpDetector.cpp
        ServiceState.cpp
    )
    orion_apply_target_defaults(OrionVeniceNetServiceTests)
    target_include_directories(OrionVeniceNetServiceTests PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}"
        "${CMAKE_CURRENT_SOURCE_DIR}/../src")
    if(WIN32)
        target_link_libraries(OrionVeniceNetServiceTests PRIVATE ws2_32 advapi32)
    endif()
    add_test(NAME OrionVeniceNetServiceTests COMMAND OrionVeniceNetServiceTests)
endif()
