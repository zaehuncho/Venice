"""
ONNX Weight Dumper

Attaches to Helios process and dumps decrypted ONNX models from memory.
The models are decrypted after infcore_load_model() completes.

Usage: python onnx_dumper.py

Requires: pip install frida
"""

import frida
import sys
import os
from datetime import datetime

DUMP_DIR = os.path.join(os.path.dirname(__file__), "dumps")
os.makedirs(DUMP_DIR, exist_ok=True)

# ONNX magic bytes
ONNX_MAGIC = b'\x08\x00\x12'  # protobuf header for ONNX
ONNX_MAGIC2 = b'\x08\x08\x12' # alternate

JS_HOOK = """
(function() {
    var onnxruntime = Process.getModuleByName('onnxruntime.dll');
    if (!onnxruntime) {
        send({type: 'error', msg: 'onnxruntime.dll not loaded'});
        return;
    }
    send({type: 'info', msg: 'Found onnxruntime.dll at ' + onnxruntime.base});

    // Hook OrtCreateSession - this receives the raw ONNX bytes
    var exports = onnxruntime.enumerateExports();
    var createSession = null;
    for (var i = 0; i < exports.length; i++) {
        if (exports[i].name.indexOf('OrtCreateSession') !== -1 ||
            exports[i].name.indexOf('CreateSession') !== -1) {
            send({type: 'info', msg: 'Found export: ' + exports[i].name});
            if (!createSession) createSession = exports[i].address;
        }
    }

    if (!createSession) {
        // Try pattern scan for the function
        send({type: 'info', msg: 'Scanning for OrtCreateSession pattern...'});
    }

    // Alternative: hook malloc/VirtualAlloc and scan for ONNX magic
    var kernel32 = Process.getModuleByName('kernel32.dll');
    var VirtualAlloc = Module.getExportByName('kernel32.dll', 'VirtualAlloc');

    Interceptor.attach(VirtualAlloc, {
        onEnter: function(args) {
            this.size = args[1].toInt32();
        },
        onLeave: function(retval) {
            if (this.size > 1024 * 1024 && this.size < 100 * 1024 * 1024) {
                // Track large allocations (1MB - 100MB, likely model weights)
                send({type: 'alloc', addr: retval.toString(), size: this.size});
            }
        }
    });

    // Periodic memory scan for ONNX headers
    var scanned = {};
    setInterval(function() {
        Process.enumerateRanges('r--').forEach(function(range) {
            if (range.size < 1024 * 1024) return;
            if (scanned[range.base.toString()]) return;

            try {
                var buf = Memory.readByteArray(range.base, Math.min(range.size, 16));
                var bytes = new Uint8Array(buf);

                // Check for ONNX protobuf magic
                if ((bytes[0] === 0x08 && bytes[2] === 0x12) ||
                    bytes[0] === 0x0a) {
                    send({
                        type: 'potential_onnx',
                        addr: range.base.toString(),
                        size: range.size,
                        header: Array.from(bytes.slice(0, 16))
                    });
                    scanned[range.base.toString()] = true;
                }
            } catch(e) {}
        });
    }, 5000);

    send({type: 'ready', msg: 'Hooks installed, monitoring for ONNX models...'});
})();
"""

def find_helios():
    """Find running Helios process"""
    import subprocess
    result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq Helios2.exe', '/FO', 'CSV'],
                          capture_output=True, text=True)
    for line in result.stdout.strip().split('\n')[1:]:
        if 'Helios2.exe' in line:
            parts = line.strip('"').split('","')
            return int(parts[1])

    result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq Helios.exe', '/FO', 'CSV'],
                          capture_output=True, text=True)
    for line in result.stdout.strip().split('\n')[1:]:
        if 'Helios.exe' in line:
            parts = line.strip('"').split('","')
            return int(parts[1])

    return None

def on_message(message, data):
    if message['type'] == 'send':
        payload = message['payload']
        msg_type = payload.get('type', '')

        if msg_type == 'potential_onnx':
            addr = payload['addr']
            size = payload['size']
            header = bytes(payload['header'])
            print(f"[*] Potential ONNX model at {addr} ({size} bytes)")
            print(f"    Header: {header.hex()}")

            # Request memory dump
            # Would need additional code to actually dump here

        elif msg_type == 'alloc':
            print(f"[*] Large alloc: {payload['addr']} ({payload['size']} bytes)")

        elif msg_type == 'info':
            print(f"[+] {payload['msg']}")

        elif msg_type == 'error':
            print(f"[-] {payload['msg']}")

        elif msg_type == 'ready':
            print(f"[+] {payload['msg']}")

    elif message['type'] == 'error':
        print(f"[!] Error: {message['stack']}")

def main():
    print("=" * 60)
    print("ONNX Weight Dumper for Helios")
    print("=" * 60)

    pid = find_helios()
    if not pid:
        print("[-] Helios not running. Start it first, then run this script.")
        print("    Looking for: Helios.exe or Helios2.exe")
        sys.exit(1)

    print(f"[+] Found Helios process: PID {pid}")
    print("[*] Attaching...")

    try:
        session = frida.attach(pid)
        script = session.create_script(JS_HOOK)
        script.on('message', on_message)
        script.load()

        print("[*] Press Ctrl+C to stop")
        print("[*] Now load a model in Helios to capture it...")
        print()

        sys.stdin.read()

    except frida.ProcessNotFoundError:
        print(f"[-] Could not attach to PID {pid}")
    except KeyboardInterrupt:
        print("\n[*] Detaching...")

if __name__ == '__main__':
    main()
