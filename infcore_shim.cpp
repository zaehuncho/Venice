/*
    InferenceCore.dll Auth Bypass Shim

    Compile: cl /LD /O2 infcore_shim.cpp /link /DEF:infcore_shim.def /OUT:InferenceCore.dll

    Place in: <HeliosII>/versions/<ver>/lib/InferenceCore.dll (backup original first)

    This shim intercepts auth calls and bypasses validation while forwarding
    all other calls to the real InferenceCore (renamed to InferenceCore_orig.dll)
*/

#include <windows.h>
#include <cstdint>
#include <cstring>

static HMODULE g_real = nullptr;
static bool g_dumped = false;

// Function pointer types for all exports
#define INFCORE_FUNCS(X) \
    X(int, infcore_init, (void)) \
    X(void, infcore_shutdown, (void)) \
    X(void, infcore_cleanup_build, (void)) \
    X(void, infcore_set_session, (const char*, const char*, const char*, const char*)) \
    X(int, infcore_get_last_error, (void)) \
    X(int, infcore_get_debug_log, (char*, int)) \
    X(int, infcore_add_dll_path, (const char*)) \
    X(int, infcore_has_tensorrt, (void)) \
    X(int, infcore_get_gpu_count, (void)) \
    X(int, infcore_get_gpu_name, (int, char*, int)) \
    X(int, infcore_set_build_gpu, (int)) \
    X(int, infcore_list_models, (char*, int)) \
    X(void*, infcore_create_engine, (const char*)) \
    X(int, infcore_start_inference, (void*, const char*, uint32_t)) \
    X(void, infcore_stop_inference, (void*)) \
    X(void, infcore_pause_engine, (void*)) \
    X(void, infcore_resume_engine, (void*)) \
    X(int, infcore_is_paused, (void*)) \
    X(void, infcore_destroy_engine, (void*)) \
    X(int, infcore_load_model, (void*, const char*)) \
    X(int, infcore_unload_model, (void*))

// Declare function pointers
#define DECLARE_FN(ret, name, args) static ret (*p_##name) args = nullptr;
INFCORE_FUNCS(DECLARE_FN)
#undef DECLARE_FN

// Load real DLL and resolve functions
static bool LoadReal() {
    if (g_real) return true;

    char path[MAX_PATH];
    GetModuleFileNameA(nullptr, path, MAX_PATH);
    char* slash = strrchr(path, '\\');
    if (slash) strcpy(slash + 1, "InferenceCore_orig.dll");

    g_real = LoadLibraryA(path);
    if (!g_real) return false;

    #define LOAD_FN(ret, name, args) \
        p_##name = (ret (*) args)GetProcAddress(g_real, #name);
    INFCORE_FUNCS(LOAD_FN)
    #undef LOAD_FN

    return true;
}

// Hook for dumping decrypted models
static void TryDumpModel(void* engine, const char* uuid) {
    if (g_dumped) return;

    // After load_model succeeds, the decrypted ONNX is in memory
    // We could scan for ONNX magic bytes or hook onnxruntime
    // For now just log success
    char logpath[MAX_PATH];
    GetTempPathA(MAX_PATH, logpath);
    strcat(logpath, "infcore_shim.log");

    FILE* f = fopen(logpath, "a");
    if (f) {
        fprintf(f, "[SHIM] Model loaded: %s\n", uuid ? uuid : "(null)");
        fclose(f);
    }
}

extern "C" {

// ========== AUTH BYPASS ==========

__declspec(dllexport) void infcore_set_session(const char* token, const char* user, const char* global, const char* avatar) {
    LoadReal();
    // Call real function but we don't care if it fails
    if (p_infcore_set_session) p_infcore_set_session(token, user, global, avatar);
    // Auth always succeeds from our perspective
}

__declspec(dllexport) int infcore_load_model(void* engine, const char* uuid) {
    LoadReal();
    if (!p_infcore_load_model) return 0; // INFCORE_OK

    int result = p_infcore_load_model(engine, uuid);

    // If real load succeeded, try to dump
    if (result == 0) {
        TryDumpModel(engine, uuid);
    }

    return result;
}

// ========== PASSTHROUGH ==========

__declspec(dllexport) int infcore_init(void) {
    LoadReal();
    return p_infcore_init ? p_infcore_init() : 0;
}

__declspec(dllexport) void infcore_shutdown(void) {
    if (p_infcore_shutdown) p_infcore_shutdown();
}

__declspec(dllexport) void infcore_cleanup_build(void) {
    if (p_infcore_cleanup_build) p_infcore_cleanup_build();
}

__declspec(dllexport) int infcore_get_last_error(void) {
    return p_infcore_get_last_error ? p_infcore_get_last_error() : 0;
}

__declspec(dllexport) int infcore_get_debug_log(char* buf, int sz) {
    return p_infcore_get_debug_log ? p_infcore_get_debug_log(buf, sz) : 0;
}

__declspec(dllexport) int infcore_add_dll_path(const char* path) {
    LoadReal();
    return p_infcore_add_dll_path ? p_infcore_add_dll_path(path) : 0;
}

__declspec(dllexport) int infcore_has_tensorrt(void) {
    LoadReal();
    return p_infcore_has_tensorrt ? p_infcore_has_tensorrt() : 0;
}

__declspec(dllexport) int infcore_get_gpu_count(void) {
    LoadReal();
    return p_infcore_get_gpu_count ? p_infcore_get_gpu_count() : 0;
}

__declspec(dllexport) int infcore_get_gpu_name(int idx, char* buf, int sz) {
    LoadReal();
    return p_infcore_get_gpu_name ? p_infcore_get_gpu_name(idx, buf, sz) : 0;
}

__declspec(dllexport) int infcore_set_build_gpu(int idx) {
    LoadReal();
    return p_infcore_set_build_gpu ? p_infcore_set_build_gpu(idx) : 0;
}

__declspec(dllexport) int infcore_list_models(char* buf, int sz) {
    LoadReal();
    return p_infcore_list_models ? p_infcore_list_models(buf, sz) : 0;
}

__declspec(dllexport) void* infcore_create_engine(const char* uuid) {
    LoadReal();
    return p_infcore_create_engine ? p_infcore_create_engine(uuid) : nullptr;
}

__declspec(dllexport) int infcore_start_inference(void* e, const char* rb, uint32_t pid) {
    LoadReal();
    return p_infcore_start_inference ? p_infcore_start_inference(e, rb, pid) : 0;
}

__declspec(dllexport) void infcore_stop_inference(void* e) {
    if (p_infcore_stop_inference) p_infcore_stop_inference(e);
}

__declspec(dllexport) void infcore_pause_engine(void* e) {
    if (p_infcore_pause_engine) p_infcore_pause_engine(e);
}

__declspec(dllexport) void infcore_resume_engine(void* e) {
    if (p_infcore_resume_engine) p_infcore_resume_engine(e);
}

__declspec(dllexport) int infcore_is_paused(void* e) {
    return p_infcore_is_paused ? p_infcore_is_paused(e) : 0;
}

__declspec(dllexport) void infcore_destroy_engine(void* e) {
    if (p_infcore_destroy_engine) p_infcore_destroy_engine(e);
}

__declspec(dllexport) int infcore_unload_model(void* e) {
    return p_infcore_unload_model ? p_infcore_unload_model(e) : 0;
}

} // extern "C"

BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD fdwReason, LPVOID lpvReserved) {
    if (fdwReason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hinstDLL);
    }
    return TRUE;
}
