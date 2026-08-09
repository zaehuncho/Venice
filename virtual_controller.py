import ctypes
import ctypes.wintypes
import dataclasses
import logging
import os
import sys
import threading
import time
from ctypes import Structure, byref, c_short, c_ubyte, c_uint, c_ulong, c_ushort, c_void_p
from dataclasses import dataclass
from enum import IntEnum, IntFlag
logger = logging.getLogger('VirtualController')

class DS4Button(IntFlag):
    NONE = 0
    THUMB_RIGHT = 1 << 0
    THUMB_LEFT = 1 << 1
    OPTIONS = 1 << 2
    CREATE = 1 << 3
    TRIGGER_R = 1 << 4
    TRIGGER_L = 1 << 5
    SHOULDER_R = 1 << 6
    SHOULDER_L = 1 << 7
    TRIANGLE = 1 << 8
    CIRCLE = 1 << 9
    CROSS = 1 << 10
    SQUARE = 1 << 11
    PS = 1 << 12
    TOUCHPAD = 1 << 13

class DS4DPad(IntEnum):
    NONE = 8
    UP = 0
    UP_RIGHT = 1
    RIGHT = 2
    DOWN_RIGHT = 3
    DOWN = 4
    DOWN_LEFT = 5
    LEFT = 6
    UP_LEFT = 7

@dataclass
class ControllerState:
    buttons: int = 0
    dpad: int = DS4DPad.NONE
    left_stick_x: int = 0
    left_stick_y: int = 0
    right_stick_x: int = 0
    right_stick_y: int = 0
    l2_trigger: int = 0
    r2_trigger: int = 0
    gyro_x: int = 0
    gyro_y: int = 0
    gyro_z: int = 0
    accel_x: int = 0
    accel_y: int = 0
    accel_z: int = 0
    touch_x: int = 0
    touch_y: int = 0
    touch_active: bool = False
    timestamp_ns: int = 0

    def is_button_pressed(self, button):
        return bool(self.buttons & button)

    def set_button(self, button, pressed):
        if pressed:
            self.buttons |= button
        else:
            self.buttons &= ~button

    def clone(self):
        return dataclasses.replace(self)

    @property
    def square_pressed(self):
        return self.is_button_pressed(DS4Button.SQUARE)

    @property
    def cross_pressed(self):
        return self.is_button_pressed(DS4Button.CROSS)

    @property
    def right_stick_magnitude(self):
        x = self.right_stick_x / 127.0
        y = self.right_stick_y / 127.0
        return min(1.0, (x * x + y * y) ** 0.5)

    @property
    def right_stick_down(self):
        return self.right_stick_y > 50

    @property
    def right_stick_up(self):
        return self.right_stick_y < -50
_VIGEM_TARGET_TYPE_XBOX360 = 0
_VIGEM_TARGET_TYPE_DS4 = 2
_VIGEM_ERROR_NONE = 536870912
_VIGEM_ERROR_BUS_NOT_FOUND = 3758096385
_VIGEM_ERROR_NO_FREE_SLOT = 3758096386
_VIGEM_ERROR_TARGET_NOT_PLUGGED_IN = 3758096390

class _DS4_REPORT(Structure):
    _fields_ = [('wButtons', c_ushort), ('bSpecial', c_ubyte), ('bThumbLX', c_ubyte), ('bThumbLY', c_ubyte), ('bThumbRX', c_ubyte), ('bThumbRY', c_ubyte), ('bTriggerL', c_ubyte), ('bTriggerR', c_ubyte)]

