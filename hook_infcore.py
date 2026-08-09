"""Hook InferenceCore.dll to intercept model loading and capture decryption"""
import frida
import subprocess
import time
import sys
import os

OUTPUT_DIR = r"C:\Users\aaron\Desktop\EXPLOITS\helios_bypass\models"
os.makedirs(OUTPUT_DIR, exist_ok=True)

def find_helios():
    result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq Helios2.exe', '/FO', 'CSV'],
                          capture_output=True, text=True)
    for line in result.stdout.strip().split('\n')[1:]:
        if 'Helios2' in line:
            return int(line.strip('"').split('","')[1])
    return None

HOOK_SCRIPT = r'''
(function() {
    send({type: 'init'});

    var infcore = Process.findModuleByName('InferenceCore.dll');
    if (!infcore) {
        send({type: 'error', msg: 'InferenceCore.dll not found'});
        return;
    }

    send({type: 'found', module: 'InferenceCore.dll', base: infcore.base.toString()});

    // List all exports
    var exports = infcore.enumerateExports();
    exports.forEach(function(e) {
        send({type: 'export', name: e.name, addr: e.address.toString()});
    });

    // Hook exports that might load/decrypt models
    var hookTargets = ['load', 'model', 'decrypt', 'init', 'create', 'session'];

    exports.forEach(function(e) {
        var shouldHook = false;
        for (var i = 0; i < hookTargets.length; i++) {
            if (e.name.toLowerCase().indexOf(hookTargets[i]) !== -1) {
                shouldHook = true;
                break;
            }
        }

        if (shouldHook && e.type === 'function') {
            try {
                Interceptor.attach(e.address, {
                    onEnter: function(args) {
                        send({type: 'call', func: e.name, args: [
                            args[0] ? args[0].toString() : 'null',
                            args[1] ? args[1].toString() : 'null'
                        ]});
                        this.funcName = e.name;
                    },
                    onLeave: function(retval) {
                        send({type: 'ret', func: this.funcName, val: retval.toString()});
                    }
                });
                send({type: 'hooked', name: e.name});
            } catch(err) {
                send({type: 'hook_error', name: e.name, err: err.toString()});
            }
        }
    });

    // Hook ONNX runtime model loading
    var onnxrt = Process.findModuleByName('onnxruntime.dll');
    if (onnxrt) {
        send({type: 'found', module: 'onnxruntime.dll', base: onnxrt.base.toString()});

        // Look for session creation
        var onnxExports = onnxrt.enumerateExports();
        onnxExports.forEach(function(e) {
            if (e.name.indexOf('CreateSession') !== -1 ||
                e.name.indexOf('CreateEnv') !== -1) {
                send({type: 'onnx_export', name: e.name});
            }
        });
    }

    // Hook file reads for .onnx or .ennx files
    var CreateFileW = Module.getExportByName('kernel32.dll', 'CreateFileW');
    Interceptor.attach(CreateFileW, {
        onEnter: function(args) {
            var path = args[0].readUtf16String();
            if (path && (path.indexOf('.onnx') !== -1 ||
                        path.indexOf('.ennx') !== -1 ||
                        path.indexOf('model') !== -1)) {
                send({type: 'file_open', path: path});
                this.modelPath = path;
            }
        },
        onLeave: function(retval) {
            if (this.modelPath) {
                send({type: 'file_handle', path: this.modelPath, handle: retval.toString()});
            }
        }
    });

    // Hook memory mapping for large files
    var MapViewOfFile = Module.getExportByName('kernel32.dll', 'MapViewOfFile');
    Interceptor.attach(MapViewOfFile, {
        onLeave: function(retval) {
            if (!retval.isNull()) {
                try {
                    var header = retval.readByteArray(16);
                    var bytes = new Uint8Array(header);
                    // Check for protobuf/ONNX header
                    if (bytes[0] === 0x08) {
                        send({
                            type: 'mapped_model',
                            addr: retval.toString(),
                            header: Array.from(bytes)
                        });
                    }
                } catch(e) {}
            }
        }
    });

    send({type: 'ready'});
})();
'''

def main():
    pid = find_helios()
    if not pid:
        print("[-] Helios not running")
        return

    print(f"[*] Attaching to Helios PID {pid}...")
    session = frida.attach(pid)

    model_count = [0]

    def on_msg(msg, data):
        if msg['type'] == 'send':
            p = msg['payload']
            t = p.get('type', '')

            if t == 'init':
                print('[+] Hook initialized')
            elif t == 'ready':
                print('[+] All hooks installed - waiting for model load...')
            elif t == 'found':
                print(f'[+] {p["module"]} @ {p["base"]}')
            elif t == 'export':
                # Only print interesting exports
                name = p['name'].lower()
                if any(x in name for x in ['model', 'load', 'decrypt', 'session', 'create']):
                    print(f'    Export: {p["name"]}')
            elif t == 'onnx_export':
                print(f'    ONNX: {p["name"]}')
            elif t == 'hooked':
                print(f'[H] Hooked: {p["name"]}')
            elif t == 'call':
                print(f'[>] {p["func"]}({p["args"][0][:30]}...)')
            elif t == 'ret':
                print(f'[<] {p["func"]} = {p["val"]}')
            elif t == 'file_open':
                print(f'[F] Opening: {p["path"]}')
            elif t == 'file_handle':
                print(f'    Handle: {p["handle"]}')
            elif t == 'mapped_model':
                print(f'[!] Mapped model @ {p["addr"]}')
                print(f'    Header: {p["header"]}')
            elif t == 'error':
                print(f'[-] {p["msg"]}')
            elif t == 'hook_error':
                pass  # Silent
            else:
                pass  # Silent

        elif msg['type'] == 'error':
            print(f'[ERROR] {msg}')

    script = session.create_script(HOOK_SCRIPT)
    script.on('message', on_msg)
    script.load()

    print("\n[*] Now start a script in Helios that loads a model")
    print("[*] Press Ctrl+C to stop\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    session.detach()
    print(f"\n[*] Done")

if __name__ == '__main__':
    main()
