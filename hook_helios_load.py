"""Hook Helios to see what environment it sets before loading ch.dll"""
import frida
import subprocess
import time
import sys

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

script = session.create_script(r'''
(function() {
    send({type: 'attached'});

    // Hook SetEnvironmentVariableW to see what gets set
    var SetEnvW = Module.getExportByName('kernel32.dll', 'SetEnvironmentVariableW');
    Interceptor.attach(SetEnvW, {
        onEnter: function(args) {
            var name = args[0].readUtf16String();
            var value = args[1].isNull() ? null : args[1].readUtf16String();
            if (value && value.length < 500) {
                send({type: 'setenv', name: name, value: value});
            } else {
                send({type: 'setenv', name: name, value: '(too long or null)'});
            }
        }
    });

    // Hook LoadLibraryW
    var LoadLibraryW = Module.getExportByName('kernel32.dll', 'LoadLibraryW');
    Interceptor.attach(LoadLibraryW, {
        onEnter: function(args) {
            var name = args[0].readUtf16String();
            if (name && (name.indexOf('ch.dll') !== -1 || name.indexOf('python') !== -1)) {
                send({type: 'loadlib', dll: name});
            }
        },
        onLeave: function(retval) {
            if (this.dll && this.dll.indexOf('ch.dll') !== -1) {
                send({type: 'loaded', dll: this.dll, handle: retval.toString()});
            }
        }
    });

    // Check current environment for HELIOS vars
    var GetEnvW = Module.getExportByName('kernel32.dll', 'GetEnvironmentVariableW');
    var names = ['HELIOS_SESSION_TOKEN', 'HELIOS_USER', 'HELIOS_LICENSE'];
    names.forEach(function(name) {
        var buf = Memory.alloc(2048);
        var namePtr = Memory.allocUtf16String(name);
        var len = new NativeFunction(GetEnvW, 'uint32', ['pointer', 'pointer', 'uint32'])(namePtr, buf, 1024);
        if (len > 0) {
            send({type: 'env', name: name, value: buf.readUtf16String()});
        }
    });

    send({type: 'ready'});
})();
''')

def on_msg(msg, data):
    if msg['type'] == 'send':
        p = msg['payload']
        t = p.get('type', '')
        if t == 'attached':
            print('[+] Attached')
        elif t == 'ready':
            print('[+] Hooks ready')
        elif t == 'setenv':
            print(f'[env] {p["name"]} = {p["value"][:100]}...' if len(str(p["value"])) > 100 else f'[env] {p["name"]} = {p["value"]}')
        elif t == 'loadlib':
            print(f'[dll] Loading: {p["dll"]}')
        elif t == 'loaded':
            print(f'[dll] Loaded: {p["dll"]} -> {p["handle"]}')
        elif t == 'env':
            print(f'[ENV] {p["name"]} = {p["value"][:50]}...' if len(p["value"]) > 50 else f'[ENV] {p["name"]} = {p["value"]}')
        else:
            print(f'[?] {p}')
    elif msg['type'] == 'error':
        print(f'[!] {msg}')

script.on('message', on_msg)
script.load()

print("\n[*] Monitoring for 30 seconds...")
print("[*] Click Start in Helios to run the 2k_Vision script")

try:
    time.sleep(30)
except KeyboardInterrupt:
    pass

session.detach()
print("\n[*] Done")
