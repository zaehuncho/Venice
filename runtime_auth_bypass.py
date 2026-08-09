"""Runtime auth bypass using Frida - hook isUserAuthenticated to always return true"""
import frida
import subprocess
import time
import sys

def find_helios():
    result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq Helios2.exe', '/FO', 'CSV'],
                          capture_output=True, text=True)
    for line in result.stdout.strip().split('\n')[1:]:
        if 'Helios2' in line:
            return int(line.strip('"').split('","')[1])
    return None

# Spawn Helios if not running
pid = find_helios()
if not pid:
    print("[*] Spawning Helios...")
    pid = frida.spawn([r"C:\Users\aaron\Desktop\HeliosII\Helios.exe"])
    session = frida.attach(pid)
    resumed = False
else:
    print(f"[*] Attaching to existing Helios PID {pid}")
    session = frida.attach(pid)
    resumed = True

HOOK_SCRIPT = r'''
(function() {
    send({type: 'init'});

    // Find Helios.exe module
    var helios = Process.findModuleByName('Helios.exe');
    if (!helios) {
        send({type: 'error', msg: 'Helios.exe not found'});
        return;
    }
    send({type: 'found', module: 'Helios.exe', base: helios.base.toString()});

    // Search for isUserAuthenticated in exports
    var exports = helios.enumerateExports();
    var authFunc = null;
    var sessionFunc = null;

    exports.forEach(function(e) {
        if (e.name.indexOf('isUserAuthenticated') !== -1) {
            authFunc = e.address;
            send({type: 'found_export', name: e.name, addr: e.address.toString()});
        }
        if (e.name.indexOf('getAuthSession') !== -1) {
            sessionFunc = e.address;
            send({type: 'found_export', name: e.name, addr: e.address.toString()});
        }
    });

    // Also check CvPython.dll
    var cvpython = Process.findModuleByName('CvPython.dll');
    if (cvpython) {
        send({type: 'found', module: 'CvPython.dll', base: cvpython.base.toString()});

        cvpython.enumerateExports().forEach(function(e) {
            if (e.name.indexOf('Authenticated') !== -1 || e.name.indexOf('authenticated') !== -1) {
                send({type: 'cvpython_export', name: e.name, addr: e.address.toString()});
            }
        });
    }

    // Search for the PluginHost class which has isUserAuthenticated
    // Look for the pattern in memory

    // Strategy: Hook any function that returns bool and is called before UI updates
    // We can find this by looking for the string "isUserAuthenticated" in memory
    // and finding xrefs to it

    var ranges = Process.enumerateRanges('r--');
    var authStringAddr = null;

    ranges.forEach(function(range) {
        try {
            var pattern = '69 73 55 73 65 72 41 75 74 68 65 6E 74 69 63 61 74 65 64';  // "isUserAuthenticated"
            var matches = Memory.scanSync(range.base, range.size, pattern);
            if (matches.length > 0) {
                authStringAddr = matches[0].address;
                send({type: 'string_found', pattern: 'isUserAuthenticated', addr: authStringAddr.toString()});
            }
        } catch(e) {}
    });

    // Alternative: Find all instances where a function returns 0 or 1
    // and is followed by a conditional that disables UI

    // For now, let's try hooking PluginHost::isUserAuthenticated directly
    // It should be in Helios.exe exports or as a symbol

    // Check symbols
    var symbols = helios.enumerateSymbols();
    symbols.forEach(function(s) {
        if (s.name.indexOf('isUserAuth') !== -1 || s.name.indexOf('Authenticated') !== -1) {
            send({type: 'symbol', name: s.name, addr: s.address.toString()});
        }
    });

    // If we found the auth function, hook it
    if (authFunc) {
        Interceptor.attach(authFunc, {
            onLeave: function(retval) {
                // Force return true
                retval.replace(ptr(1));
                send({type: 'auth_bypass', original: retval.toInt32()});
            }
        });
        send({type: 'hooked', func: 'isUserAuthenticated'});
    } else {
        send({type: 'warning', msg: 'isUserAuthenticated not found in exports, trying pattern scan...'});

        // Scan for a common pattern: function that just returns 0 or 1
        // mov eax, 0; ret OR xor eax, eax; ret
        // These are likely auth check stubs

        // Look for 'B8 00 00 00 00 C3' (mov eax, 0; ret)
        // or '33 C0 C3' (xor eax, eax; ret)
        // and replace with 'B8 01 00 00 00 C3' (mov eax, 1; ret)

        var textSection = null;
        helios.enumerateRanges('r-x').forEach(function(r) {
            if (r.base.equals(helios.base.add(0x1000))) {
                textSection = r;
            }
        });

        if (textSection) {
            // Scan for xor eax,eax; ret (auth fail stubs)
            var pattern = '33 C0 C3';
            var matches = Memory.scanSync(textSection.base, textSection.size, pattern);
            send({type: 'pattern_matches', pattern: pattern, count: matches.length});

            // Patch first few that look like auth checks
            matches.slice(0, 10).forEach(function(m, i) {
                // Change to mov eax, 1; ret
                Memory.patchCode(m.address, 6, function(code) {
                    var writer = new X86Writer(code, {pc: m.address});
                    writer.putMovRegU32('eax', 1);
                    writer.putRet();
                    writer.flush();
                });
                send({type: 'patched', addr: m.address.toString(), index: i});
            });
        }
    }

    send({type: 'ready'});
})();
'''

def on_msg(msg, data):
    if msg['type'] == 'send':
        p = msg['payload']
        t = p.get('type', '')
        if t == 'init':
            print('[+] Script loaded')
        elif t == 'found':
            print(f'[+] {p["module"]} @ {p["base"]}')
        elif t == 'found_export':
            print(f'[!] FOUND: {p["name"]} @ {p["addr"]}')
        elif t == 'cvpython_export':
            print(f'[*] CvPython: {p["name"]}')
        elif t == 'string_found':
            print(f'[*] String "{p["pattern"]}" @ {p["addr"]}')
        elif t == 'symbol':
            print(f'[*] Symbol: {p["name"]} @ {p["addr"]}')
        elif t == 'hooked':
            print(f'[+] Hooked: {p["func"]}')
        elif t == 'auth_bypass':
            print(f'[!] AUTH BYPASSED (was {p["original"]})')
        elif t == 'warning':
            print(f'[!] {p["msg"]}')
        elif t == 'pattern_matches':
            print(f'[*] Pattern {p["pattern"]}: {p["count"]} matches')
        elif t == 'patched':
            print(f'[+] Patched @ {p["addr"]} (#{p["index"]})')
        elif t == 'ready':
            print('[+] Ready - check Helios UI')
        elif t == 'error':
            print(f'[-] {p["msg"]}')
        else:
            print(f'[?] {p}')
    elif msg['type'] == 'error':
        print(f'[ERROR] {msg}')

script = session.create_script(HOOK_SCRIPT)
script.on('message', on_msg)
script.load()

if not resumed:
    print("[*] Resuming Helios...")
    frida.resume(pid)

print("\n[*] Auth bypass active. Press Ctrl+C when done.\n")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass

session.detach()
print("\n[*] Done")
