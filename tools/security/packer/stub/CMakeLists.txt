cmake_minimum_required(VERSION 3.20)
project(orion_stub C ASM_MASM)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

add_library(orion_stub_x64 SHARED
    src/stub_main.c
    src/pe_loader.c
    src/crypto.c
    src/key_scatter.c
    src/tls_anchor.c
    src/stubrt.c
    src/antidebug.c
    src/antidump.c
    src/memguard.c
    src/venice_vm.c
    src/venice_trampoline.asm
    src/miniz.c
    src/chkstk.asm
    # Decoy imports (user32/advapi32/ole32/shell32/gdi32/shlwapi): never called,
    # pulls those DLLs into the IAT so the packed binary's import graph looks
    # like a normal Windows app instead of a bare packer stub. See file header.
    src/stub_junk_imports.c
)

target_include_directories(orion_stub_x64 PRIVATE src)
target_link_libraries(orion_stub_x64 PRIVATE kernel32)

# Freestanding: no CRT, no default libs, no DLL entry point.
target_link_options(orion_stub_x64 PRIVATE
    /NODEFAULTLIB
    /NOENTRY
    /DYNAMICBASE
    /NXCOMPAT
    /HIGHENTROPYVA
)

# Vendored miniz: strip to inflate-only (no stdio/time/archive/zip writer),
# lower warning level (we don't own this code).
set_source_files_properties(src/miniz.c PROPERTIES
    COMPILE_OPTIONS "/W3;/WX-"
    COMPILE_DEFINITIONS "MINIZ_NO_STDIO;MINIZ_NO_TIME;MINIZ_NO_DEFLATE_APIS;MINIZ_NO_ARCHIVE_APIS;MINIZ_NO_ZLIB_COMPATIBLE_NAMES"
)

# Key-binding integrity: the builder hashes .text to derive the AES key.
# /guard:cf inserts extra call-site checks that change .text content and
# require relocations inside .text, breaking both the hash and the
# reloc-free .text assertion. Deliberately disabled.
# Apply C compiler options to .c files only (not MASM .asm files).
file(GLOB _C_SOURCES src/*.c)
if(CMAKE_C_COMPILER_ID STREQUAL "Clang")
    set_source_files_properties(${_C_SOURCES} PROPERTIES
        COMPILE_OPTIONS "/W4;/GS-;/Oi;/O2;-fno-builtin;-Wno-missing-field-initializers;-Wno-missing-braces;-Wno-unused-but-set-variable;-Wno-unknown-pragmas;-Wno-ignored-pragma-intrinsic;-Wno-inconsistent-dllimport"
    )
else()
    set_source_files_properties(${_C_SOURCES} PROPERTIES
        COMPILE_OPTIONS "/W4;/WX;/GS-;/Oi;/O2"
    )
endif()

# Strip /guard:cf that CMake may inject via CMAKE_C_FLAGS or toolchain files.
foreach(_flag CMAKE_C_FLAGS CMAKE_C_FLAGS_RELEASE CMAKE_C_FLAGS_RELWITHDEBINFO)
    string(REPLACE "/guard:cf" "" ${_flag} "${${_flag}}")
endforeach()
foreach(_flag CMAKE_SHARED_LINKER_FLAGS CMAKE_SHARED_LINKER_FLAGS_RELEASE CMAKE_SHARED_LINKER_FLAGS_RELWITHDEBINFO)
    string(REPLACE "/guard:cf" "" ${_flag} "${${_flag}}")
endforeach()

option(VVM_SHUFFLE_OPCODES "Enable per-build opcode shuffling for Venice VM" ON)
if(VVM_SHUFFLE_OPCODES)
    find_package(Python3 REQUIRED COMPONENTS Interpreter)
    set(_vvm_venice_dir  "${CMAKE_CURRENT_SOURCE_DIR}/../venice")
    set(_vvm_shuffler    "${_vvm_venice_dir}/shuffle_opcodes.py")
    set(_vvm_progen      "${_vvm_venice_dir}/generate_programs.py")
    set(_vvm_asm         "${_vvm_venice_dir}/venice_asm.py")
    set(_vvm_programs_dir "${_vvm_venice_dir}/programs")
    set(_vvm_shuffled_h  "${CMAKE_CURRENT_BINARY_DIR}/venice_opcodes_shuffled.h")
    set(_vvm_shuffled_py "${_vvm_venice_dir}/venice_opcodes_shuffled.py")
    set(_vvm_programs_h  "${CMAKE_CURRENT_BINARY_DIR}/venice_programs_shuffled.h")

    # Step 1: mint a fresh per-build opcode permutation.
    add_custom_command(
        OUTPUT "${_vvm_shuffled_h}" "${_vvm_shuffled_py}"
        COMMAND ${Python3_EXECUTABLE} "${_vvm_shuffler}" generate
                --c-header "${_vvm_shuffled_h}"
                --py-dict "${_vvm_shuffled_py}"
        COMMENT "Shuffling Venice VM opcodes (per-build randomization)"
        DEPENDS "${_vvm_shuffler}"
        VERBATIM
    )

    # Step 2: re-assemble the .vasm sources against the shuffled map so the
    # bytecode blobs in venice_programs.h match the interpreter's decode.
    # Without this the interpreter would misdispatch canonical bytecode and
    # every packed binary would fail crypto_derive_key. All .vasm files are
    # DEPENDS so a source edit forces a rebuild.
    file(GLOB _vvm_vasm_sources "${_vvm_programs_dir}/*.vasm")
    add_custom_command(
        OUTPUT "${_vvm_programs_h}"
        COMMAND ${Python3_EXECUTABLE} "${_vvm_progen}"
                --shuffled-map "${_vvm_shuffled_py}"
                --programs-dir "${_vvm_programs_dir}"
                --output       "${_vvm_programs_h}"
        COMMENT "Assembling Venice VM programs with shuffled opcodes"
        DEPENDS "${_vvm_progen}" "${_vvm_asm}" "${_vvm_shuffled_py}"
                ${_vvm_vasm_sources}
        VERBATIM
    )

    add_custom_target(venice_shuffle
        DEPENDS "${_vvm_shuffled_h}" "${_vvm_programs_h}")
    add_dependencies(orion_stub_x64 venice_shuffle)
    target_include_directories(orion_stub_x64 PRIVATE "${CMAKE_CURRENT_BINARY_DIR}")
    target_compile_definitions(orion_stub_x64 PRIVATE VVM_SHUFFLED)
endif()

# Diagnostic tracing (writes ornpk_diag.txt with per-step markers). Enable with
# -DORNPK_ENABLE_DIAG=ON when hunting a stub-loader regression; OFF by default
# so shipped builds don't leak side-channel telemetry to disk or announce a
# packer stub to an analyst who spots the file appearing.
option(ORNPK_ENABLE_DIAG "Emit ornpk_diag.txt trace from pe_loader (debug only)" OFF)
if(ORNPK_ENABLE_DIAG)
    target_compile_definitions(orion_stub_x64 PRIVATE ORNPK_DIAG)
endif()

# Prebuilt output: copy the DLL to stub/prebuilt/ after build.
set(PREBUILT_DIR "${CMAKE_CURRENT_SOURCE_DIR}/prebuilt")
add_custom_command(TARGET orion_stub_x64 POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E make_directory "${PREBUILT_DIR}"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "$<TARGET_FILE:orion_stub_x64>"
        "${PREBUILT_DIR}/orion_stub_x64.dll"
    COMMENT "Copying stub to prebuilt/"
)