class ViGEmClient:

    def __init__(self):
        self._dll = None
        self._client = None
        self._target = None
        self._connected = False
        self._lock = threading.Lock()
        self._report = _DS4_REPORT()
        self._dll_path = ''

    @property
    def connected(self):
        with self._lock:
            return self._connected

    def _find_vigem_dll(self):
        here = os.path.dirname(os.path.abspath(__file__))
        parent = os.path.dirname(here)
        search_dirs = [
            here,
            os.path.join(here, 'vendor', 'vigem'),
            os.path.join(here, 'vigem'),
            os.path.join(here, 'native_orion', 'vendor', 'vigem'),
            os.path.join(here, 'native_orion', 'deploy'),
            os.path.join(here, 'native_orion', 'build', 'Release'),
            os.path.join(parent, 'native_orion', 'vendor', 'vigem'),
            os.path.join(parent, 'native_orion', 'deploy'),
            os.path.join(parent, 'vendor', 'vigem'),
            os.path.join(os.environ.get('PROGRAMFILES', 'C:\\Program Files'), 'Nefarius Software Solutions', 'ViGEm Bus Driver'),
            os.path.join(os.environ.get('PROGRAMFILES(X86)', 'C:\\Program Files (x86)'), 'Nefarius Software Solutions', 'ViGEm Bus Driver'),
        ]
        for d in search_dirs:
            for name in ('ViGEmClient.dll', 'ViGEmClient_x64.dll'):
                path = os.path.join(d, name)
                if os.path.isfile(path):
                    return path
        return ''

    def connect(self):
        with self._lock:
            if self._connected:
                return True
            try:
                self._dll_path = self._find_vigem_dll()
                if not self._dll_path:
                    logger.warning('ViGEmClient.dll not found. Install ViGEmBus from https://github.com/nefarius/ViGEmBus/releases')
                    return False
                self._dll = ctypes.CDLL(self._dll_path)
                self._dll.vigem_alloc.restype = c_void_p
                self._client = self._dll.vigem_alloc()
                if not self._client:
                    logger.error('vigem_alloc failed')
                    return False
                self._dll.vigem_connect.argtypes = [c_void_p]
                self._dll.vigem_connect.restype = c_ulong
                err = self._dll.vigem_connect(self._client)
                if err != _VIGEM_ERROR_NONE:
                    logger.error('vigem_connect failed: 0x%08X', err)
                    self._cleanup()
                    return False
                self._dll.vigem_target_ds4_alloc.restype = c_void_p
                self._target = self._dll.vigem_target_ds4_alloc()
                if not self._target:
                    logger.error('vigem_target_ds4_alloc failed')
                    self._cleanup()
                    return False
                self._dll.vigem_target_add.argtypes = [c_void_p, c_void_p]
                self._dll.vigem_target_add.restype = c_ulong
                err = self._dll.vigem_target_add(self._client, self._target)
                if err != _VIGEM_ERROR_NONE:
                    logger.error('vigem_target_add failed: 0x%08X', err)
                    self._cleanup()
                    return False
                # Pre-set argtypes/restype for the hot-path submit call ONCE,
                # not on every submit_state invocation (~1kHz).
                self._dll.vigem_target_ds4_update.argtypes = [c_void_p, c_void_p, _DS4_REPORT]
                self._dll.vigem_target_ds4_update.restype = c_ulong
                self._connected = True
                logger.info('Virtual DS4 controller connected via ViGEm')
                return True
            except Exception as e:
                logger.error('ViGEm init failed: %s', e)
                self._cleanup()
                return False

    def disconnect(self):
        with self._lock:
            self._cleanup()
            self._connected = False

    def _cleanup(self):
        if self._dll and self._target and self._client:
            try:
                self._dll.vigem_target_remove.argtypes = [c_void_p, c_void_p]
                self._dll.vigem_target_remove(self._client, self._target)
            except Exception:
                pass
        if self._dll and self._target:
            try:
                self._dll.vigem_target_free.argtypes = [c_void_p]
                self._dll.vigem_target_free(self._target)
            except Exception:
                pass
            self._target = None
        if self._dll and self._client:
            try:
                self._dll.vigem_disconnect.argtypes = [c_void_p]
                self._dll.vigem_disconnect(self._client)
            except Exception:
                pass
            try:
                self._dll.vigem_free.argtypes = [c_void_p]
                self._dll.vigem_free(self._client)
            except Exception:
                pass
            self._client = None

    def submit_state(self, state):
        # Lock-free fast-path: _connected is only written under _lock in
        # connect()/disconnect(), and GIL makes the bool read atomic. The DLL
        # call itself is thread-safe per ViGEm docs.
        if not self._connected or not self._dll:
            return False
        try:
            wButtons = 0
            dpad_val = state.dpad & 15
            wButtons |= dpad_val
            if state.buttons & DS4Button.SQUARE:
                wButtons |= 1 << 4
            if state.buttons & DS4Button.CROSS:
                wButtons |= 1 << 5
            if state.buttons & DS4Button.CIRCLE:
                wButtons |= 1 << 6
            if state.buttons & DS4Button.TRIANGLE:
                wButtons |= 1 << 7
            if state.buttons & DS4Button.SHOULDER_L:
                wButtons |= 1 << 8
            if state.buttons & DS4Button.SHOULDER_R:
                wButtons |= 1 << 9
            if state.buttons & DS4Button.TRIGGER_L:
                wButtons |= 1 << 10
            if state.buttons & DS4Button.TRIGGER_R:
                wButtons |= 1 << 11
            if state.buttons & DS4Button.CREATE:
                wButtons |= 1 << 12
            if state.buttons & DS4Button.OPTIONS:
                wButtons |= 1 << 13
            if state.buttons & DS4Button.THUMB_LEFT:
                wButtons |= 1 << 14
            if state.buttons & DS4Button.THUMB_RIGHT:
                wButtons |= 1 << 15
            self._report.wButtons = wButtons & 65535
            bSpecial = 0
            if state.buttons & DS4Button.PS:
                bSpecial |= 1 << 0
            if state.buttons & DS4Button.TOUCHPAD:
                bSpecial |= 1 << 1
            self._report.bSpecial = bSpecial & 255
            self._report.bThumbLX = max(0, min(255, state.left_stick_x + 128))
            self._report.bThumbLY = max(0, min(255, state.left_stick_y + 128))
            self._report.bThumbRX = max(0, min(255, state.right_stick_x + 128))
            self._report.bThumbRY = max(0, min(255, state.right_stick_y + 128))
            self._report.bTriggerL = max(0, min(255, state.l2_trigger))
            self._report.bTriggerR = max(0, min(255, state.r2_trigger))
            # argtypes/restype set once in connect(), no per-call overhead
            err = self._dll.vigem_target_ds4_update(self._client, self._target, self._report)
            return err == _VIGEM_ERROR_NONE
        except Exception as e:
            logger.warning('ViGEm submit failed: %s', e)
            return False

    def status(self):
        with self._lock:
            return {'connected': self._connected, 'backend': 'vigem_ds4', 'dll_path': self._dll_path}

