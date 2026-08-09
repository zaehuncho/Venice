"""Hook Helios at runtime to extract model data when it loads"""
import frida
import subprocess
import time
import sys
import os

# Find Helios
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

print(f"[*] Attaching to Helios PID {pid}...")
session = frida.attach(pid)

# Hook script to intercept model loading and auth
script = session.create_script(r'''
(function() {
    send({type: 'attached', pid: Process.id});

    // Track loaded modules
    var loadedModules = {};
    Process.enumerateModules().forEach(function(m) {
        loadedModules[m.name.toLowerCase()] = m.base;
    });

    // Check if ch.dll is loaded
    var ch = Process.findModuleByName('ch.dll');
    if (ch) {
        send({type: 'found', module: 'ch.dll', base: ch.base.toString()});

        // Hook the r() function (init)
        var r_func = ch.findExportByName('r');
        if (r_func) {
            Interceptor.attach(r_func, {
                onEnter: function(args) {
                    send({type: 'r_call', w: args[0].toInt32(), h: args[1].toInt32()});
                },
                onLeave: function(retval) {
                    send({type: 'r_return', code: retval.toInt32()});
                }
            });
            send({type: 'hooked', func: 'r'});
        }

        // Hook CheckAuth
        var checkAuth = ch.findExportByName('CheckAuth');
        if (checkAuth) {
            Interceptor.attach(checkAuth, {
                onEnter: function(args) {
                    send({type: 'auth_check'});
                },
                onLeave: function(retval) {
                    // Force return TRUE
                    retval.replace(ptr(1));
                    send({type: 'auth_forced', original: retval.toInt32()});
                }
            });
            send({type: 'hooked', func: 'CheckAuth'});
        }

        // Hook IsLicensed
        var isLicensed = ch.findExportByName('IsLicensed');
        if (isLicensed) {
            Interceptor.attach(isLicensed, {
                onEnter: function(args) {},
                onLeave: function(retval) {
                    retval.replace(ptr(1));
                    send({type: 'license_forced'});
                }
            });
            send({type: 'hooked', func: 'IsLicensed'});
        }
    } else {
        send({type: 'not_loaded', module: 'ch.dll'});
    }

    // Hook LoadLibraryW to catch when ch.dll loads
    var LoadLibraryW = Module.getExportByName('kernel32.dll', 'LoadLibraryW');
    Interceptor.attach(LoadLibraryW, {
        onEnter: function(args) {
            var name = args[0].readUtf16String();
            if (name && name.toLowerCase().indexOf('ch.dll') !== -1) {
                send({type: 'loading', dll: name});
                this.target = name;
            }
        },
        onLeave: function(retval) {
            if (this.target) {
                if (!retval.isNull()) {
                    send({type: 'loaded', dll: this.target, handle: retval.toString()});
                    // TODO: Hook exports after load
                } else {
                    send({type: 'load_failed', dll: this.target});
                }
            }
        }
    });

    // Hook network calls for model downloads
    var ws2_send = Module.findExportByName('ws2_32.dll', 'send');
    if (ws2_send) {
        Interceptor.attach(ws2_send, {
            onEnter: function(args) {
                var buf = args[1];
                var len = args[2].toInt32();
                if (len > 0 && len < 1000) {
                    try {
                        var data = buf.readUtf8String(Math.min(len, 200));
                        if (data && (data.indexOf('model') !== -1 ||
                                    data.indexOf('onnx') !== -1 ||
                                    data.indexOf('decrypt') !== -1 ||
                                    data.indexOf('key') !== -1)) {
                            send({type: 'net_send', data: data.substring(0, 200)});
                        }
                    } catch(e) {}
                }
            }
        });
    }

    send({type: 'ready'});
})();
''')

def on_msg(msg, data):
    if msg['type'] == 'send':
        p = msg['payload']
        t = p.get('type', '')
        if t == 'attached':
            print(f'[+] Attached to PID {p["pid"]}')
        elif t == 'ready':
            print('[+] Hooks installed')
        elif t == 'found':
            print(f'[+] Found {p["module"]} at {p["base"]}')
        elif t == 'not_loaded':
            print(f'[!] {p["module"]} not loaded yet')
        elif t == 'hooked':
            print(f'[+] Hooked {p["func"]}')
        elif t == 'loading':
            print(f'[>] Loading: {p["dll"]}')
        elif t == 'loaded':
            print(f'[<] Loaded: {p["dll"]} @ {p["handle"]}')
        elif t == 'load_failed':
            print(f'[-] Load failed: {p["dll"]}')
        elif t == 'r_call':
            print(f'[>] r({p["w"]}, {p["h"]})')
        elif t == 'r_return':
            print(f'[<] r() = {p["code"]}')
        elif t == 'auth_check':
            print('[*] CheckAuth called')
        elif t == 'auth_forced':
            print(f'[!] Auth forced to TRUE (was {p["original"]})')
        elif t == 'license_forced':
            print('[!] License forced to TRUE')
        elif t == 'net_send':
            print(f'[NET] {p["data"][:100]}...')
        else:
            print(f'[?] {p}')
    elif msg['type'] == 'error':
        print(f'[ERROR] {msg}')

script.on('message', on_msg)
script.load()

print("\n[*] Monitoring Helios...")
print("[*] Now click Start in Helios to run the 2k_Vision script")
print("[*] Press Ctrl+C to stop\n")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass

session.detach()
print("\n[*] Done")
