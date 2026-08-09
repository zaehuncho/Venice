"""Capture model data from InferenceCore by hooking infcore_load_model"""
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

    // Hook infcore_set_session to capture auth token
    var setSession = infcore.findExportByName('infcore_set_session');
    if (setSession) {
        Interceptor.attach(setSession, {
            onEnter: function(args) {
                var token = args[0].readUtf8String();
                send({type: 'session', token: token ? token.substring(0, 100) : '(null)'});
            }
        });
        send({type: 'hooked', func: 'infcore_set_session'});
    }

    // Hook infcore_load_model to capture model UUID and path
    var loadModel = infcore.findExportByName('infcore_load_model');
    if (loadModel) {
        Interceptor.attach(loadModel, {
            onEnter: function(args) {
                // Args might be: engine handle, model UUID, model path, etc.
                var arg0 = args[0];
                var arg1 = args[1];
                var arg2 = args[2];

                // Try to read as strings
                var uuid = null, path = null;
                try { uuid = arg1.readUtf8String(); } catch(e) {}
                try { path = arg2.readUtf8String(); } catch(e) {}

                send({
                    type: 'load_model',
                    arg0: arg0.toString(),
                    uuid: uuid,
                    path: path
                });
            },
            onLeave: function(retval) {
                send({type: 'load_model_ret', code: retval.toInt32()});
            }
        });
        send({type: 'hooked', func: 'infcore_load_model'});
    }

    // Hook infcore_list_models to see available models
    var listModels = infcore.findExportByName('infcore_list_models');
    if (listModels) {
        Interceptor.attach(listModels, {
            onEnter: function(args) {
                send({type: 'list_models_call'});
            },
            onLeave: function(retval) {
                // Return value might be JSON or struct pointer
                if (!retval.isNull()) {
                    try {
                        var str = retval.readUtf8String();
                        if (str && str.length < 10000) {
                            send({type: 'list_models', data: str});
                        }
                    } catch(e) {
                        send({type: 'list_models', data: retval.toString()});
                    }
                }
            }
        });
        send({type: 'hooked', func: 'infcore_list_models'});
    }

    // Hook file operations for .onnx/.ennx files
    var CreateFileW = Module.getExportByName('kernel32.dll', 'CreateFileW');
    Interceptor.attach(CreateFileW, {
        onEnter: function(args) {
            var path = args[0].readUtf16String();
            if (path && (path.indexOf('.onnx') !== -1 ||
                        path.indexOf('.ennx') !== -1)) {
                send({type: 'open_model_file', path: path});
            }
        }
    });

    // Hook ReadFile to capture model bytes
    var modelHandle = null;
    var ReadFile = Module.getExportByName('kernel32.dll', 'ReadFile');
    Interceptor.attach(ReadFile, {
        onEnter: function(args) {
            this.handle = args[0];
            this.buffer = args[1];
            this.toRead = args[2].toInt32();
        },
        onLeave: function(retval) {
            if (retval.toInt32() && this.toRead > 100000) {
                // Large read - might be model
                try {
                    var header = this.buffer.readByteArray(64);
                    var bytes = new Uint8Array(header);
                    // ONNX starts with protobuf (0x08) or has 'onnx' magic
                    if (bytes[0] === 0x08 ||
                        (bytes[0] === 0x6f && bytes[1] === 0x6e)) {
                        send({
                            type: 'model_data',
                            size: this.toRead,
                            header: Array.from(bytes.slice(0, 32))
                        }, this.buffer.readByteArray(this.toRead));
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
                print('[+] Initialized')
            elif t == 'ready':
                print('[+] Hooks ready - start a script in Helios to capture model')
            elif t == 'hooked':
                print(f'[H] {p["func"]}')
            elif t == 'session':
                print(f'[!] Session token: {p["token"]}')
            elif t == 'load_model':
                print(f'[>] infcore_load_model()')
                print(f'    UUID: {p["uuid"]}')
                print(f'    Path: {p["path"]}')
            elif t == 'load_model_ret':
                status = 'OK' if p['code'] == 0 else f'ERROR {p["code"]}'
                print(f'[<] Load result: {status}')
            elif t == 'list_models_call':
                print('[>] infcore_list_models()')
            elif t == 'list_models':
                print(f'[<] Models: {p["data"][:500]}')
            elif t == 'open_model_file':
                print(f'[F] Opening: {p["path"]}')
            elif t == 'model_data':
                model_count[0] += 1
                path = os.path.join(OUTPUT_DIR, f'captured_model_{model_count[0]}.onnx')
                print(f'[!!!] CAPTURED MODEL: {p["size"]} bytes')
                print(f'      Header: {p["header"][:16]}')
                if data:
                    with open(path, 'wb') as f:
                        f.write(data)
                    print(f'      Saved: {path}')
            elif t == 'error':
                print(f'[-] {p["msg"]}')

        elif msg['type'] == 'error':
            print(f'[ERROR] {msg}')

    script = session.create_script(HOOK_SCRIPT)
    script.on('message', on_msg)
    script.load()

    print("\n[*] Now start a script in Helios (click Start on 2k_Vision)")
    print("[*] Press Ctrl+C when done\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    session.detach()
    print(f"\n[*] Done. Captured {model_count[0]} model(s)")

if __name__ == '__main__':
    main()