def check_vigem_status():
    client = ViGEmClient()
    dll = client._find_vigem_dll()
    if not dll:
        return (False, 'ViGEmClient.dll not found.\n\nPlease install ViGEmBus from:\nhttps://github.com/nefarius/ViGEmBus/releases')
    try:
        ok = client.connect()
        if ok:
            client.disconnect()
            return (True, '')
        else:
            return (False, 'ViGEmBus connection failed.\n\nVerify the driver is running or reinstall from:\nhttps://github.com/nefarius/ViGEmBus/releases')
    except Exception as e:
        return (False, f'ViGEmBus connection error: {e}')

class HidHideClient:
    _IOCTL_SET_WHITELIST = 2147573764
    _IOCTL_GET_WHITELIST = 2147573760
    _IOCTL_SET_ACTIVE = 2147573768
    _IOCTL_GET_ACTIVE = 2147573772

    def __init__(self):
        self._enabled = False
        self._device_path = '\\\\.\\HidHide'
        self._whitelisted_pids = []
        self._lock = threading.Lock()

    @property
    def enabled(self):
        with self._lock:
            return self._enabled

    def activate(self, whitelist_current_process=True):
        if os.name != 'nt':
            return False
        with self._lock:
            try:
                kernel32 = ctypes.windll.kernel32
                GENERIC_READ = 2147483648
                GENERIC_WRITE = 1073741824
                FILE_SHARE_READ = 1
                FILE_SHARE_WRITE = 2
                OPEN_EXISTING = 3
                handle = kernel32.CreateFileW(self._device_path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
                INVALID_HANDLE = -1
                if handle == INVALID_HANDLE:
                    logger.info('HidHide driver not installed or not accessible')
                    return False
                try:
                    if whitelist_current_process:
                        exe_path = sys.executable
                        if exe_path:
                            path_bytes = (exe_path + '\x00').encode('utf-16-le')
                            buf = ctypes.create_string_buffer(path_bytes)
                            bytes_returned = ctypes.wintypes.DWORD(0)
                            kernel32.DeviceIoControl(handle, self._IOCTL_SET_WHITELIST, buf, len(path_bytes), None, 0, byref(bytes_returned), None)
                    active = ctypes.c_ubyte(1)
                    bytes_returned = ctypes.wintypes.DWORD(0)
                    result = kernel32.DeviceIoControl(handle, self._IOCTL_SET_ACTIVE, byref(active), 1, None, 0, byref(bytes_returned), None)
                    self._enabled = bool(result)
                    if self._enabled:
                        logger.info('HidHide activated - physical controller hidden')
                    return self._enabled
                finally:
                    kernel32.CloseHandle(handle)
            except Exception as e:
                logger.warning('HidHide activation failed: %s', e)
                return False

    def deactivate(self):
        if os.name != 'nt':
            return
        with self._lock:
            try:
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.CreateFileW(self._device_path, 2147483648 | 1073741824, 1 | 2, None, 3, 0, None)
                if handle == -1:
                    return
                try:
                    active = ctypes.c_ubyte(0)
                    bytes_returned = ctypes.wintypes.DWORD(0)
                    kernel32.DeviceIoControl(handle, self._IOCTL_SET_ACTIVE, byref(active), 1, None, 0, byref(bytes_returned), None)
                    self._enabled = False
                    logger.info('HidHide deactivated - controllers visible')
                finally:
                    kernel32.CloseHandle(handle)
            except Exception as e:
                logger.warning('HidHide deactivation failed: %s', e)

    def status(self):
        with self._lock:
            return {'enabled': self._enabled, 'backend': 'hidhide'}

class _XINPUT_GAMEPAD(Structure):
    _fields_ = [('wButtons', c_ushort), ('bLeftTrigger', c_ubyte), ('bRightTrigger', c_ubyte), ('sThumbLX', c_short), ('sThumbLY', c_short), ('sThumbRX', c_short), ('sThumbRY', c_short)]

class _XINPUT_STATE(Structure):
    _fields_ = [('dwPacketNumber', c_uint), ('Gamepad', _XINPUT_GAMEPAD)]

class PhysicalControllerReader:
    _XINPUT_GAMEPAD = _XINPUT_GAMEPAD
    _XINPUT_STATE = _XINPUT_STATE
    _XINPUT_GAMEPAD_DPAD_UP = 1
    _XINPUT_GAMEPAD_DPAD_DOWN = 2
    _XINPUT_GAMEPAD_DPAD_LEFT = 4
    _XINPUT_GAMEPAD_DPAD_RIGHT = 8
    _XINPUT_GAMEPAD_START = 16
    _XINPUT_GAMEPAD_BACK = 32
    _XINPUT_GAMEPAD_LEFT_THUMB = 64
    _XINPUT_GAMEPAD_RIGHT_THUMB = 128
    _XINPUT_GAMEPAD_LEFT_SHOULDER = 256
    _XINPUT_GAMEPAD_RIGHT_SHOULDER = 512
    _XINPUT_GAMEPAD_A = 4096
    _XINPUT_GAMEPAD_B = 8192
    _XINPUT_GAMEPAD_X = 16384
    _XINPUT_GAMEPAD_Y = 32768
    _XINPUT_GAMEPAD_GUIDE = 0x0400

    def __init__(self, user_index=None):
        self._user_index = user_index
        self._xinput = None
        self._xinput_ex_ordinal = None
        self._available = False
        self._last_packet = 0
        self._lock = threading.Lock()
        self._init_xinput()

    def _init_xinput(self):
        if os.name != 'nt':
            return
        for dll_name in ('xinput1_4.dll', 'xinput1_3.dll', 'xinput9_1_0.dll'):
            try:
                self._xinput = ctypes.windll.LoadLibrary(dll_name)
                self._available = True
                logger.info('XInput loaded: %s', dll_name)
                if dll_name == 'xinput1_3.dll':
                    try:
                        _lib13 = ctypes.WinDLL('xinput1_3.dll')
                        self._xinput_ex_ordinal = ctypes.WINFUNCTYPE(
                            c_uint, c_uint, ctypes.POINTER(self._XINPUT_STATE)
                        )(100, _lib13)
                        logger.info('XInputGetStateEx (Guide/PS button) loaded via ordinal 100')
                    except Exception:
                        pass
                if self._user_index is None:
                    self._user_index = self._detect_slot()
                return
            except Exception:
                continue
        logger.info('XInput not available - physical controller reading disabled')

    def _detect_slot(self):
        if self._xinput is None:
            return 0
        for idx in range(4):
            try:
                st = self._XINPUT_STATE()
                if self._xinput.XInputGetState(idx, byref(st)) == 0:
                    logger.info('PhysicalControllerReader: controller on XInput slot %d', idx)
                    return idx
            except Exception:
                pass
        logger.warning('PhysicalControllerReader: no controller on slots 0-3, defaulting to 0')
        return 0

    @property
    def available(self):
        return self._available

    def read_state(self):
        if not self._available or self._xinput is None:
            return None
        with self._lock:
            try:
                state = self._XINPUT_STATE()
                result = self._xinput.XInputGetState(self._user_index, byref(state))
                if result != 0:
                    _new = self._detect_slot()
                    if _new != self._user_index:
                        logger.info('XInput slot %d->%d', self._user_index, _new)
                        self._user_index = _new
                        result = self._xinput.XInputGetState(self._user_index, byref(state))
                if result != 0:
                    return None
                gp = state.Gamepad
                cs = ControllerState(timestamp_ns=time.perf_counter_ns())
                xb = gp.wButtons
                if xb & self._XINPUT_GAMEPAD_X:
                    cs.buttons |= DS4Button.SQUARE
                if xb & self._XINPUT_GAMEPAD_A:
                    cs.buttons |= DS4Button.CROSS
                if xb & self._XINPUT_GAMEPAD_B:
                    cs.buttons |= DS4Button.CIRCLE
                if xb & self._XINPUT_GAMEPAD_Y:
                    cs.buttons |= DS4Button.TRIANGLE
                if xb & self._XINPUT_GAMEPAD_LEFT_SHOULDER:
                    cs.buttons |= DS4Button.SHOULDER_L
                if xb & self._XINPUT_GAMEPAD_RIGHT_SHOULDER:
                    cs.buttons |= DS4Button.SHOULDER_R
                if xb & self._XINPUT_GAMEPAD_LEFT_THUMB:
                    cs.buttons |= DS4Button.THUMB_LEFT
                if xb & self._XINPUT_GAMEPAD_RIGHT_THUMB:
                    cs.buttons |= DS4Button.THUMB_RIGHT
                if xb & self._XINPUT_GAMEPAD_START:
                    cs.buttons |= DS4Button.OPTIONS
                if xb & self._XINPUT_GAMEPAD_BACK:
                    cs.buttons |= DS4Button.CREATE
                if self._xinput_ex_ordinal is not None:
                    try:
                        _st_ex = self._XINPUT_STATE()
                        if self._xinput_ex_ordinal(self._user_index, byref(_st_ex)) == 0:
                            if _st_ex.Gamepad.wButtons & self._XINPUT_GAMEPAD_GUIDE:
                                cs.buttons |= DS4Button.PS
                    except Exception:
                        pass
                if gp.bLeftTrigger > 20:
                    cs.buttons |= DS4Button.TRIGGER_L
                if gp.bRightTrigger > 20:
                    cs.buttons |= DS4Button.TRIGGER_R
                up = bool(xb & self._XINPUT_GAMEPAD_DPAD_UP)
                down = bool(xb & self._XINPUT_GAMEPAD_DPAD_DOWN)
                left = bool(xb & self._XINPUT_GAMEPAD_DPAD_LEFT)
                right = bool(xb & self._XINPUT_GAMEPAD_DPAD_RIGHT)
                if up and right:
                    cs.dpad = DS4DPad.UP_RIGHT
                elif up and left:
                    cs.dpad = DS4DPad.UP_LEFT
                elif down and right:
                    cs.dpad = DS4DPad.DOWN_RIGHT
                elif down and left:
                    cs.dpad = DS4DPad.DOWN_LEFT
                elif up:
                    cs.dpad = DS4DPad.UP
                elif down:
                    cs.dpad = DS4DPad.DOWN
                elif left:
                    cs.dpad = DS4DPad.LEFT
                elif right:
                    cs.dpad = DS4DPad.RIGHT
                else:
                    cs.dpad = DS4DPad.NONE
                cs.left_stick_x = max(-128, min(127, gp.sThumbLX >> 8))
                cs.left_stick_y = max(-128, min(127, -(gp.sThumbLY >> 8)))
                cs.right_stick_x = max(-128, min(127, gp.sThumbRX >> 8))
                cs.right_stick_y = max(-128, min(127, -(gp.sThumbRY >> 8)))
                cs.l2_trigger = gp.bLeftTrigger
                cs.r2_trigger = gp.bRightTrigger
                self._last_packet = state.dwPacketNumber
                return cs
            except Exception as e:
                logger.warning('XInput read failed: %s', e)
                return None

    def status(self):
        return {'available': self._available, 'user_index': self._user_index, 'backend': 'xinput'}

class ControllerIOHub:

    def __init__(self):
        self._physical = PhysicalControllerReader()
        self._vigem = ViGEmClient()
        self._hidhide = HidHideClient()
        self._remap_fn = None
        self._stop_evt = threading.Event()
        self._poll_thread = None
        self._poll_interval_ms = 1.0
        self._last_state = None
        self._state_lock = threading.Lock()
        self._timer_period_set = False
        self._stats = {'polls': 0, 'submits': 0, 'errors': 0, 'avg_poll_us': 0.0, 'remap_active': False}
        self._stats_lock = threading.Lock()

    def set_remap_function(self, fn):
        self._remap_fn = fn
        with self._stats_lock:
            self._stats['remap_active'] = fn is not None

    def _set_timer_resolution(self):
        if os.name != 'nt' or self._timer_period_set:
            return
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)
            self._timer_period_set = True
            logger.debug('Timer resolution set to 1ms')
        except Exception:
            pass

    def _restore_timer_resolution(self):
        if not self._timer_period_set:
            return
        try:
            ctypes.windll.winmm.timeEndPeriod(1)
            self._timer_period_set = False
        except Exception:
            pass

    def start(self, hide_physical=True):
        if not self._vigem.connect():
            logger.error('Failed to connect ViGEm')
            return False
        if hide_physical:
            self._hidhide.activate()
        self._set_timer_resolution()
        self._stop_evt.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, name='ControllerIO', daemon=True)
        self._poll_thread.start()
        logger.info('Controller I/O hub started (%.0f Hz)', 1000.0 / self._poll_interval_ms)
        return True

    def stop(self):
        self._stop_evt.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=3.0)
        self._hidhide.deactivate()
        self._vigem.disconnect()
        self._restore_timer_resolution()
        logger.info('Controller I/O hub stopped')

    def get_last_state(self):
        with self._state_lock:
            return self._last_state.clone() if self._last_state else None

    def inject_state(self, state):
        return self._vigem.submit_state(state)

    def status(self):
        with self._stats_lock:
            stats = dict(self._stats)
        stats['physical'] = self._physical.status()
        stats['vigem'] = self._vigem.status()
        stats['hidhide'] = self._hidhide.status()
        return stats

    def _poll_loop(self):
        if os.name == 'nt':
            try:
                THREAD_PRIORITY_TIME_CRITICAL = 15
                ctypes.windll.kernel32.SetThreadPriority(
                    ctypes.windll.kernel32.GetCurrentThread(),
                    THREAD_PRIORITY_TIME_CRITICAL)
                logger.debug('Controller poll thread set to TIME_CRITICAL priority')
            except Exception:
                pass
        poll_times = []
        interval_s = self._poll_interval_ms / 1000.0
        while not self._stop_evt.is_set():
            t0 = time.perf_counter()
            try:
                physical_state = self._physical.read_state()
                if physical_state is None:
                    with self._stats_lock:
                        self._stats['polls'] += 1
                    remaining = interval_s - (time.perf_counter() - t0)
                    if remaining > 0.0001:
                        time.sleep(remaining)
                    continue
                remap_fn = self._remap_fn
                if remap_fn is not None:
                    try:
                        output_state = remap_fn(physical_state)
                    except Exception:
                        output_state = physical_state
                else:
                    output_state = physical_state
                ok = self._vigem.submit_state(output_state)
                with self._state_lock:
                    self._last_state = output_state
                with self._stats_lock:
                    self._stats['polls'] += 1
                    if ok:
                        self._stats['submits'] += 1
                    else:
                        self._stats['errors'] += 1
                elapsed_us = (time.perf_counter() - t0) * 1000000.0
                poll_times.append(elapsed_us)
                if len(poll_times) >= 1000:
                    avg = sum(poll_times) / len(poll_times)
                    with self._stats_lock:
                        self._stats['avg_poll_us'] = round(avg, 1)
                    poll_times.clear()
            except Exception:
                with self._stats_lock:
                    self._stats['errors'] += 1
            remaining = interval_s - (time.perf_counter() - t0)
            if remaining > 0.0001:
                time.sleep(remaining)