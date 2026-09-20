cmake_minimum_required(VERSION 3.24)

project(OrionNative
    VERSION 1.0.0
    LANGUAGES CXX
)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(CMAKE_AUTOMOC ON)
set(CMAKE_AUTORCC ON)
set(CMAKE_AUTOUIC ON)

option(ORION_BUILD_TESTS "Build native Orion tests" ON)
option(ORION_REQUIRE_OPENCV "Fail configure if OpenCV is unavailable" OFF)
option(ORION_PRODUCTION_HARDENING "Enable production-oriented native compiler/linker hardening" ON)
option(ORION_ENABLE_SPECTRE_MITIGATION "Enable MSVC /Qspectre; requires Spectre-mitigated libraries installed" OFF)
# Production switch: compiles out EVERY developer escape hatch (local dev license,
# update-gate skip, debug UI, calibration freeze env, settings auto-sign) and makes
# the release-integrity manifest mandatory regardless of the on-disk policy file.
# tools/package_orion_release.py refuses to package a build configured without it.
option(ORION_PRODUCTION "Production build: compile out all developer escape hatches" OFF)
set(ORION_LIBCRYPTO_PATH "" CACHE FILEPATH
    "Exact libcrypto-3-x64.dll staged beside runtime binaries; production builds also hash-pin it")
set(ORION_LIBCRYPTO_SHA256 "" CACHE STRING
    "Build-pinned SHA-256 of production libcrypto-3-x64.dll (normally computed from ORION_LIBCRYPTO_PATH)")
if(ORION_LIBCRYPTO_PATH)
    get_filename_component(ORION_LIBCRYPTO_PATH "${ORION_LIBCRYPTO_PATH}" ABSOLUTE
                           BASE_DIR "${CMAKE_CURRENT_SOURCE_DIR}")
    if(NOT EXISTS "${ORION_LIBCRYPTO_PATH}")
        message(FATAL_ERROR "ORION_LIBCRYPTO_PATH does not exist: ${ORION_LIBCRYPTO_PATH}")
    endif()
    file(SHA256 "${ORION_LIBCRYPTO_PATH}" _orion_libcrypto_computed_sha256)
    string(TOLOWER "${_orion_libcrypto_computed_sha256}" _orion_libcrypto_computed_sha256)
    if(ORION_LIBCRYPTO_SHA256)
        string(TOLOWER "${ORION_LIBCRYPTO_SHA256}" _orion_libcrypto_requested_sha256)
        if(NOT _orion_libcrypto_requested_sha256 STREQUAL _orion_libcrypto_computed_sha256)
            message(FATAL_ERROR
                "ORION_LIBCRYPTO_SHA256 does not match ORION_LIBCRYPTO_PATH bytes")
        endif()
    endif()
    set(ORION_LIBCRYPTO_SHA256 "${_orion_libcrypto_computed_sha256}" CACHE STRING
        "Build-pinned SHA-256 of production libcrypto-3-x64.dll" FORCE)
endif()
if(ORION_PRODUCTION)
    string(TOLOWER "${ORION_LIBCRYPTO_SHA256}" ORION_LIBCRYPTO_SHA256)
    string(LENGTH "${ORION_LIBCRYPTO_SHA256}" _orion_libcrypto_hash_length)
    if(NOT _orion_libcrypto_hash_length EQUAL 64
       OR NOT ORION_LIBCRYPTO_SHA256 MATCHES "^[0-9a-f]+$")
        message(FATAL_ERROR
            "ORION_PRODUCTION requires a 64-hex ORION_LIBCRYPTO_SHA256. Prefer "
            "-DORION_LIBCRYPTO_PATH=<exact libcrypto-3-x64.dll> so CMake computes it.")
    endif()
    add_compile_definitions(ORION_PRODUCTION_BUILD=1)
endif()

find_package(Qt6 6.8 REQUIRED COMPONENTS Core Gui Widgets Network Qml Quick QuickControls2)
if(ORION_BUILD_TESTS)
    find_package(Qt6 6.8 REQUIRED COMPONENTS Test)
endif()
if(COMMAND qt_policy)
    qt_policy(SET QTP0001 NEW)
    qt_policy(SET QTP0004 NEW)
endif()

find_package(OpenCV QUIET COMPONENTS core imgproc videoio)
if(NOT OpenCV_FOUND AND ORION_REQUIRE_OPENCV)
    message(FATAL_ERROR "OpenCV was not found. Set OpenCV_DIR to the OpenCVConfig.cmake folder.")
endif()

