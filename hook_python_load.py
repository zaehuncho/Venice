"""Hook Helios to see what Python loading does"""
import frida
import time
import subprocess

# Find Helios
result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq Helios2.exe', '/FO', 'CSV'],
                      capture_output=True, text=True)
pid = None
for line in result.stdout.strip().split('\n')[1:]:
    if 'Helios2' in line:
        pid = int(line.strip('"').split('","')[1])
        break

if not pid:
    print("Helios2 not running")
    exit(1)

print(f"Attaching to PID {pid}...")
session = frida.attach(pid)

script = session.create_script(r'''
(function() {
    send({type: 'ready'});

    // Hook LoadLibraryW
    var LoadLibraryW = Module.getExportByName('kernel32.dll', 'LoadLibraryW');
    Interceptor.attach(LoadLibraryW, {
        onEnter: function(args) {
            var name = args[0].readUtf16String();
            if (name && name.toLowerCase().indexOf('python') !== -1) {
                send({type: 'load', dll: name});
            }
        },
        onLeave: function(retval) {
            if (this.name && retval.isNull()) {
                send({type: 'load_failed', dll: this.name});
            }
        }
    });

    // Hook CreateProcessW to see if it spawns Python
    var CreateProcessW = Module.getExportByName('kernel32.dll', 'CreateProcessW');
    Interceptor.attach(CreateProcessW, {
        onEnter: function(args) {
            var app = args[0].isNull() ? null : args[0].readUtf16String();
            var cmd = args[1].isNull() ? null : args[1].readUtf16String();
            if ((app && app.indexOf('python') !== -1) || (cmd && cmd.indexOf('python') !== -1)) {
                send({type: 'spawn', app: app, cmd: cmd});
            }
        }
    });

    send({type: 'hooks_installed'});
})();
''')

def on_msg(msg, data):
    if msg['type'] == 'send':
        p = msg['payload']
        t = p.get('type', '')
        if t == 'ready':
            print('[+] Ready')
        elif t == 'hooks_installed':
            print('[+] Hooks installed')
        elif t == 'load':
            print(f'[*] LoadLibrary: {p["dll"]}')
        elif t == 'load_failed':
            print(f'[-] Load FAILED: {p.get("dll", "?")}')
        elif t == 'spawn':
            print(f'[*] CreateProcess: {p.get("app")} | {p.get("cmd")}')
        elif t == 'getproc':
            status = 'OK' if p['found'] else 'MISSING'
            print(f'[*] GetProcAddress: {p["func"]} = {status}')
        else:
            print(f'[?] {p}')

script.on('message', on_msg)
script.load()

print("\n[*] Monitoring for 10 seconds...")
print("[*] If Python shows Invalid, click on Tools > Settings in Helios")
time.sleep(10)

session.detach()
print("\n[*] Done")
