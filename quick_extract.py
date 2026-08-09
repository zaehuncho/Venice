"""
Quick ONNX extraction - hooks model loading and dumps immediately
"""

import frida
import sys
import os
import time
from datetime import datetime

EXTRACT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extracted")
os.makedirs(EXTRACT_DIR, exist_ok=True)

JS_HOOK = r"""
(function() {
    send({type: 'init', msg: 'Starting hooks...'});

    var dumpRequests = [];

    // Find key modules
    var infcore = Process.findModuleByName('InferenceCore.dll');
    var ort = Process.findModuleByName('onnxruntime.dll');

    if (infcore) {
        send({type: 'found', module: 'InferenceCore.dll', base: infcore.base.toString()});
    }
    if (ort) {
        send({type: 'found', module: 'onnxruntime.dll', base: ort.base.toString()});
    }

    // Hook infcore_list_models to get available models
    if (infcore) {
        var listModels = infcore.findExportByName('infcore_list_models');
        if (listModels) {
            Interceptor.attach(listModels, {
                onEnter: function(args) {
                    this.buf = args[0];
                    this.size = args[1].toInt32();
                },
                onLeave: function(retval) {
                    var len = retval.toInt32();
                    if (len > 0) {
                        var json = this.buf.readUtf8String(len);
                        send({type: 'models', json: json});
                    }
                }
            });
            send({type: 'hook', name: 'infcore_list_models'});
        }

        // Hook load_model
        var loadModel = infcore.findExportByName('infcore_load_model');
        if (loadModel) {
            Interceptor.attach(loadModel, {
                onEnter: function(args) {
                    this.uuid = args[1].readUtf8String();
                    send({type: 'load_start', uuid: this.uuid});
                },
                onLeave: function(retval) {
                    send({type: 'load_end', uuid: this.uuid, result: retval.toInt32()});
                }
            });
            send({type: 'hook', name: 'infcore_load_model'});
        }
    }

    // Hook ORT session creation to catch model bytes
    if (ort) {
        var exports = ort.enumerateExports();
        exports.forEach(function(exp) {
            // OrtCreateSessionFromArray takes raw model bytes
            if (exp.name.indexOf('CreateSessionFromArray') !== -1 ||
                exp.name.indexOf('OrtSessionOptionsAppendExecutionProvider') !== -1) {
                send({type: 'export', name: exp.name});
            }

            if (exp.name.indexOf('CreateSessionFromArray') !== -1) {
                Interceptor.attach(exp.address, {
                    onEnter: function(args) {
                        // args layout varies but typically includes model_data ptr and size
                        this.modelData = args[1];  // Usually second arg
                        this.modelSize = args[2].toInt32();  // Usually third arg

                        if (this.modelSize > 10000 && this.modelSize < 100000000) {
                            send({type: 'session_array', size: this.modelSize});

                            // Read the model bytes
                            try {
                                var data = Memory.readByteArray(this.modelData, this.modelSize);
                                send({type: 'model_data', size: this.modelSize}, data);
                            } catch(e) {
                                send({type: 'error', msg: 'Failed to read model: ' + e});
                            }
                        }
                    }
                });
                send({type: 'hook', name: exp.name});
            }
        });
    }

    // Scan memory for ONNX files
    function scanMemory() {
        send({type: 'scan', msg: 'Scanning memory for ONNX...'});
        var count = 0;

        Process.enumerateRanges('r--').forEach(function(range) {
            if (range.size < 500000 || range.size > 200000000) return;

            try {
                // Scan for protobuf ONNX header
                var matches = Memory.scanSync(range.base, Math.min(range.size, 50000000), '08 ?? 12');

                matches.forEach(function(match) {
                    // Verify it looks like ONNX by checking for strings
                    try {
                        var preview = Memory.readByteArray(match.address, 512);
                        var str = '';
                        var bytes = new Uint8Array(preview);
                        for (var i = 0; i < 512; i++) {
                            if (bytes[i] >= 32 && bytes[i] < 127) str += String.fromCharCode(bytes[i]);
                        }

                        if (str.indexOf('onnx') !== -1 || str.indexOf('ir_version') !== -1 ||
                            str.indexOf('producer') !== -1 || str.indexOf('TensorRT') !== -1 ||
                            str.indexOf('pytorch') !== -1 || str.indexOf('graph') !== -1) {

                            // Try to estimate size by looking for end
                            var estimatedSize = range.size - (match.address - range.base);
                            if (estimatedSize > 50000000) estimatedSize = 50000000;

                            send({
                                type: 'onnx_match',
                                addr: match.address.toString(),
                                rangeBase: range.base.toString(),
                                rangeSize: range.size,
                                estimatedSize: estimatedSize,
                                strPreview: str.substring(0, 100)
                            });
                            count++;
                        }
                    } catch(e) {}
                });
            } catch(e) {}
        });

        send({type: 'scan_done', count: count});
    }

    // Handle dump requests
    recv('dump', function(msg) {
        var addr = ptr(msg.addr);
        var size = msg.size;
        send({type: 'dumping', addr: msg.addr, size: size});

        try {
            var data = Memory.readByteArray(addr, size);
            send({type: 'dump_complete', addr: msg.addr, size: size}, data);
        } catch(e) {
            send({type: 'dump_error', msg: e.toString()});
        }
    });

    // Initial scan after short delay
    setTimeout(function() {
        scanMemory();
    }, 1000);

    send({type: 'ready', msg: 'Hooks ready!'});
})();
"""

