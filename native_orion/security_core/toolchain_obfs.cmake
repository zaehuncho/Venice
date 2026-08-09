# ============================================================================
# OLLVM obfuscation toolchain for SecurityCore
# ============================================================================
# Points CMake at a local OLLVM fork of clang-cl for the obfuscation build.
# The paths below are placeholders -- install an OLLVM fork (e.g.
# heroims/obfuscator or AkiraFukami/obfuscator) to C:/llvm-obfs and adjust
# these paths to match your installation.
# ============================================================================

set(CMAKE_C_COMPILER   "D:/llvm-obfs/bin/clang-cl.exe" CACHE FILEPATH "")
set(CMAKE_CXX_COMPILER "D:/llvm-obfs/bin/clang-cl.exe" CACHE FILEPATH "")
set(CMAKE_LINKER        "D:/llvm-obfs/bin/lld-link.exe" CACHE FILEPATH "")

# OLLVM obfuscation passes applied to every TU in the sub-project:
#   -fla   Control Flow Flattening  -- replaces structured control flow with a
#          switch-dispatch loop, defeating static CFG analysis.
#   -bcf   Bogus Control Flow       -- injects opaque predicates and dead-code
#          blocks to bloat and confuse the CFG.
#   -sub   Instruction Substitution -- replaces arithmetic / logic instructions
#          with semantically equivalent but harder-to-read sequences.
add_compile_options(-mllvm -fla -mllvm -bcf -mllvm -sub)
