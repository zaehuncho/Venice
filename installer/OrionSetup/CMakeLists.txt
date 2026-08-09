cmake_minimum_required(VERSION 3.24)

project(OrionSetup
    VERSION 1.0.0
    LANGUAGES CXX
)

# Standalone subproject. Configure with:
#   cmake -S . -B build -G "Visual Studio 17 2022" -A x64 \
#         -DCMAKE_PREFIX_PATH=C:/Qt/6.8.0/msvc2022_64
#   cmake --build build --config Release
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_AUTOMOC ON)
set(CMAKE_AUTORCC ON)

find_package(Qt6 6.5 REQUIRED COMPONENTS Core Gui Qml Quick Network)

qt_standard_project_setup(REQUIRES 6.5)

# WIN32 GUI executable (no console window).
qt_add_executable(OrionSetup WIN32
    src/main.cpp
    src/InstallerController.cpp
    src/InstallerController.h
    resources.qrc
)

# The QML front-end. URI must match engine.loadFromModule("OrionSetup", "Main").
set_source_files_properties(qml/Theme.qml PROPERTIES QT_QML_SINGLETON_TYPE TRUE)
qt_add_qml_module(OrionSetup
    URI OrionSetup
    VERSION 1.0
    QML_FILES
        qml/Main.qml
        qml/Theme.qml
        qml/WelcomeScreen.qml
        qml/ProgressScreen.qml
        qml/FinishScreen.qml
        qml/ErrorScreen.qml
        qml/ShotMeter.qml
        qml/ComponentRow.qml
        qml/PrimaryButton.qml
        qml/GhostButton.qml
        qml/Chip.qml
        qml/WinButton.qml
        qml/TipMark.qml
        qml/CheckMark.qml
        qml/ShieldMark.qml
)

target_link_libraries(OrionSetup PRIVATE
    Qt6::Core
    Qt6::Gui
    Qt6::Qml
    Qt6::Quick
    Qt6::Network
)

if(WIN32)
    # advapi32: QSettings registry (uninstaller entry). shell32/ole32: shortcuts.
    # wintrust/crypt32: Authenticode verification (WinVerifyTrust) of the driver
    # installers and of OrionSetup.exe's own signature.
    target_link_libraries(OrionSetup PRIVATE
        advapi32 shell32 ole32 user32 wintrust crypt32)

    # Opt-in hard-fail on unverified components. Uncomment for a real release
    # build (or set "production": true in components.json) so placeholder SHA-256
    # hashes abort the install instead of being skipped:
    #   target_compile_definitions(OrionSetup PRIVATE ORION_REQUIRE_VERIFIED)

    # Version info + embedded UAC/DPI manifest. /MANIFEST:NO stops the linker
    # from generating a competing default manifest.
    target_sources(OrionSetup PRIVATE OrionSetup.rc)
    target_link_options(OrionSetup PRIVATE "/MANIFEST:NO")

    set_target_properties(OrionSetup PROPERTIES WIN32_EXECUTABLE TRUE)
endif()

# Ship components.json next to the exe so operators can edit URLs/hashes without
# rebuilding. (It is also embedded as a resource fallback via resources.qrc.)
add_custom_command(TARGET OrionSetup POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${CMAKE_CURRENT_SOURCE_DIR}/components.json"
            "$<TARGET_FILE_DIR:OrionSetup>/components.json"
    COMMENT "Copying components.json next to OrionSetup.exe"
    VERBATIM
)
