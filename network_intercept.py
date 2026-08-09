"""
Network Intercept for Helios Model Key Extraction

Intercepts the model decryption key request to inputsense.com
and saves the response for offline decryption.

Methods:
1. mitmproxy - Full HTTPS interception
2. Frida hook - Hook WinHTTP calls directly
3. DNS redirect - Point to local server

This script implements method 2 (Frida) as it requires no cert installation.

Usage:
    python network_intercept.py

While running, load a model in Helios. The script will capture:
- The model UUID being requested
- The session token used for auth
- The server's response containing the decryption key

Requirements:
    pip install frida mitmproxy
"""

import frida
import sys
import os
import json
from datetime import datetime

CAPTURE_DIR = os.path.join(os.path.dirname(__file__), "captures")
os.makedirs(CAPTURE_DIR, exist_ok=True)

JS_HOOK = """
(function() {
    // Hook WinHTTP functions to intercept all HTTP traffic

    var winhttp = Process.getModuleByName('WINHTTP.dll');
    if (!winhttp) {
        send({type: 'error', msg: 'WINHTTP.dll not loaded'});
        return;
    }

    var pendingRequests = {};
    var requestId = 0;

    // WinHttpOpenRequest
    var WinHttpOpenRequest = Module.getExportByName('WINHTTP.dll', 'WinHttpOpenRequest');
    Interceptor.attach(WinHttpOpenRequest, {
        onEnter: function(args) {
            this.verb = args[1].readUtf16String();
            this.path = args[2].readUtf16String();
        },
        onLeave: function(retval) {
            if (retval.isNull()) return;

            var id = requestId++;
            pendingRequests[retval.toString()] = {
                id: id,
                verb: this.verb,
                path: this.path,
                headers: [],
                requestBody: null,
                responseBody: null
            };

            if (this.path && this.path.indexOf('inputsense') !== -1) {
                send({
                    type: 'request_start',
                    id: id,
                    verb: this.verb,
                    path: this.path
                });
            }
        }
    });

    // WinHttpAddRequestHeaders
    var WinHttpAddRequestHeaders = Module.getExportByName('WINHTTP.dll', 'WinHttpAddRequestHeaders');
    Interceptor.attach(WinHttpAddRequestHeaders, {
        onEnter: function(args) {
            var handle = args[0].toString();
            var headers = args[1].readUtf16String();

            if (pendingRequests[handle] && headers) {
                pendingRequests[handle].headers.push(headers);

                // Check for auth header
                if (headers.indexOf('Authorization') !== -1) {
                    send({
                        type: 'auth_header',
                        id: pendingRequests[handle].id,
                        headers: headers
                    });
                }
            }
        }
    });

    // WinHttpSendRequest
    var WinHttpSendRequest = Module.getExportByName('WINHTTP.dll', 'WinHttpSendRequest');
    Interceptor.attach(WinHttpSendRequest, {
        onEnter: function(args) {
            var handle = args[0].toString();
            var bodyLen = args[5].toInt32();

            if (pendingRequests[handle] && bodyLen > 0) {
                try {
                    var body = args[4].readUtf8String(bodyLen);
                    pendingRequests[handle].requestBody = body;
                    send({
                        type: 'request_body',
                        id: pendingRequests[handle].id,
                        body: body
                    });
                } catch(e) {}
            }
        }
    });

    // WinHttpReadData - capture response
    var WinHttpReadData = Module.getExportByName('WINHTTP.dll', 'WinHttpReadData');
    Interceptor.attach(WinHttpReadData, {
        onEnter: function(args) {
            this.handle = args[0].toString();
            this.buffer = args[1];
            this.bytesToRead = args[2].toInt32();
            this.bytesRead = args[3];
        },
        onLeave: function(retval) {
            if (!retval.toInt32()) return;

            var handle = this.handle;
            var bytesRead = this.bytesRead.readU32();

            if (pendingRequests[handle] && bytesRead > 0) {
                try {
                    var data = this.buffer.readUtf8String(bytesRead);

                    if (!pendingRequests[handle].responseBody) {
                        pendingRequests[handle].responseBody = '';
                    }
                    pendingRequests[handle].responseBody += data;

                    // Check if this looks like a key response
                    if (data.indexOf('key') !== -1 || data.indexOf('grant') !== -1) {
                        send({
                            type: 'key_response',
                            id: pendingRequests[handle].id,
                            path: pendingRequests[handle].path,
                            data: data
                        });
                    }
                } catch(e) {}
            }
        }
    });

    // Also hook bcrypt for key material
    try {
        var bcrypt = Process.getModuleByName('bcrypt.dll');
        var BCryptImportKeyPair = Module.getExportByName('bcrypt.dll', 'BCryptImportKeyPair');

        if (BCryptImportKeyPair) {
            Interceptor.attach(BCryptImportKeyPair, {
                onEnter: function(args) {
                    var blobType = args[2].readUtf16String();
                    var blobLen = args[4].toInt32();

                    if (blobLen > 0 && blobLen < 1024) {
                        try {
                            var blob = args[3].readByteArray(blobLen);
                            send({
                                type: 'key_import',
                                blobType: blobType,
                                blobLen: blobLen,
                                blob: Array.from(new Uint8Array(blob))
                            });
                        } catch(e) {}
                    }
                }
            });
        }
    } catch(e) {}

    send({type: 'ready', msg: 'Network hooks installed'});
})();
"""


