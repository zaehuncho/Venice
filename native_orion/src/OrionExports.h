#pragma once

#if defined(_WIN32) || defined(__CYGWIN__)
#define ORION_DECL_EXPORT __declspec(dllexport)
#define ORION_DECL_IMPORT __declspec(dllimport)
#else
#define ORION_DECL_EXPORT __attribute__((visibility("default")))
#define ORION_DECL_IMPORT __attribute__((visibility("default")))
#endif

#if defined(ORION_COMMON_BUILD)
#define ORION_COMMON_API ORION_DECL_EXPORT
#else
#define ORION_COMMON_API ORION_DECL_IMPORT
#endif

#if defined(ORION_AUTOMATION_BUILD)
#define ORION_AUTOMATION_API ORION_DECL_EXPORT
#else
#define ORION_AUTOMATION_API ORION_DECL_IMPORT
#endif

#if defined(ORION_VISION_BUILD)
#define ORION_VISION_API ORION_DECL_EXPORT
#else
#define ORION_VISION_API ORION_DECL_IMPORT
#endif

#if defined(ORION_REMOTEPLAY_BUILD)
#define ORION_REMOTEPLAY_API ORION_DECL_EXPORT
#else
#define ORION_REMOTEPLAY_API ORION_DECL_IMPORT
#endif

#if defined(ORION_SECURITY_BUILD)
#define ORION_SECURITY_API ORION_DECL_EXPORT
#else
#define ORION_SECURITY_API ORION_DECL_IMPORT
#endif

#if defined(ORION_UPDATER_BUILD)
#define ORION_UPDATER_API ORION_DECL_EXPORT
#else
#define ORION_UPDATER_API ORION_DECL_IMPORT
#endif
