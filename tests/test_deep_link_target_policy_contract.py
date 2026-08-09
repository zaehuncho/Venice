from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "native_orion" / "src" / "main.cpp"
POLICY = ROOT / "native_orion" / "src" / "DeepLinkTargetPolicy.cpp"


def _forwarding_body() -> str:
    source = MAIN.read_text(encoding="utf-8")
    start = source.index("bool forwardDeepLinkToRunningInstance")
    end = source.index("// Receives WM_COPYDATA", start)
    return source[start:end]


def test_internal_deep_link_identity_contracts_remain_stable():
    source = MAIN.read_text(encoding="utf-8")

    assert 'kOrionDeepLinkCopyDataId = 0x4F52444C' in source
    assert 'QStringLiteral("HKEY_CURRENT_USER\\\\Software\\\\Classes\\\\orion")' in source
    assert 'QStringLiteral("orion://")' in source
    assert 'L"Local\\\\OrionNativeLauncher"' in source
    # The protocol scheme, registry class, CopyData id and launcher mutex above are
    # INTERNAL identity and stay "orion" forever. The lookup TITLE is different in
    # kind: it is the customer-visible taskbar/Alt-Tab label, retitled to Venice on
    # 2026-08-06 because the window was still announcing the pre-rebrand name. It
    # must track qml/Main.qml's `title:` exactly — see the note there.
    assert 'FindWindowW(nullptr, L"Venice")' in source


def test_deep_link_send_is_gated_by_verified_exact_executable_owner():
    body = _forwarding_body()

    # The title is only a LOCATOR and is never proof of process identity — which is
    # what the ordering assertion below actually pins, and why retitling the window
    # to Venice (2026-08-06) is safe: any other process that titles a window "Venice"
    # is still rejected by the ownership gate before the secret-bearing URI is sent.
    assert 'FindWindowW(nullptr, L"Venice")' in body
    ownership_gate = body.index("windowOwnedByExecutable")
    current_executable = body.index("QCoreApplication::applicationFilePath()")
    final_pid_check = body.index("finalProcessId")
    secret_send = body.index("SendMessageW")

    assert ownership_gate < current_executable < final_pid_check < secret_send
    assert "finalProcessId != verifiedProcessId" in body[:secret_send]
    assert "return false;" in body[final_pid_check:secret_send]


def test_target_policy_uses_windows_process_and_canonical_path_apis():
    source = POLICY.read_text(encoding="utf-8")

    for required_api in (
        "GetWindowThreadProcessId",
        "OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION",
        "QueryFullProcessImageNameW",
        "CreateFileW",
        "GetFinalPathNameByHandleW",
        "CompareStringOrdinal",
    ):
        assert required_api in source

    # The final comparison must preserve Windows' case-insensitive semantics.
    assert "CompareStringOrdinal(" in source
    assert "TRUE)" in source
