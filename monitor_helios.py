"""Monitor Helios for script and model loading"""

import frida
import time
import sys

JS_HOOK = r'''
(function() {
    send({type: 'attached'});

    // Hook DLL loading
    var LoadLibrary = Module.getExportByName('kernel32.dll', 'LoadLibraryW');
    Interceptor.attach(LoadLibrary, {
        onEnter: function(args) {
            var name = args[0].readUtf16String();
            if (name && (name.indexOf('ch.dll') !== -1 || name.indexOf('nba2k') !== -1 ||
                        name.indexOf('inference') !== -1 || name.indexOf('onnx') !== -1 ||
                        name.indexOf('python') !== -1)) {
                send({type: 'dll_load', name: name});
            }
        }
    });

    // Check if InferenceCore is already loaded
    var infcore = Process.findModuleByName('InferenceCore.dll');
    if (infcore) {
        send({type: 'found', module: 'InferenceCore.dll'});

        // Hook model loading
        var loadModel = infcore.findExportByName('infcore_load_model');
        if (loadModel) {
            Interceptor.attach(loadModel, {
                onEnter: function(args) {
                    var uuid = args[1].readUtf8String();
                    send({type: 'model_load', uuid: uuid});
                },
                onLeave: function(retval) {
                    send({type: 'model_result', code: retval.toInt32()});
                }
            });
        }

        // Hook session
        var setSession = infcore.findExportByName('infcore_set_session');
        if (setSession) {
            Interceptor.attach(setSession, {
                onEnter: function(args) {
                    var token = args[0].readUtf8String();
                    send({type: 'session', token: token ? token.substring(0, 50) : '(null)'});
                }
            });
        }
    }

    // Hook ch.dll if loaded
    var ch = Process.findModuleByName('ch.dll');
    if (ch) {
        send({type: 'found', module: 'ch.dll'});

        ['CheckAuth', 'IsLicensed', 'r', 'p'].forEach(function(name) {
            var exp = ch.findExportByName(name);
            if (exp) {
                Interceptor.attach(exp, {
                    onEnter: function(args) {
                        send({type: 'ch_call', func: name});
                    },
                    onLeave: function(retval) {
                        send({type: 'ch_return', func: name, val: retval.toInt32()});
                    }
                });
            }
        });
    }

    send({type: 'ready'});
})();
'''

def main():
    # Find Helios
    import subprocess
    result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq Helios2.exe', '/FO', 'CSV'],
                          capture_output=True, text=True)
    pid = None
    for line in result.stdout.strip().split('\n')[1:]:
        if 'Helios2' in line:
            pid = int(line.strip('"').split('","')[1])
            break

    if not pid:
        print("[-] Helios2 not running")
        sys.exit(1)

    print(f"[*] Attaching to PID {pid}...")
    session = frida.attach(pid)

    def on_msg(msg, data):
        if msg['type'] == 'send':
            p = msg['payload']
            t = p.get('type', '')
            if t == 'attached':
                print('[+] Attached')
            elif t == 'ready':
                print('[+] Hooks ready')
            elif t == 'found':
                print(f'[+] Found {p["module"]}')
            elif t == 'dll_load':
                print(f'[*] Loading DLL: {p["name"]}')
            elif t == 'model_load':
                print(f'[>] Model load: {p["uuid"]}')
            elif t == 'model_result':
                code = p['code']
                status = 'OK' if code == 0 else f'ERROR {code}'
                print(f'[<] Result: {status}')
            elif t == 'session':
                print(f'[*] Session token: {p["token"]}...')
            elif t == 'ch_call':
                print(f'[>] ch.dll: {p["func"]}()')
            elif t == 'ch_return':
                print(f'[<] ch.dll: {p["func"]}() = {p["val"]}')
            else:
                print(f'[?] {p}')

    script = session.create_script(JS_HOOK)
    script.on('message', on_msg)
    script.load()

    print()
    print('[*] Now load a script in Helios:')
    print('    1. Go to CV Python panel')
    print('    2. Select _2k_Vision/2k_Vision.py')
    print('    3. Click Run/Start')
    print()
    print('[*] Press Ctrl+C to stop')

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    session.detach()
    print('\n[*] Done')

if __name__ == '__main__':
    main()