class QuickExtractor:
    def __init__(self, pid):
        self.pid = pid
        self.session = None
        self.script = None
        self.onnx_locations = []
        self.saved_files = []

    def on_message(self, message, data):
        if message['type'] == 'send':
            p = message['payload']
            t = p.get('type', '')

            if t == 'init':
                print(f"[*] {p['msg']}")
            elif t == 'found':
                print(f"[+] Found {p['module']} @ {p['base']}")
            elif t == 'hook':
                print(f"[+] Hooked: {p['name']}")
            elif t == 'export':
                print(f"    Export: {p['name']}")
            elif t == 'ready':
                print(f"\n[+] {p['msg']}\n")
            elif t == 'models':
                print(f"\n[*] Available models JSON:")
                print(p['json'][:500])
                self.save_json('models.json', p['json'])
            elif t == 'load_start':
                print(f"\n[>] Loading model: {p['uuid']}")
            elif t == 'load_end':
                status = "OK" if p['result'] == 0 else f"ERR {p['result']}"
                print(f"[<] Load result: {status}")
            elif t == 'scan':
                print(f"[*] {p['msg']}")
            elif t == 'scan_done':
                print(f"[*] Scan complete: {p['count']} potential ONNX regions")
            elif t == 'onnx_match':
                print(f"\n[!] ONNX FOUND @ {p['addr']}")
                print(f"    Region: {p['rangeBase']} ({p['rangeSize']:,} bytes)")
                print(f"    Preview: {p['strPreview'][:80]}...")
                self.onnx_locations.append(p)
            elif t == 'session_array':
                print(f"\n[!] ORT CreateSessionFromArray: {p['size']:,} bytes")
            elif t == 'model_data':
                print(f"[+] Captured model data: {p['size']:,} bytes")
                if data:
                    self.save_model(data)
            elif t == 'dumping':
                print(f"[*] Dumping {p['size']:,} bytes from {p['addr']}...")
            elif t == 'dump_complete':
                print(f"[+] Dump complete: {p['size']:,} bytes")
                if data:
                    self.save_model(data, p['addr'])
            elif t == 'dump_error':
                print(f"[-] Dump error: {p['msg']}")
            elif t == 'error':
                print(f"[-] {p['msg']}")

        elif message['type'] == 'error':
            print(f"[!] {message.get('stack', message)}")

    def save_model(self, data, addr='auto'):
        ts = datetime.now().strftime('%H%M%S')
        fname = f"model_{ts}_{addr[-6:] if len(addr) > 6 else addr}.onnx"
        fpath = os.path.join(EXTRACT_DIR, fname)

        with open(fpath, 'wb') as f:
            f.write(data)

        print(f"[+] SAVED: {fpath} ({len(data):,} bytes)")
        self.saved_files.append(fpath)

        # Quick verify
        if data[:2] == b'\x08\x00' or data[:2] == b'\x08\x08':
            print(f"    Header looks like ONNX protobuf")

    def save_json(self, name, content):
        fpath = os.path.join(EXTRACT_DIR, name)
        with open(fpath, 'w') as f:
            f.write(content)
        print(f"[+] Saved: {fpath}")

    def dump_location(self, loc):
        """Request dump of found ONNX location"""
        addr = loc['addr']
        size = min(loc['estimatedSize'], 50 * 1024 * 1024)  # Max 50MB
        self.script.post({'type': 'dump', 'addr': addr, 'size': size})

    def run(self, timeout=60):
        print(f"[*] Attaching to PID {self.pid}...")

        self.session = frida.attach(self.pid)
        self.script = self.session.create_script(JS_HOOK)
        self.script.on('message', self.on_message)
        self.script.load()

        print(f"[*] Monitoring for {timeout} seconds...")
        print(f"[*] Load a model in Helios now!\n")

        try:
            time.sleep(timeout)
        except KeyboardInterrupt:
            print("\n[*] Interrupted")

        # Dump any found ONNX locations
        if self.onnx_locations:
            print(f"\n[*] Dumping {len(self.onnx_locations)} found ONNX regions...")
            for loc in self.onnx_locations[:5]:  # Limit to 5
                self.dump_location(loc)
                time.sleep(2)  # Wait for dump

        self.session.detach()

        print(f"\n{'='*50}")
        print(f"EXTRACTION COMPLETE")
        print(f"{'='*50}")
        print(f"ONNX regions found: {len(self.onnx_locations)}")
        print(f"Files saved: {len(self.saved_files)}")
        for f in self.saved_files:
            print(f"  - {f}")
        print(f"\nOutput: {EXTRACT_DIR}")


def main():
    print("="*50)
    print("QUICK ONNX EXTRACTOR")
    print("="*50)

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
        print("[-] Helios2.exe not running!")
        sys.exit(1)

    print(f"[+] Found Helios2.exe PID: {pid}\n")

    extractor = QuickExtractor(pid)
    extractor.run(timeout=30)


if __name__ == '__main__':
    main()