def find_helios():
    """Find running Helios process"""
    import subprocess
    for proc_name in ['Helios2.exe', 'Helios.exe', 'InferenceCore.dll']:
        result = subprocess.run(
            ['tasklist', '/FI', f'IMAGENAME eq {proc_name}', '/FO', 'CSV'],
            capture_output=True, text=True
        )
        for line in result.stdout.strip().split('\n')[1:]:
            if proc_name in line:
                parts = line.strip('"').split('","')
                return int(parts[1])
    return None


def on_message(message, data):
    if message['type'] == 'send':
        payload = message['payload']
        msg_type = payload.get('type', '')

        if msg_type == 'request_start':
            print(f"\n[>] {payload['verb']} {payload['path']}")

        elif msg_type == 'auth_header':
            print(f"    [AUTH] {payload['headers'][:100]}...")

            # Save auth token
            save_capture('auth_token', payload)

        elif msg_type == 'request_body':
            print(f"    [BODY] {payload['body'][:200]}...")
            save_capture('request_body', payload)

        elif msg_type == 'key_response':
            print(f"\n[!] KEY RESPONSE CAPTURED!")
            print(f"    Path: {payload['path']}")
            print(f"    Data: {payload['data'][:500]}...")

            # Save key response
            save_capture('key_response', payload)

        elif msg_type == 'key_import':
            print(f"\n[!] CRYPTO KEY IMPORT")
            print(f"    Type: {payload['blobType']}")
            print(f"    Size: {payload['blobLen']} bytes")

            save_capture('key_import', payload)

        elif msg_type == 'ready':
            print(f"[+] {payload['msg']}")

        elif msg_type == 'error':
            print(f"[-] {payload['msg']}")

    elif message['type'] == 'error':
        print(f"[!] Error: {message.get('stack', message)}")


def save_capture(name, data):
    """Save captured data to file"""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = os.path.join(CAPTURE_DIR, f"{name}_{timestamp}.json")

    with open(filename, 'w') as f:
        json.dump(data, f, indent=2)

    print(f"    [SAVED] {filename}")


def main():
    print("=" * 60)
    print("Helios Network Intercept - Model Key Capture")
    print("=" * 60)
    print()
    print("This will capture:")
    print("  - Session tokens (Authorization headers)")
    print("  - Model decryption key responses")
    print("  - Crypto key imports")
    print()
    print(f"Captures saved to: {CAPTURE_DIR}")
    print()

    pid = find_helios()
    if not pid:
        print("[-] Helios not running.")
        print("    Start Helios, then run this script.")
        print("    Looking for: Helios.exe or Helios2.exe")
        sys.exit(1)

    print(f"[+] Found Helios process: PID {pid}")
    print("[*] Attaching...")

    try:
        session = frida.attach(pid)
        script = session.create_script(JS_HOOK)
        script.on('message', on_message)
        script.load()

        print()
        print("[*] Hooks active. Now load a model in Helios...")
        print("[*] Press Ctrl+C to stop")
        print()

        sys.stdin.read()

    except frida.ProcessNotFoundError:
        print(f"[-] Could not attach to PID {pid}")
    except KeyboardInterrupt:
        print("\n[*] Stopping...")


if __name__ == '__main__':
    main()