function(orion_apply_target_defaults target)
    target_include_directories(${target} PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/src")
    target_compile_definitions(${target} PUBLIC
        NOMINMAX
        WIN32_LEAN_AND_MEAN
    )
    if(MSVC)
        target_compile_options(${target} PRIVATE /W4 /permissive- /EHsc /Zc:__cplusplus /sdl /guard:cf)
        target_link_options(${target} PRIVATE /DYNAMICBASE /NXCOMPAT /guard:cf /CETCOMPAT)
        # [2026-09-11] Release builds carry a PDB: two shutdown crash dumps could only be read down to
        # "somewhere in the precise-fire thread" because the Release exe shipped no symbols.
        target_compile_options(${target} PRIVATE $<$<CONFIG:Release>:/Zi>)
        target_link_options(${target} PRIVATE $<$<CONFIG:Release>:/DEBUG:FULL>)
        if(ORION_PRODUCTION_HARDENING)
            target_compile_definitions(${target} PRIVATE
                $<$<CONFIG:Release>:ORION_RELEASE_HARDENED=1>
            )
            target_compile_options(${target} PRIVATE
                $<$<CONFIG:Release>:/O2 /GL /Gw /Gy /GS /ZH:SHA_256>
            )
            if(ORION_ENABLE_SPECTRE_MITIGATION)
                target_compile_options(${target} PRIVATE $<$<CONFIG:Release>:/Qspectre>)
            endif()
            target_link_options(${target} PRIVATE
                $<$<CONFIG:Release>:/LTCG /OPT:REF /OPT:ICF /INCREMENTAL:NO>
            )
        endif()
    else()
        target_compile_options(${target} PRIVATE -Wall -Wextra -Wpedantic)
    endif()
endfunction()

function(orion_apply_shared_defaults target)
    orion_apply_target_defaults(${target})
endfunction()

add_library(OrionCommon SHARED
    src/AppConfig.cpp
    src/AppConfig.h
    # [ORION_BANNER_LEAD_TRIM 2026-09-15] header-only policy, listed so it shows in the IDE tree
    # and so the settings/learning layer's dependency on it is explicit.
    src/BannerLeadTrim.h
    src/Diagnostics.cpp
    src/Diagnostics.h
    src/OrionTypes.h
    src/RemotePlayExecutablePolicy.h
)
orion_apply_shared_defaults(OrionCommon)
target_compile_definitions(OrionCommon PRIVATE ORION_COMMON_BUILD)
target_link_libraries(OrionCommon PUBLIC Qt6::Core)

add_library(AutomationCore SHARED
    src/AutomationEngine.cpp
    src/AutomationEngine.h
    src/ControllerDeviceSelector.cpp
    src/ControllerDeviceSelector.h
    # Inbound meter delay. COMPILED but deliberately NOT CONSTRUCTED: nothing in
    # OrionAppController instantiates MeterDelayController, and nexus_svc ships
    # with the intercept disarmed, so this is dormant code with test coverage
    # rather than a live feature. See the header for why, and
    # tools/meter_delay_bench.py for the offline proof of its safety property.
    src/MeterDelayController.cpp
    src/MeterDelayController.h
)
orion_apply_shared_defaults(AutomationCore)
target_compile_definitions(AutomationCore PRIVATE ORION_AUTOMATION_BUILD)
target_link_libraries(AutomationCore PUBLIC
    OrionCommon
    Qt6::Core
)

add_library(VisionCore SHARED
    src/MeterDetector.cpp
    src/MeterDetector.h
)
orion_apply_shared_defaults(VisionCore)
target_compile_definitions(VisionCore PRIVATE ORION_VISION_BUILD)
target_link_libraries(VisionCore PUBLIC
    OrionCommon
    Qt6::Core
)
if(OpenCV_FOUND)
    target_compile_definitions(VisionCore PUBLIC ORION_WITH_OPENCV=1)
    target_include_directories(VisionCore PUBLIC ${OpenCV_INCLUDE_DIRS})
    target_link_libraries(VisionCore PUBLIC ${OpenCV_LIBS})
else()
    target_compile_definitions(VisionCore PUBLIC ORION_WITH_OPENCV=0)
endif()

add_library(RemotePlayCore SHARED
    src/FrameDecoder.cpp
    src/FrameDecoder.h
    src/SharedMemoryFrameReader.cpp
    src/SharedMemoryFrameReader.h
    src/SharedMemoryFramePump.cpp
    src/SharedMemoryFramePump.h
    src/VideoInputDeviceEnumeration.cpp
    src/VideoInputDeviceEnumeration.h
    src/NetworkBridge.cpp
    src/NetworkBridge.h
    src/PassiveCourtFlowClassifier.h
    src/PoseArmProtocol.h
    src/PreviewPresentationBuffer.h
    src/RemotePlayDecodePolicy.h
    src/RemotePlaySession.cpp
    src/RemotePlaySession.h
    src/ShotGateProtocol.h
    src/VirtualController.cpp
    src/VirtualController.h
)
orion_apply_shared_defaults(RemotePlayCore)
target_compile_definitions(RemotePlayCore PRIVATE ORION_REMOTEPLAY_BUILD)
target_link_libraries(RemotePlayCore PUBLIC
    OrionCommon
    Qt6::Core
    Qt6::Gui
    Qt6::Network
)
if(WIN32)
    target_link_libraries(RemotePlayCore PUBLIC setupapi hid winmm xinput ws2_32 dwmapi ole32 oleaut32 strmiids user32)
endif()

# ---------------------------------------------------------------------------
# Phase 2B: OLLVM-obfuscated SecurityCore (opt-in via ExternalProject)
# ---------------------------------------------------------------------------
# When ON, SecurityCore is built as a separate CMake project using clang-cl
# with OLLVM obfuscation passes (fla/bcf/sub) and Phase 2C symbol stripping.
# The resulting DLL is imported back so downstream targets link transparently.
# Requires: Ninja generator, an OLLVM fork installed at the paths in
# security_core/toolchain_obfs.cmake.
# When OFF (default), SecurityCore builds inline with the normal toolchain.
option(ORION_OBFUSCATE_SECURITY "Build SecurityCore with OLLVM obfuscation" OFF)

if(ORION_OBFUSCATE_SECURITY)
    include(ExternalProject)
    set(_sc_obfs_build_dir "${CMAKE_BINARY_DIR}/SecurityCore_obfs")

    # Forward production flags to the sub-project when applicable.
    set(_sc_obfs_extra_args "")
    if(ORION_PRODUCTION)
        list(APPEND _sc_obfs_extra_args
            -DORION_PRODUCTION=ON
            -DORION_LIBCRYPTO_SHA256=${ORION_LIBCRYPTO_SHA256}
        )
    endif()
    if(ORION_LIBCRYPTO_PATH)
        list(APPEND _sc_obfs_extra_args
            -DORION_LIBCRYPTO_PATH=${ORION_LIBCRYPTO_PATH}
        )
    endif()

    ExternalProject_Add(SecurityCore_Obfs
        SOURCE_DIR  "${CMAKE_CURRENT_SOURCE_DIR}/security_core"
        BINARY_DIR  "${_sc_obfs_build_dir}"
        CMAKE_GENERATOR "Ninja"
        CMAKE_ARGS
            -DCMAKE_TOOLCHAIN_FILE=${CMAKE_CURRENT_SOURCE_DIR}/security_core/toolchain_obfs.cmake
            -DCMAKE_BUILD_TYPE=Release
            -DQt6_DIR=${Qt6_DIR}
            -DORION_SRC_DIR=${CMAKE_CURRENT_SOURCE_DIR}/src
            -DORION_COMMON_INCLUDE_DIR=${CMAKE_CURRENT_SOURCE_DIR}/src
            -DORION_COMMON_LIBRARY=$<TARGET_LINKER_FILE:OrionCommon>
            -DORION_NATIVE_VERSION=${PROJECT_VERSION}
            ${_sc_obfs_extra_args}
        BUILD_BYPRODUCTS
            "${_sc_obfs_build_dir}/SecurityCore.lib"
            "${_sc_obfs_build_dir}/SecurityCore.dll"
        BUILD_ALWAYS 1
        INSTALL_COMMAND ""
        DEPENDS OrionCommon
    )

    # Imported target so downstream targets (OrionNative, OrionUpdater, tests,
    # admin tools) link against the obfuscated SecurityCore transparently.
    add_library(SecurityCore SHARED IMPORTED GLOBAL)
    set_target_properties(SecurityCore PROPERTIES
        IMPORTED_IMPLIB   "${_sc_obfs_build_dir}/SecurityCore.lib"
        IMPORTED_LOCATION "${_sc_obfs_build_dir}/SecurityCore.dll"
        INTERFACE_INCLUDE_DIRECTORIES "${CMAKE_CURRENT_SOURCE_DIR}/src"
    )
    set_property(TARGET SecurityCore APPEND PROPERTY
        INTERFACE_COMPILE_DEFINITIONS "ORION_NATIVE_VERSION=\"${PROJECT_VERSION}\""
    )
    target_link_libraries(SecurityCore INTERFACE
        OrionCommon Qt6::Core Qt6::Network
    )
    if(WIN32)
        target_link_libraries(SecurityCore INTERFACE advapi32 crypt32 user32)
    endif()
    add_dependencies(SecurityCore SecurityCore_Obfs)

    # Stage the obfuscated SecurityCore.dll next to each consumer exe.
    # In the OFF path SecurityCore builds inline and is already colocated.
    foreach(_sc_consumer OrionNative OrionUpdater)
        add_custom_command(TARGET ${_sc_consumer} POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different
                    "$<TARGET_FILE:SecurityCore>"
                    "$<TARGET_FILE_DIR:${_sc_consumer}>/"
            COMMENT "Staging obfuscated SecurityCore.dll next to ${_sc_consumer}"
            VERBATIM)
    endforeach()
    if(ORION_LIBCRYPTO_PATH)
        foreach(_sc_consumer OrionNative OrionUpdater)
            add_custom_command(TARGET ${_sc_consumer} POST_BUILD
                COMMAND ${CMAKE_COMMAND} -E copy_if_different
                        "${_sc_obfs_build_dir}/libcrypto-3-x64.dll"
                        "$<TARGET_FILE_DIR:${_sc_consumer}>/"
                COMMENT "Staging libcrypto-3-x64.dll next to ${_sc_consumer}"
                VERBATIM)
        endforeach()
    endif()
endif()

if(NOT ORION_OBFUSCATE_SECURITY)
add_library(SecurityCore SHARED
    src/Ed25519.cpp
    src/Ed25519.h
    src/LeaseGate.cpp
    src/LeaseGate.h
    src/LicenseClient.cpp
    src/LicenseClient.h
    src/NetworkSecurity.cpp
    src/NetworkSecurity.h
    src/ReleaseManifestTrust.h
    src/SecurityManager.cpp
    src/SecurityManager.h
    src/UpdateManifest.cpp
    src/UpdateManifest.h
)
orion_apply_shared_defaults(SecurityCore)
target_compile_definitions(SecurityCore PUBLIC
    ORION_NATIVE_VERSION="${PROJECT_VERSION}"
)
target_compile_definitions(SecurityCore PRIVATE ORION_SECURITY_BUILD)
if(ORION_PRODUCTION)
    target_compile_definitions(SecurityCore PRIVATE
        ORION_LIBCRYPTO_SHA256="${ORION_LIBCRYPTO_SHA256}"
    )
endif()
if(ORION_LIBCRYPTO_PATH)
    add_custom_command(TARGET SecurityCore POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
                "${ORION_LIBCRYPTO_PATH}"
                "$<TARGET_FILE_DIR:SecurityCore>/libcrypto-3-x64.dll"
        COMMENT "Staging configured libcrypto-3-x64.dll for Ed25519 verification"
        VERBATIM
    )
endif()
target_link_libraries(SecurityCore PUBLIC
    OrionCommon
    Qt6::Core
    Qt6::Network
)
if(WIN32)
    target_link_libraries(SecurityCore PUBLIC advapi32 crypt32 user32)
endif()
endif()

# UpdaterCore holds the path-traversal-safe archive extraction and backup/restore
# helpers used by OrionUpdater.exe. QZipReader/QZipWriter live in Qt's private Core
# module, so this target links Qt6::CorePrivate.
add_library(UpdaterCore SHARED
    src/UpdaterArchive.cpp
    src/UpdaterArchive.h
)
orion_apply_shared_defaults(UpdaterCore)
target_compile_definitions(UpdaterCore PRIVATE ORION_UPDATER_BUILD)
target_link_libraries(UpdaterCore PUBLIC
    OrionCommon
    Qt6::Core
    Qt6::CorePrivate
)

# OrionUpdater.exe is a standalone self-update helper shipped next to
# OrionNative.exe. The launcher hands off to it (it cannot overwrite itself while
# running), then exits; the updater waits, verifies, replaces, and relaunches.
add_executable(OrionUpdater
    src/updater_main.cpp
)
orion_apply_target_defaults(OrionUpdater)
# Pinned Ed25519 update-signing PUBLIC key (not secret). Matches SSM
# /orion/ed25519_public_key on the backend; see docs/UPDATER_CLIENT.md. Embedding
# it makes the trusted key tamper-resistant vs. a side-loaded update_pubkeys.json.
target_compile_definitions(OrionUpdater PRIVATE
    ORION_NATIVE_VERSION="${PROJECT_VERSION}"
    ORION_UPDATE_ED25519_PUBKEY_ID="orion-ed25519-v1"
    ORION_UPDATE_ED25519_PUBKEY="OJQ2E7ZAFClOCM4S4/5QzLeQjjEMSZjPiGMzbiLZdWs="
)
target_link_libraries(OrionUpdater PRIVATE
    OrionCommon
    SecurityCore
    UpdaterCore
    Qt6::Core
    Qt6::Network
    Qt6::Widgets
)
if(WIN32)
    target_link_libraries(OrionUpdater PRIVATE advapi32 crypt32 user32)
    set_target_properties(OrionUpdater PROPERTIES WIN32_EXECUTABLE TRUE)
endif()

add_executable(OrionNative
    src/main.cpp
    src/DeepLinkTargetPolicy.cpp
    src/DeepLinkTargetPolicy.h
    src/OrderedFileLogSink.cpp
    src/OrderedFileLogSink.h
    src/OrionAppController.cpp
    src/OrionAppController.h
    src/OrionInputClient.cpp
    src/OrionInputClient.h
    # [ORION_PACKET_BRIDGE_NAMES task #64] The sc.exe start/query path consumes
    # venicenet::candidateServiceNames() (VeniceNetSvc first, NexusVisionSvc
    # legacy) through this header; the route .cpp is compiled in directly
    # because the DLL is LoadLibrary-only (never import-linked).
    src/PacketBridgeServiceNames.h
    venicenet/venicenet_service_route.cpp
    # [VENICENET WAVE 2B] Qt wrapper that LoadLibrary's VeniceNet.dll (the
    # meter-delay IPC client). Never links the DLL import lib — resolved at
    # runtime, so a stale install missing the DLL degrades gracefully.
    src/VeniceNetClient.cpp
    src/VeniceNetClient.h
)
orion_apply_target_defaults(OrionNative)
# VeniceNetClient.h includes the ratified C ABI header (venicenet/VeniceNet.h).
target_include_directories(OrionNative PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/venicenet")
target_compile_definitions(OrionNative PRIVATE
    ORION_NATIVE_VERSION="${PROJECT_VERSION}"
)
target_link_libraries(OrionNative PRIVATE
    OrionCommon
    AutomationCore
    VisionCore
    RemotePlayCore
    SecurityCore
    Qt6::Core
    Qt6::CorePrivate   # QZipWriter for the redacted diagnostics bundle
    Qt6::Gui
    Qt6::Widgets
    Qt6::Network
    Qt6::Qml
    Qt6::Quick
    Qt6::QuickControls2
)

set_source_files_properties(qml/Theme.qml PROPERTIES QT_QML_SINGLETON_TYPE TRUE)
set_source_files_properties(qml/components/TourRegistry.qml PROPERTIES QT_QML_SINGLETON_TYPE TRUE)

set(ORION_ADMIN_QML_FILES
    qml/admin/AdminMain.qml
    qml/admin/AdminLoginPane.qml
    qml/admin/AdminDashboardPage.qml
    qml/admin/AdminLicensesPage.qml
    qml/admin/AdminStaffPage.qml
    qml/admin/AdminAuditPage.qml
    qml/admin/AdminConfigPage.qml
    qml/admin/AdminAccountPage.qml
    qml/admin/AdminButton.qml
    qml/admin/AdminCard.qml
    qml/admin/AdminCombo.qml
    qml/admin/AdminField.qml
    qml/admin/AdminJsonView.qml
    qml/admin/AdminKeyValue.qml
    qml/admin/AdminPill.qml
    qml/admin/AdminReasonField.qml
    qml/admin/AdminSecretReveal.qml
    qml/admin/AdminStatTile.qml
    qml/admin/AdminToggle.qml
    qml/Theme.qml
)

function(orion_add_admin_tool target mode_def uri)
    add_executable(${target}
        src/admin_tool_main.cpp
        src/AdminToolController.cpp
        src/AdminToolController.h
    )
    orion_apply_target_defaults(${target})
    target_compile_definitions(${target} PRIVATE
        ORION_NATIVE_VERSION="${PROJECT_VERSION}"
        ${mode_def}
    )
    target_link_libraries(${target} PRIVATE
        OrionCommon
        SecurityCore
        Qt6::Core
        Qt6::Gui
        Qt6::Widgets
        Qt6::Network
        Qt6::Qml
        Qt6::Quick
        Qt6::QuickControls2
    )
    qt_add_qml_module(${target}
        URI ${uri}
        VERSION 1.0
        QML_FILES
            ${ORION_ADMIN_QML_FILES}
    )
    if(WIN32)
        target_link_libraries(${target} PRIVATE advapi32 crypt32 user32)
        set_target_properties(${target} PROPERTIES WIN32_EXECUTABLE TRUE)
    endif()
endfunction()

orion_add_admin_tool(OrionOwner ORION_OWNER_TOOL OrionOwner)
orion_add_admin_tool(OrionStaff ORION_STAFF_TOOL OrionStaff)

qt_add_qml_module(OrionNative
    URI OrionNative
    VERSION 1.0
    QML_FILES
        qml/Main.qml
        qml/Theme.qml
        qml/components/AppShell.qml
        qml/components/Sidebar.qml
        qml/components/FirstRunTour.qml
        qml/components/TourRegistry.qml
        qml/components/TopStatusBar.qml
        qml/components/Card.qml
        qml/components/StatusPill.qml
        qml/components/TelemetryCard.qml
        qml/components/LogViewer.qml
        qml/components/PrimaryButton.qml
        qml/components/DangerButton.qml
        qml/components/ThemedSlider.qml
        qml/components/InfoTip.qml
        qml/components/DashboardToggle.qml
        qml/components/DashboardCombo.qml
        qml/components/ColorWheel.qml
        qml/components/OrionBanner.qml
        qml/components/VeniceBackdrop.qml
        qml/components/NavIcon.qml
        qml/components/StreamSetupForm.qml
        qml/components/MeterConfigPanel.qml
        qml/components/MeterTelemetryHud.qml
        qml/components/ShotLeadCard.qml
        qml/components/RhythmCard.qml
        qml/components/NoMeterCard.qml
        qml/components/ShotLeadUsableMaxIndicator.qml
        qml/components/ShotVerdictTally.qml
        qml/components/TipTimingCard.qml
        qml/components/ColorSwatchRow.qml
        qml/components/PressedBadge.qml
        qml/components/VeniceProfileCard.qml
        qml/pages/AuthGate.qml
        qml/pages/StreamSetupGate.qml
        qml/pages/LegalGate.qml
        qml/pages/UpdateGatePage.qml
        qml/pages/RemotePlayPage.qml
        qml/pages/DashboardPage.qml
        qml/pages/PatchNotesPage.qml
        qml/pages/DebugPage.qml
)

if(WIN32)
    target_link_libraries(OrionNative PRIVATE advapi32 crypt32 user32 setupapi hid winmm xinput ws2_32 dwmapi ole32 oleaut32 strmiids iphlpapi)

    # GUI subsystem: no console window on launch. OrionNative logs to files, not
    # the console, so nothing depends on attached std handles (same as OrionUpdater
    # and the admin tools above).
    set_target_properties(OrionNative PROPERTIES WIN32_EXECUTABLE TRUE)

    # Customer-facing executable identity (Venice) + embedded application icon.
    # Without this resource the exe carries no version block at all, so Task Manager
    # falls back to the raw filename and the shell shows a generic icon until Qt
    # sets the window icon at runtime. See venice.rc.in for the naming split (the
    # FILENAME stays OrionNative.exe on purpose — the updater depends on it).
    # The icon path is injected as an absolute path because the configured .rc lands
    # in the build tree, where a source-relative path would not resolve.
    set(VENICE_ICON_PATH "${CMAKE_CURRENT_SOURCE_DIR}/../assets/orion.ico")
    if(EXISTS "${VENICE_ICON_PATH}")
        # The RC compiler treats backslashes as escapes; forward slashes are safe.
        file(TO_CMAKE_PATH "${VENICE_ICON_PATH}" VENICE_ICON_PATH)
        configure_file(venice.rc.in "${CMAKE_CURRENT_BINARY_DIR}/venice.rc" @ONLY)
        target_sources(OrionNative PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/venice.rc")
    else()
        message(WARNING "assets/orion.ico not found; OrionNative will build without "
                        "an embedded icon or version resource (Task Manager will show "
                        "the raw filename).")
    endif()

    # Auto-copy ViGEmClient.dll next to OrionNative.exe so the virtual controller
    # can load it at runtime without requiring a system-wide install of the DLL.
    set(_orion_vigem_dll "${CMAKE_CURRENT_SOURCE_DIR}/deploy/ViGEmClient.dll")
    if(NOT EXISTS "${_orion_vigem_dll}")
        set(_orion_vigem_dll "${CMAKE_CURRENT_SOURCE_DIR}/vendor/vigem/ViGEmClient.dll")
    endif()
    if(EXISTS "${_orion_vigem_dll}")
        add_custom_command(TARGET OrionNative POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different
                    "${_orion_vigem_dll}"
                    "$<TARGET_FILE_DIR:OrionNative>/ViGEmClient.dll"
            COMMENT "Copying ViGEmClient.dll next to OrionNative.exe"
            VERBATIM
        )
    else()
        message(WARNING "ViGEmClient.dll not found in deploy/ or vendor/vigem/. Virtual controller will not work.")
    endif()

    # Copy branding assets (logo png/ico) next to the exe so the logo loads in a
    # production build where rootDir == the exe dir (dev builds resolve to the repo
    # root via the settings.json fallback). Keeps the window/taskbar + in-app mark.
    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/../assets")
        add_custom_command(TARGET OrionNative POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_directory
                    "${CMAKE_CURRENT_SOURCE_DIR}/../assets"
                    "$<TARGET_FILE_DIR:OrionNative>/assets"
            COMMENT "Copying branding assets next to OrionNative.exe"
            VERBATIM
        )
    endif()

    if(OpenCV_FOUND AND DEFINED OpenCV_DIR)
        get_filename_component(_orion_opencv_root "${OpenCV_DIR}" ABSOLUTE)
        set(_orion_opencv_bin "${_orion_opencv_root}/x64/vc16/bin")
        if(EXISTS "${_orion_opencv_bin}")
            file(GLOB _orion_opencv_runtime_dlls "${_orion_opencv_bin}/opencv*.dll")
            foreach(_orion_opencv_dll IN LISTS _orion_opencv_runtime_dlls)
                add_custom_command(TARGET OrionNative POST_BUILD
                    COMMAND ${CMAKE_COMMAND} -E copy_if_different
                            "${_orion_opencv_dll}"
                            "$<TARGET_FILE_DIR:OrionNative>/"
                    COMMENT "Copying OpenCV runtime DLLs next to OrionNative.exe"
                    VERBATIM
                )
            endforeach()
        endif()
    endif()

    # Deploy the Qt runtime (QML module plugins QtQuick/QtQml/... + platform/image/tls
    # plugins) next to the exe so the build is self-contained. Without this a clean
    # build lacks the QML modules and the app fails to start (can't resolve
    # `import QtQuick`). windeployqt is found relative to the Qt6 install root.
    get_filename_component(_orion_qt_bin "${Qt6_DIR}/../../../bin" ABSOLUTE)
    find_program(ORION_WINDEPLOYQT NAMES windeployqt windeployqt.exe HINTS "${_orion_qt_bin}")
    if(ORION_WINDEPLOYQT)
        add_custom_command(TARGET OrionNative POST_BUILD
            COMMAND "${ORION_WINDEPLOYQT}" --release --no-translations --qmldir "${CMAKE_CURRENT_SOURCE_DIR}/qml"
                    "$<TARGET_FILE:OrionNative>"
            COMMENT "Deploying Qt runtime (windeployqt: QML modules + plugins)"
            VERBATIM
        )
    else()
        message(WARNING "windeployqt not found near ${_orion_qt_bin}; the build will not be self-contained (run windeployqt manually).")
    endif()
endif()

# [VENICENET WAVE 1 2026-08-08] In-process network meter-delay engine (C ABI
# DLL, Qt-free).  Replaces the NexusVisionSvc Python bridge; see
# docs/VENICENET_API.md.  Added AFTER OrionNative so the subdirectory can stage
# VeniceNet.dll next to the exe (if(TARGET OrionNative) guard).  Testing must
# be enabled BEFORE the subdirectory is processed or its add_test() is silently
# dropped from the generated CTest metadata (the duplicate enable_testing()
# below is harmless).
if(ORION_BUILD_TESTS)
    enable_testing()
endif()
add_subdirectory(venicenet)

# [VENICENET WAVE 2A 2026-08-08] VeniceNetSvc.exe — C++ Windows service port of
# nexus_svc.py's meter-delay path (Option 3: keep the SYSTEM service, rewrite in
# C++, ship no Python runtime). Qt-free; dynamic-links WinDivert64.dll. Added
# after OrionNative (so it can reuse the src/ include path for PreciseWaitTimer.h)
# and after enable_testing() so its add_test() lands in the CTest metadata.
add_subdirectory(venicenet_service)

if(ORION_BUILD_TESTS)
    enable_testing()
    # Pure timing mathematics is always tested, independently of Qt's GUI
    # harness. This component has no release authority until measured priors
    # and boundaries are supplied by the live pipeline.
    add_executable(GreenWindowMathChecks tests/GreenWindowMathTests.cpp)
    orion_apply_target_defaults(GreenWindowMathChecks)
    add_test(NAME GreenWindowMathChecks COMMAND GreenWindowMathChecks)
    # GUI-subsystem QtTest output is not reliably streamed into CTest on Windows.
    # Record the original invocation through a bounded wrapper; no retry or
    # assertion changes. Each target/configuration/run gets independent evidence.
    if(WIN32)
        find_program(ORION_TEST_POWERSHELL NAMES powershell.exe REQUIRED)
    endif()
    function(orion_add_qtest target)
        if(WIN32)
            add_test(NAME ${target}
                COMMAND "${ORION_TEST_POWERSHELL}"
                    -NoProfile -NonInteractive -ExecutionPolicy Bypass
                    -File "${CMAKE_CURRENT_SOURCE_DIR}/../scripts/run_orion_qtest.ps1"
                    -TestExecutable "$<TARGET_FILE:${target}>"
                    -TestName "${target}" -Configuration "$<CONFIG>"
                    -LogDirectory "${CMAKE_CURRENT_BINARY_DIR}/Testing/QtTest"
                    -TimeoutSeconds 120
            )
            # The child receives the original 120 s bound; 15 s is wrapper
            # startup/termination/evidence grace, not extra test execution time.
            set_tests_properties(${target} PROPERTIES TIMEOUT 135)
        else()
            add_test(NAME ${target} COMMAND ${target})
        endif()
    endfunction()
    add_executable(OrionNativeTests
        tests/AutomationEngineTests.cpp
        # [ORION_PACKET_BRIDGE_NAMES task #64] scServiceStartTriesBothNames pins the
        # VeniceNetSvc/NexusVisionSvc resolution against the ratified candidate list.
        venicenet/venicenet_service_route.cpp
    )
    orion_apply_target_defaults(OrionNativeTests)
    # The verifier builds named targets, not ALL_BUILD. Keep the pure math
    # executable fresh before CTest instead of relying on a prior manual build.
    add_dependencies(OrionNativeTests GreenWindowMathChecks)
    # [ORION_NO_METER_V2 2026-09-14 owner] The NO METER (input-timed) path is back IN the
    # shipped app: the Live page carries the METER / NO METER switch again and AppConfig no
    # longer forces input_timed_enabled=false. Nothing is compiled out and no target needs a
    # define — the tests drive the same code the product runs.
    target_include_directories(OrionNativeTests PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/venicenet")
    target_link_libraries(OrionNativeTests PRIVATE
        OrionCommon
        AutomationCore
        VisionCore
        RemotePlayCore   # FrameDecoder async-decode path coverage (pulls Qt6::Gui/QImage transitively)
        SecurityCore
        Qt6::Core
        Qt6::Test
    )
    if(WIN32)
        target_link_libraries(OrionNativeTests PRIVATE advapi32 crypt32 user32)
    endif()
    orion_add_qtest(OrionNativeTests)
    add_executable(OrionFireEpochClockTests
        tests/FireEpochClockTests.cpp
    )
    orion_apply_target_defaults(OrionFireEpochClockTests)
    target_link_libraries(OrionFireEpochClockTests PRIVATE
        Qt6::Core
        Qt6::Test
    )
    orion_add_qtest(OrionFireEpochClockTests)
    # Standalone so it builds and runs without the whole automation stack --
    # MeterDelayController depends on nothing but Qt6::Core.
    add_executable(OrionMeterDelayTests
        tests/MeterDelayControllerTests.cpp
        src/MeterDelayController.cpp
        src/MeterDelayController.h
    )
    orion_apply_target_defaults(OrionMeterDelayTests)
    # [ORION_METER_DELAY_STANDALONE_BUILD 2026-08-07] The header marks the class
    # ORION_AUTOMATION_API which resolves to __declspec(dllimport) unless
    # ORION_AUTOMATION_BUILD is defined. This standalone target compiles the .cpp
    # directly (no AutomationCore link) so it must own the export -- otherwise
    # MOC's staticMetaObject definition trips C2491 (definition of dllimport
    # static data member not allowed).
    target_compile_definitions(OrionMeterDelayTests PRIVATE ORION_AUTOMATION_BUILD)
    target_link_libraries(OrionMeterDelayTests PRIVATE
        Qt6::Core
        Qt6::Test
    )
    orion_add_qtest(OrionMeterDelayTests)
    # The MeterDelay production fence was retired 2026-08-07 (feature ships as an
    # opt-in customer toggle). The OrionMeterDelayFenceDevTests /
    # OrionMeterDelayFenceProdTests pair asserted the fence held and is now void;
    # both targets were removed with it.
    add_executable(OrionRemotePlayPathTests
        tests/RemotePlayExecutablePolicyTests.cpp
    )
    orion_apply_target_defaults(OrionRemotePlayPathTests)
    target_link_libraries(OrionRemotePlayPathTests PRIVATE
        OrionCommon
        Qt6::Core
        Qt6::Test
    )
    orion_add_qtest(OrionRemotePlayPathTests)
    # [ORION_INPUT_DEAD_UX 2026-08-30] Dead-input UX contracts: the shared
    # pressUndeliverable() predicate (log line == screen), the overlay severity
    # gating, and the bounded input-session auto-retry planner (backoff, clean
    # give-up, cold-vs-warm classification, press-triggered attempt throttle).
    # Header-only policy under test, so this stays a small standalone binary.
    add_executable(OrionInputRetryTests
        tests/InputSessionRetryPolicyTests.cpp
    )
    orion_apply_target_defaults(OrionInputRetryTests)
    target_link_libraries(OrionInputRetryTests PRIVATE
        OrionCommon
        Qt6::Core
        Qt6::Test
    )
    orion_add_qtest(OrionInputRetryTests)
    # [VENICE_PROFILE 2026-08-08] venice-profile.json export/import contracts
    # (src/VeniceProfile.h): deterministic round trip, load-band clamping on import,
    # and the never-exports-license allowlist snapshot. Headless (structs + QJson);
    # tests/test_venice_profile.py pins the same strings in CI and runs this binary
    # on the rig via ctest.
    add_executable(OrionVeniceProfileTests
        tests/VeniceProfileTests.cpp
    )
    orion_apply_target_defaults(OrionVeniceProfileTests)
    target_link_libraries(OrionVeniceProfileTests PRIVATE
        OrionCommon
        Qt6::Core
        Qt6::Test
    )
    orion_add_qtest(OrionVeniceProfileTests)
    add_executable(OrionPassiveCourtFlowTests
        tests/PassiveCourtFlowClassifierTests.cpp
    )
    orion_apply_target_defaults(OrionPassiveCourtFlowTests)
    target_link_libraries(OrionPassiveCourtFlowTests PRIVATE
        Qt6::Core
        Qt6::Test
    )
    orion_add_qtest(OrionPassiveCourtFlowTests)
    add_executable(OrionPreviewPresentationTests
        tests/PreviewPresentationBufferTests.cpp
    )
    orion_apply_target_defaults(OrionPreviewPresentationTests)
    target_link_libraries(OrionPreviewPresentationTests PRIVATE
        Qt6::Core
        Qt6::Gui
        Qt6::Qml
        Qt6::Quick
        Qt6::QuickControls2
        Qt6::Test
    )
    target_compile_definitions(OrionPreviewPresentationTests PRIVATE
        ORION_QML_SOURCE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/qml"
    )
    orion_add_qtest(OrionPreviewPresentationTests)
    # [ORION_ACTIVITY_FEED / PROFILE 2026-09-14] The customer-facing Activity ring
    # rule (ui_notifications::shouldEnterActivityRing, header-only) and the backend
    # `profile` block parser (SecurityCore). Standalone: neither needs the GUI
    # controller or the automation stack.
    add_executable(OrionActivityFeedPolicyTests
        tests/ActivityFeedPolicyTests.cpp
    )
    orion_apply_target_defaults(OrionActivityFeedPolicyTests)
    target_link_libraries(OrionActivityFeedPolicyTests PRIVATE
        OrionCommon
        SecurityCore
        Qt6::Core
        Qt6::Network
        Qt6::Test
    )
    orion_add_qtest(OrionActivityFeedPolicyTests)
    # [ORION_BANNER_VERDICT_LIVE 2026-09-14] The live shot-verdict tally's rules
    # (orion::ShotVerdictTally, header-only): bucketing, the 10-shot window, seq de-dupe,
    # the two resets, and THE suggestion sentence. Standalone like the policy suite above --
    # it needs no controller instance (whose ctor spawns the packet bridge and whose dtor
    # force-kills chiaki images machine-wide); the controller's Q_PROPERTY contract for the
    # same feature is pinned in MeterDelaySettingsPropertyTests.
    add_executable(OrionShotVerdictTallyTests
        tests/ShotVerdictTallyTests.cpp
    )
    orion_apply_target_defaults(OrionShotVerdictTallyTests)
    target_link_libraries(OrionShotVerdictTallyTests PRIVATE
        Qt6::Core
        Qt6::Test
    )
    orion_add_qtest(OrionShotVerdictTallyTests)
    add_executable(OrionOrderedFileLogSinkTests
        tests/OrderedFileLogSinkTests.cpp
        src/OrderedFileLogSink.cpp
        src/OrderedFileLogSink.h
    )
    orion_apply_target_defaults(OrionOrderedFileLogSinkTests)
    target_link_libraries(OrionOrderedFileLogSinkTests PRIVATE
        Qt6::Core
        Qt6::Test
    )
    orion_add_qtest(OrionOrderedFileLogSinkTests)
    add_executable(OrionInputProtocolTests
        tests/OrionInputProtocolTests.cpp
        src/OrionInputClient.cpp
    )
    orion_apply_target_defaults(OrionInputProtocolTests)
    target_link_libraries(OrionInputProtocolTests PRIVATE
        OrionCommon
        Qt6::Core
        Qt6::Test
    )
    orion_add_qtest(OrionInputProtocolTests)
    if(WIN32)
        add_executable(OrionDeepLinkTargetPolicyTests
            tests/DeepLinkTargetPolicyTests.cpp
            src/DeepLinkTargetPolicy.cpp
            src/DeepLinkTargetPolicy.h
        )
        orion_apply_target_defaults(OrionDeepLinkTargetPolicyTests)
        target_link_libraries(OrionDeepLinkTargetPolicyTests PRIVATE
            Qt6::Core
            Qt6::Test
            user32
        )
        orion_add_qtest(OrionDeepLinkTargetPolicyTests)

        add_executable(OrionShmInteropTests
            tests/SharedMemoryFrameReaderTests.cpp
        )
        orion_apply_target_defaults(OrionShmInteropTests)
        target_compile_definitions(OrionShmInteropTests PRIVATE
            ORION_REPO_ROOT="${CMAKE_CURRENT_SOURCE_DIR}/.."
        )
        target_link_libraries(OrionShmInteropTests PRIVATE
            RemotePlayCore
            Qt6::Core
            Qt6::Gui
            Qt6::Test
        )
        orion_add_qtest(OrionShmInteropTests)

        add_executable(OrionShmNotificationTests
            tests/SharedMemoryFramePumpNotificationTests.cpp
        )
        orion_apply_target_defaults(OrionShmNotificationTests)
        target_link_libraries(OrionShmNotificationTests PRIVATE
            RemotePlayCore
            Qt6::Core
            Qt6::Gui
            Qt6::Test
        )
        orion_add_qtest(OrionShmNotificationTests)

        # [ORION_PRECISE_WAIT] Wakeup-precision primitives for the precise-fire worker
        # (src/PreciseWaitTimer.h). Measures + pins the hires waitable-timer wake error and
        # reports the condvar/timer-resolution comparison distributions (see the test header).
        add_executable(OrionPreciseWaitTests
            tests/PreciseWaitTests.cpp
        )
        orion_apply_target_defaults(OrionPreciseWaitTests)
        target_link_libraries(OrionPreciseWaitTests PRIVATE
            Qt6::Core
            Qt6::Test
            winmm
        )
        orion_add_qtest(OrionPreciseWaitTests)
        # [ORION_METER_DELAY_UI 2026-08-08] Meta-object contract for the customer
        # meter-delay controls. MeterConfigPanel.qml binds orion.meterDelayEnabled /
        # meterDelayMs / meterDelayBypassOnDefense; nothing in the build fails when a
        # Q_PROPERTY goes missing (QML property resolution is runtime-only), which is
        # exactly how the "meter delay toggle doesn't work" ship-blocker shipped. This
        # target compiles the REAL OrionAppController translation unit so the
        # assertions run against the shipped staticMetaObject. The suite never
        # constructs the controller (ctor can spawn the packet bridge, dtor taskkills
        # chiaki images machine-wide); see the test file header.
        add_executable(OrionMeterDelaySettingsTests
            tests/MeterDelaySettingsPropertyTests.cpp
            src/OrionAppController.cpp
            src/OrionAppController.h
            src/OrionInputClient.cpp
            src/OrionInputClient.h
            src/OrderedFileLogSink.cpp
            src/OrderedFileLogSink.h
            # [ORION_PACKET_BRIDGE_NAMES task #64] OrionAppController.cpp consumes
            # venicenet::candidateServiceNames() for the sc.exe service-name resolution.
            venicenet/venicenet_service_route.cpp
            # [VENICENET WAVE 2B] OrionAppController.h now includes VeniceNetClient.h.
            src/VeniceNetClient.cpp
            src/VeniceNetClient.h
        )
        orion_apply_target_defaults(OrionMeterDelaySettingsTests)
        target_include_directories(OrionMeterDelaySettingsTests PRIVATE
            "${CMAKE_CURRENT_SOURCE_DIR}/venicenet")
        target_link_libraries(OrionMeterDelaySettingsTests PRIVATE
            OrionCommon
            AutomationCore
            VisionCore
            RemotePlayCore
            SecurityCore
            Qt6::Core
            Qt6::CorePrivate   # qzipwriter_p.h in OrionAppController.cpp (diagnostics bundle)
            Qt6::Gui
            Qt6::Network
            Qt6::Qml
            Qt6::Quick
            Qt6::Test
            advapi32 crypt32 user32 setupapi hid winmm xinput ws2_32 dwmapi ole32 oleaut32 strmiids iphlpapi
        )
        orion_add_qtest(OrionMeterDelaySettingsTests)
        # QTEST_MAIN creates a QGuiApplication even for non-visual tests. On a
        # clean/headless runner Qt otherwise opens a hidden platform-plugin
        # MessageBox before QTest starts, which looks like a deadlocked test and
        # leaves CTest with no useful output.
        get_filename_component(_orion_qt_plugins "${Qt6_DIR}/../../../plugins" ABSOLUTE)
        set(_orion_test_env
            "QT_QPA_PLATFORM=offscreen"
            "QT_QPA_PLATFORM_PLUGIN_PATH=${_orion_qt_plugins}/platforms"
        )
        set(_orion_test_path_mods
            "PATH=path_list_prepend:$<TARGET_FILE_DIR:Qt6::Core>"
            "PATH=path_list_prepend:$<TARGET_FILE_DIR:Qt6::Test>"
            "PATH=path_list_prepend:$<TARGET_FILE_DIR:OrionCommon>"
            "PATH=path_list_prepend:$<TARGET_FILE_DIR:AutomationCore>"
            "PATH=path_list_prepend:$<TARGET_FILE_DIR:VisionCore>"
            "PATH=path_list_prepend:$<TARGET_FILE_DIR:RemotePlayCore>"
            "PATH=path_list_prepend:$<TARGET_FILE_DIR:Qt6::Gui>"
            "PATH=path_list_prepend:$<TARGET_FILE_DIR:SecurityCore>"
        )
        if(OpenCV_FOUND AND DEFINED OpenCV_DIR)
            get_filename_component(_orion_opencv_root "${OpenCV_DIR}" ABSOLUTE)
            set(_orion_opencv_bin "${_orion_opencv_root}/x64/vc16/bin")
            if(EXISTS "${_orion_opencv_bin}")
                list(APPEND _orion_test_path_mods "PATH=path_list_prepend:${_orion_opencv_bin}")
            endif()
        endif()
        set_tests_properties(OrionNativeTests OrionFireEpochClockTests OrionMeterDelayTests OrionMeterDelaySettingsTests OrionRemotePlayPathTests OrionInputRetryTests OrionVeniceProfileTests OrionPassiveCourtFlowTests OrionPreviewPresentationTests OrionActivityFeedPolicyTests OrionShotVerdictTallyTests OrionOrderedFileLogSinkTests OrionInputProtocolTests OrionDeepLinkTargetPolicyTests OrionShmInteropTests OrionShmNotificationTests OrionPreciseWaitTests PROPERTIES
            ENVIRONMENT "${_orion_test_env}"
            ENVIRONMENT_MODIFICATION "${_orion_test_path_mods}"
        )
    endif()

    add_executable(OrionUpdaterTests
        tests/OrionUpdaterTests.cpp
    )
    orion_apply_target_defaults(OrionUpdaterTests)
    target_link_libraries(OrionUpdaterTests PRIVATE
        OrionCommon
        SecurityCore
        UpdaterCore
        Qt6::Core
        Qt6::CorePrivate
        Qt6::Test
    )
    if(WIN32)
        target_link_libraries(OrionUpdaterTests PRIVATE advapi32 crypt32 user32)
    endif()
    orion_add_qtest(OrionUpdaterTests)
    if(WIN32)
        set(_orion_updater_test_path_mods
            "PATH=path_list_prepend:$<TARGET_FILE_DIR:Qt6::Core>"
            "PATH=path_list_prepend:$<TARGET_FILE_DIR:Qt6::Test>"
            "PATH=path_list_prepend:$<TARGET_FILE_DIR:OrionCommon>"
            "PATH=path_list_prepend:$<TARGET_FILE_DIR:SecurityCore>"
            "PATH=path_list_prepend:$<TARGET_FILE_DIR:UpdaterCore>"
        )
        set_tests_properties(OrionUpdaterTests PROPERTIES
            ENVIRONMENT "${_orion_test_env}"
            ENVIRONMENT_MODIFICATION "${_orion_updater_test_path_mods}"
        )
    endif()
endif()

if(ORION_BUILD_TESTS)
    add_executable(OrionXboxPolicyTests tests/XboxRemotePlayPolicyTests.cpp)
    orion_apply_target_defaults(OrionXboxPolicyTests)
    target_link_libraries(OrionXboxPolicyTests PRIVATE OrionCommon RemotePlayCore Qt6::Core Qt6::Gui Qt6::Test)
    orion_add_qtest(OrionXboxPolicyTests)
    if(WIN32)
        set_tests_properties(OrionXboxPolicyTests PROPERTIES
            ENVIRONMENT "${_orion_test_env}"
            ENVIRONMENT_MODIFICATION "${_orion_test_path_mods}")
    endif()
endif()

if(ORION_BUILD_TESTS)
    add_executable(OrionLauncherResponseTests tests/LauncherResponsivenessTests.cpp)
    orion_apply_target_defaults(OrionLauncherResponseTests)
    target_link_libraries(OrionLauncherResponseTests PRIVATE OrionCommon RemotePlayCore Qt6::Core Qt6::Gui Qt6::Test)
    orion_add_qtest(OrionLauncherResponseTests)
    if(WIN32)
        set_tests_properties(OrionLauncherResponseTests PROPERTIES
            ENVIRONMENT "${_orion_test_env}"
            ENVIRONMENT_MODIFICATION "${_orion_test_path_mods}")
    endif()
endif()
