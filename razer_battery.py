# -*- coding: utf-8 -*-
"""
Windows 无线外设电量托盘（雷蛇毒蝰 V2 Pro / 漫步者 G3 无线耳机 / Xbox Elite 2 手柄）
====================================================================================
零第三方依赖：只用 Python 标准库 + ctypes（GUI 用 tkinter）。
视觉风格：复刻 Windows 11 系统鼠标/耳机/手柄轮廓与系统电池 / 蓝牙耳机电量提示风格。

用法：
  pythonw razer_battery.py               托盘常驻（默认）
  python  razer_battery.py --once        打印三台设备电量后退出
  python  razer_battery.py --panel       打开简洁面板（Win11 蓝牙设备三卡片）
  python  razer_battery.py --debug-ui    完整调试版（所有 HID 设备 / Feature Report / 试发命令 / 标定）
  python  razer_battery.py --scan        控制台扫描并测试三台设备协议
  python  razer_battery.py --install     创建「启动」文件夹快捷方式 + 桌面链接
  python  razer_battery.py --uninstall   删除上面两样
  任意模式加 --debug                     在控制台输出调试信息（不写任何日志文件）
"""
import ctypes
import ctypes.wintypes as wt
import datetime
import json
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import zlib

# ════════════════════════════════════════════════════════════════════
#  基本常量 / 配置
# ════════════════════════════════════════════════════════════════════
APP_NAME = "雷蛇电量托盘"
TRAY_CLASS = "RazerViperBatteryTrayWnd"
PANEL_TITLE = "无线外设电量"
DEBUG_TITLE = "无线外设电量 · 调试"
FROZEN = bool(getattr(sys, "frozen", False))
HERE = os.path.dirname(os.path.realpath(sys.executable if FROZEN else __file__))
SCRIPT = os.path.realpath(sys.executable if FROZEN else __file__)
CONFIG_PATH = os.path.join(HERE, "config.json")
LAUNCHER = os.path.join(HERE, "启动电量托盘.pyw")
ICON_PATH = os.path.join(HERE, "icon.ico")

RAZER_VID = 0x1532
RAZER_PIDS = {0x00A5: "有线", 0x00A6: "2.4G 接收器"}

HECATE_VID = 0x35BB
HECATE_G3_WIRED_PID = 0xA21F      # mi_03&col02, Report ID 0xE0
HECATE_G3_WIRELESS_PID = 0xA220   # mi_03&col03, Report ID 0xD0

# 设备顺序（按任务栏从左到右）
DEVICES = [
    {"id": "mouse",   "name": "雷蛇毒蝰 V2 Pro",    "short": "毒蝰 V2 Pro",   "uid_base": 10},
    {"id": "headset", "name": "漫步者 G3 无线耳机", "short": "漫步者 G3",     "uid_base": 20},
    {"id": "gamepad", "name": "Xbox Elite 2 手柄", "short": "Xbox Elite 2", "uid_base": 30},
]
DEV_BY_ID = {d["id"]: d for d in DEVICES}

# 任务栏图标样式（每槽位严格 1 个图标）：
#   dev_pct   = 上方 Win11 设备空心轮廓 + 下方电量百分比
#   bt_single = 蓝牙耳机风「左设备空心轮廓 + 右竖向电池条」
#   bat_pct   = 上方 Win11 横向电池 + 下方电量百分比
#   pct_only  = 纯大号百分比数字
VALID_STYLES = ("dev_pct", "bt_single", "bat_pct", "pct_only")
VALID_MODES = ("lowest", "all")

DEFAULT_CONFIG = {
    "interval": 60,           # 刷新间隔（秒），30 ~ 1800
    "tray_mode": "lowest",    # lowest = 仅显示 1 个图标（默认显示电量最低的在线设备）；all = 三台各显 1 个
    "style": "dev_pct",       # 图标样式
    "enabled": {"mouse": True, "headset": True, "gamepad": True},
    "low_alert": 20,          # 低电量通知阈值（%），0 关闭
    "extra_pids": [],         # 其他雷蛇 PID（十六进制字符串）
    # 雷蛇协议标定参数（调试版可覆盖）
    "transaction_ids": ["1F", "3F", "FF", "9F"],
    "battery_cmd": ["07", "80", "02"],
    "charge_cmd": ["07", "84", "02"],
    "value_index": 9,
    "value_scale": 255,
}
INTERVAL_CHOICES = [30, 60, 120, 300, 600, 900, 1800]

DEBUG = "--debug" in sys.argv


def dlog(*a):
    if DEBUG and sys.stdout:
        try:
            print(time.strftime("[%H:%M:%S]"), *a, flush=True)
        except Exception:
            pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg["enabled"] = dict(DEFAULT_CONFIG["enabled"])
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
            en = raw.pop("enabled", None)
            cfg.update(raw)
            if isinstance(en, dict):
                cfg["enabled"].update({k: bool(v) for k, v in en.items() if k in DEV_BY_ID})
    except Exception:
        pass
    cfg["interval"] = max(30, min(1800, int(cfg.get("interval", 60))))
    if cfg.get("style") not in VALID_STYLES:
        cfg["style"] = DEFAULT_CONFIG["style"]
    if cfg.get("tray_mode") not in VALID_MODES:
        cfg["tray_mode"] = DEFAULT_CONFIG["tray_mode"]
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(dict(cfg), f, ensure_ascii=False, indent=2)
    except Exception as e:
        dlog("保存配置失败", e)


def _hexlist(xs):
    return [int(x, 16) if isinstance(x, str) else int(x) for x in xs]


# ════════════════════════════════════════════════════════════════════
#  Win32 声明
# ════════════════════════════════════════════════════════════════════
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
hid = ctypes.WinDLL("hid", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
HANDLE = ctypes.c_void_p
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


def make_guid(s):
    s = s.strip("{}")
    a, b, c, rest = s.split("-", 3)          # 最后一节里还有连字符
    rest = bytes.fromhex(rest.replace("-", ""))
    return GUID(int(a, 16), int(b, 16), int(c, 16), (ctypes.c_ubyte * 8)(*rest))


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wt.DWORD), ("Reserved", ctypes.c_size_t)]


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("ClassGuid", GUID),
                ("DevInst", wt.DWORD), ("Reserved", ctypes.c_size_t)]


class DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wt.ULONG)]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", wt.ULONG), ("VendorID", wt.USHORT),
                ("ProductID", wt.USHORT), ("VersionNumber", wt.USHORT)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", wt.USHORT), ("UsagePage", wt.USHORT),
                ("InputReportByteLength", wt.USHORT),
                ("OutputReportByteLength", wt.USHORT),
                ("FeatureReportByteLength", wt.USHORT),
                ("Reserved", wt.USHORT * 17),
                ("NumberLinkCollectionNodes", wt.USHORT),
                ("NumberInputButtonCaps", wt.USHORT),
                ("NumberInputValueCaps", wt.USHORT),
                ("NumberInputDataIndices", wt.USHORT),
                ("NumberOutputButtonCaps", wt.USHORT),
                ("NumberOutputValueCaps", wt.USHORT),
                ("NumberOutputDataIndices", wt.USHORT),
                ("NumberFeatureButtonCaps", wt.USHORT),
                ("NumberFeatureValueCaps", wt.USHORT),
                ("NumberFeatureDataIndices", wt.USHORT)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD), ("hEvent", HANDLE)]


kernel32.CreateFileW.restype = HANDLE
kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                 wt.DWORD, wt.DWORD, HANDLE]
kernel32.CloseHandle.argtypes = [HANDLE]
kernel32.CreateMutexW.restype = HANDLE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
kernel32.WaitForSingleObject.argtypes = [HANDLE, wt.DWORD]
kernel32.WaitForSingleObject.restype = wt.DWORD
kernel32.ReleaseMutex.argtypes = [HANDLE]
kernel32.GetModuleHandleW.restype = HANDLE
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.CreateEventW.restype = HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
kernel32.ResetEvent.argtypes = [HANDLE]
kernel32.ReadFile.argtypes = [HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.POINTER(OVERLAPPED)]
kernel32.WriteFile.argtypes = [HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.POINTER(OVERLAPPED)]
kernel32.GetOverlappedResult.argtypes = [HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wt.DWORD), wt.BOOL]
kernel32.CancelIoEx.argtypes = [HANDLE, ctypes.POINTER(OVERLAPPED)]

setupapi.SetupDiGetClassDevsW.restype = HANDLE
setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, wt.HWND, wt.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID),
                                                 wt.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
                                                      ctypes.c_void_p, wt.DWORD,
                                                      ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
setupapi.SetupDiEnumDeviceInfo.argtypes = [HANDLE, wt.DWORD, ctypes.POINTER(SP_DEVINFO_DATA)]
setupapi.SetupDiGetDevicePropertyW.argtypes = [HANDLE, ctypes.POINTER(SP_DEVINFO_DATA),
                                               ctypes.POINTER(DEVPROPKEY), ctypes.POINTER(wt.ULONG),
                                               ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.DWORD]
setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [HANDLE, ctypes.POINTER(SP_DEVINFO_DATA),
                                                 wt.LPWSTR, wt.DWORD, ctypes.POINTER(wt.DWORD)]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [HANDLE]

hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
hid.HidD_GetAttributes.argtypes = [HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]
hid.HidD_GetPreparsedData.argtypes = [HANDLE, ctypes.POINTER(ctypes.c_void_p)]
hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
hid.HidP_GetCaps.restype = ctypes.c_long
hid.HidD_GetProductString.argtypes = [HANDLE, ctypes.c_void_p, wt.ULONG]
hid.HidD_GetManufacturerString.argtypes = [HANDLE, ctypes.c_void_p, wt.ULONG]
hid.HidD_SetFeature.argtypes = [HANDLE, ctypes.c_void_p, wt.ULONG]
hid.HidD_GetFeature.argtypes = [HANDLE, ctypes.c_void_p, wt.ULONG]

GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_RW = 0x3
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000


# ════════════════════════════════════════════════════════════════════
#  公共数据结构 & 跨进程互斥锁
# ════════════════════════════════════════════════════════════════════
class Reading:
    def __init__(self, dev_id="", ok=False, level=None, charging=False,
                 source="", msg="", state="absent", detail="", approx=False):
        self.dev_id = dev_id
        self.ok = ok
        self.level = level
        self.charging = charging
        self.source = source
        self.msg = msg
        self.state = state        # ok / sleep / absent / error
        self.detail = detail      # 固件版本 / 协议说明等
        self.approx = approx      # True = 分档估算（如 XInput 4 档）
        self.time = datetime.datetime.now()


class NamedMutex:
    def __init__(self, name):
        self.h = kernel32.CreateMutexW(None, False, name)

    def __enter__(self):
        if self.h:
            kernel32.WaitForSingleObject(self.h, 5000)
        return self

    def __exit__(self, *a):
        if self.h:
            kernel32.ReleaseMutex(self.h)


_MUTEXES = {}


def hid_mutex(name="Local\\RazerViperBatteryHID"):
    m = _MUTEXES.get(name)
    if m is None:
        m = _MUTEXES[name] = NamedMutex(name)
    return m


# ════════════════════════════════════════════════════════════════════
#  HID 枚举
# ════════════════════════════════════════════════════════════════════
class HidInfo:
    __slots__ = ("path", "vid", "pid", "usage_page", "usage", "in_len", "out_len",
                 "feat_len", "product", "manufacturer", "mi", "col")

    def __repr__(self):
        return (f"{self.vid:04X}:{self.pid:04X} MI_{self.mi} Col{self.col} "
                f"UP={self.usage_page:04X} U={self.usage:04X} "
                f"in={self.in_len} out={self.out_len} feat={self.feat_len} {self.product!r}")


def hid_open(path, access, flags=0):
    h = kernel32.CreateFileW(path, access, FILE_SHARE_RW, None, OPEN_EXISTING, flags, None)
    if h in (None, INVALID_HANDLE_VALUE):
        return None
    return h


def hid_open_best(path):
    """先读写打开；系统独占的鼠标/键盘集合只能用 access=0 打开（Feature Report 仍可用）。"""
    h = hid_open(path, GENERIC_READ | GENERIC_WRITE)
    if h:
        return h, "rw"
    h = hid_open(path, 0)
    return (h, "0") if h else (None, None)


def enum_hid_paths():
    guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    hdev = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x12)
    paths = []
    if hdev in (None, INVALID_HANDLE_VALUE):
        return paths
    try:
        i = 0
        cb = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
        while True:
            did = SP_DEVICE_INTERFACE_DATA()
            did.cbSize = ctypes.sizeof(did)
            if not setupapi.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(guid), i, ctypes.byref(did)):
                break
            i += 1
            need = wt.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(did), None, 0, ctypes.byref(need), None)
            if not need.value:
                continue
            buf = ctypes.create_string_buffer(need.value + 4)
            struct.pack_into("I", buf, 0, cb)
            if setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(did), buf, need, None, None):
                paths.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return paths


def hid_info(path):
    h = hid_open(path, 0)
    if not h:
        return None
    try:
        info = HidInfo()
        info.path = path
        attr = HIDD_ATTRIBUTES()
        attr.Size = ctypes.sizeof(attr)
        if not hid.HidD_GetAttributes(h, ctypes.byref(attr)):
            return None
        info.vid, info.pid = attr.VendorID, attr.ProductID
        info.usage_page = info.usage = info.in_len = info.out_len = info.feat_len = 0
        pp = ctypes.c_void_p()
        if hid.HidD_GetPreparsedData(h, ctypes.byref(pp)):
            caps = HIDP_CAPS()
            if hid.HidP_GetCaps(pp, ctypes.byref(caps)) == 0x00110000:
                info.usage_page, info.usage = caps.UsagePage, caps.Usage
                info.in_len = caps.InputReportByteLength
                info.out_len = caps.OutputReportByteLength
                info.feat_len = caps.FeatureReportByteLength
            hid.HidD_FreePreparsedData(pp)
        sbuf = ctypes.create_unicode_buffer(128)
        info.product = sbuf.value if hid.HidD_GetProductString(h, sbuf, 256) else ""
        sbuf = ctypes.create_unicode_buffer(128)
        info.manufacturer = sbuf.value if hid.HidD_GetManufacturerString(h, sbuf, 256) else ""
        m = re.search(r"mi_([0-9a-f]+)", path, re.I)
        info.mi = int(m.group(1), 16) if m else -1
        m = re.search(r"col([0-9a-f]+)", path, re.I)
        info.col = int(m.group(1), 16) if m else 0
        return info
    finally:
        kernel32.CloseHandle(h)


def enum_hid(vid=None):
    out = []
    for p in enum_hid_paths():
        if vid is not None and f"vid_{vid:04x}" not in p.lower():
            continue
        i = hid_info(p)
        if i:
            out.append(i)
    return out


# ════════════════════════════════════════════════════════════════════
#  设备 1：雷蛇毒蝰 V2 Pro（90 字节 Feature Report，Report ID 0）
# ════════════════════════════════════════════════════════════════════
RAZER_STATUS = {0x00: "无回复", 0x01: "忙", 0x02: "成功", 0x03: "失败",
                0x04: "超时（鼠标休眠/未连接）", 0x05: "不支持"}
_good_tid = {}


def razer_packet(tid, cls, cmd, size, args=b""):
    r = bytearray(90)
    r[1] = tid
    r[5] = size
    r[6] = cls
    r[7] = cmd
    r[8:8 + len(args)] = args[:80]
    crc = 0
    for b in r[2:88]:
        crc ^= b
    r[88] = crc
    return bytes(r)


def razer_transact(h, tid, cls, cmd, size, args=b"", tries=12):
    pkt = razer_packet(tid, cls, cmd, size, args)
    buf = (ctypes.c_ubyte * 91)(0, *pkt)
    if not hid.HidD_SetFeature(h, buf, 91):
        return f"SetFeature 失败（错误 {ctypes.get_last_error()}）", None
    last = None
    for n in range(tries):
        time.sleep(0.025 if n == 0 else 0.04)
        rb = (ctypes.c_ubyte * 91)()
        if not hid.HidD_GetFeature(h, rb, 91):
            return f"GetFeature 失败（错误 {ctypes.get_last_error()}）", None
        resp = bytes(rb)[1:]
        last = resp
        st = resp[0]
        if st in (0x00, 0x01):
            continue
        if st == 0x02:
            if resp[6] == cls and resp[7] == cmd:
                return "ok", resp
            continue
        return RAZER_STATUS.get(st, f"状态 0x{st:02X}"), resp
    return ("无回复" if last is None or last[0] == 0 else RAZER_STATUS.get(last[0], "?")), last


def find_viper_interfaces(cfg):
    pids = dict(RAZER_PIDS)
    for p in cfg.get("extra_pids", []):
        try:
            pids.setdefault(int(p, 16) if isinstance(p, str) else int(p), "自定义")
        except Exception:
            pass
    cands = [i for i in enum_hid(RAZER_VID) if i.pid in pids and i.feat_len >= 91]
    cands.sort(key=lambda i: (0 if pids[i.pid] == "有线" else 1, i.mi if i.mi >= 0 else 99, i.col))
    return cands, pids


def read_viper(cfg=None):
    cfg = cfg or load_config()
    tids = _hexlist(cfg["transaction_ids"])
    bcls, bcmd, bsize = _hexlist(cfg["battery_cmd"])
    ccls, ccmd, csize = _hexlist(cfg["charge_cmd"])
    vidx, vscale = int(cfg["value_index"]), max(1, int(cfg["value_scale"]))

    cands, pids = find_viper_interfaces(cfg)
    if not cands:
        return Reading("mouse", state="absent", msg="未检测到接收器或数据线")
    saw_sleep, errors = False, []
    with hid_mutex("Local\\RazerViperBatteryHID"):
        for info in cands:
            h, mode = hid_open_best(info.path)
            if not h:
                errors.append("打不开设备")
                continue
            try:
                order = [_good_tid[info.pid]] if info.pid in _good_tid else []
                order += [t for t in tids if t not in order]
                for tid in order:
                    st, resp = razer_transact(h, tid, bcls, bcmd, bsize)
                    dlog(f"[mouse] {info.pid:04X} MI_{info.mi} tid={tid:02X} access={mode} → {st}")
                    if st == "ok":
                        _good_tid[info.pid] = tid
                        level = max(0, min(100, round(resp[vidx] * 100 / vscale)))
                        charging = False
                        st2, r2 = razer_transact(h, tid, ccls, ccmd, csize)
                        if st2 == "ok":
                            charging = r2[vidx] == 1
                        return Reading("mouse", True, level, charging, pids[info.pid],
                                       state="ok", detail="雷蛇 HID 0x07/0x80")
                    if st.startswith("超时"):
                        saw_sleep = True
                        break
                    if "失败（错误" in st:
                        errors.append(st)
                        break
                    errors.append(st)
            finally:
                kernel32.CloseHandle(h)
    if saw_sleep:
        return Reading("mouse", state="sleep", source="2.4G 接收器", msg="鼠标休眠或未开机，动一下鼠标")
    return Reading("mouse", state="error", msg="设备无响应：" + (errors[-1] if errors else "未知"))


# ════════════════════════════════════════════════════════════════════
#  设备 2：漫步者 HECATE G3 无线耳机（35BB:A220 无线 / 35BB:A21F 有线）
#
#  协议（已实机验证）：
#    无线 35BB:A220 → mi_03&col03，64 字节 Output/Input，Report ID 0xD0
#    有线 35BB:A21F → mi_03&col02，64 字节 Output/Input，Report ID 0xE0
#    查询状态：[<D0|E0>, 0x35, 0xBB, 0x01, 0x00, 0x00...]
#    回复校验：resp[1]==0x35 and resp[2]==0xBB and resp[4]==0x00 and resp[3]>=4
#      resp[5] == 1  已连接（有线恒为已连接）
#      resp[6] % 101 电量百分比 0..100
#      resp[7] % 3   充电状态（0=未充, 1=充电中, 2=已充满）
#      resp[8..10]   耳机固件版本；resp[11..13] 接收器固件版本
#  注：严格使用独立 OVERLAPPED 读/写句柄 + 事件超时 + CancelIoEx。
# ════════════════════════════════════════════════════════════════════
def read_hecate_g3(cfg=None):
    cands = []
    for d in enum_hid(HECATE_VID):
        pl = d.path.lower()
        if d.pid == HECATE_G3_WIRED_PID and "mi_03&col02" in pl and d.in_len >= 64 and d.out_len >= 64:
            cands.append((0, False, 0xE0, "USB 有线", d))
        elif d.pid == HECATE_G3_WIRELESS_PID and "mi_03&col03" in pl and d.in_len >= 64 and d.out_len >= 64:
            cands.append((1, True, 0xD0, "2.4G 接收器", d))
    if not cands:
        return Reading("headset", state="absent", msg="未检测到耳机接收器或数据线")
    cands.sort(key=lambda x: x[0])

    with hid_mutex("Local\\HecateG3BatteryHID"):
        for _, wireless, rid, src, info in cands:
            hr = hid_open(info.path, GENERIC_READ, FILE_FLAG_OVERLAPPED)
            hw = hid_open(info.path, GENERIC_WRITE, FILE_FLAG_OVERLAPPED)
            if not hr or not hw:
                if hr:
                    kernel32.CloseHandle(hr)
                if hw:
                    kernel32.CloseHandle(hw)
                continue
            ev_r = kernel32.CreateEventW(None, True, False, None)
            ev_w = kernel32.CreateEventW(None, True, False, None)
            try:
                in_len, out_len = info.in_len, info.out_len
                # 先投递异步读，再写请求，防止漏收极快回复
                rbuf = (ctypes.c_ubyte * in_len)()
                ov_r = OVERLAPPED(hEvent=ev_r)
                kernel32.ResetEvent(ev_r)
                got = wt.DWORD()
                ok_r = kernel32.ReadFile(hr, rbuf, in_len, ctypes.byref(got), ctypes.byref(ov_r))
                err_r = ctypes.get_last_error()

                wbuf = (ctypes.c_ubyte * out_len)()
                wbuf[0], wbuf[1], wbuf[2], wbuf[3], wbuf[4] = rid, 0x35, 0xBB, 0x01, 0x00
                ov_w = OVERLAPPED(hEvent=ev_w)
                kernel32.ResetEvent(ev_w)
                wr = wt.DWORD()
                ok_w = kernel32.WriteFile(hw, wbuf, out_len, ctypes.byref(wr), ctypes.byref(ov_w))
                if not ok_w:
                    ew = ctypes.get_last_error()
                    if ew == 997:       # ERROR_IO_PENDING
                        if kernel32.WaitForSingleObject(ev_w, 500) == 0:
                            kernel32.GetOverlappedResult(hw, ctypes.byref(ov_w), ctypes.byref(wr), False)
                        else:
                            kernel32.CancelIoEx(hw, ctypes.byref(ov_w))
                            kernel32.WaitForSingleObject(ev_w, 200)
                    else:
                        dlog(f"[headset] WriteFile 失败 {ew}")

                deadline = time.time() + 0.65
                matched = None
                while time.time() < deadline:
                    rem = int(max(1, (deadline - time.time()) * 1000))
                    if ok_r or (err_r == 997 and kernel32.WaitForSingleObject(ev_r, rem) == 0):
                        if not ok_r:
                            kernel32.GetOverlappedResult(hr, ctypes.byref(ov_r), ctypes.byref(got), False)
                        rep = bytes(rbuf)[:got.value]
                        dlog(f"[headset] recv: {rep[:16].hex(' ')}")
                        if len(rep) >= 8 and rep[1] == 0x35 and rep[2] == 0xBB and rep[4] == 0x00 and rep[3] >= 4:
                            matched = rep
                            break
                        rbuf = (ctypes.c_ubyte * in_len)()
                        ov_r = OVERLAPPED(hEvent=ev_r)
                        kernel32.ResetEvent(ev_r)
                        ok_r = kernel32.ReadFile(hr, rbuf, in_len, ctypes.byref(got), ctypes.byref(ov_r))
                        err_r = ctypes.get_last_error()
                    else:
                        break
                if not ok_r and err_r == 997:
                    kernel32.CancelIoEx(hr, ctypes.byref(ov_r))
                    kernel32.WaitForSingleObject(ev_r, 200)

                if matched:
                    connected = (not wireless) or (matched[5] == 1)
                    if not connected:
                        return Reading("headset", state="sleep", source=src, msg="耳机未开机或未连接")
                    level = matched[6] % 101
                    chg_state = matched[7] % 3          # 0=未充, 1=充电中, 2=已充满
                    charging = (chg_state == 1)
                    fw = f"固件 v{matched[8]}.{matched[9]}.{matched[10]}" if matched[3] >= 7 and len(matched) >= 11 else "HECATE HID"
                    if chg_state == 2:
                        fw = "已充满 · " + fw
                    return Reading("headset", True, level, charging, src, state="ok", detail=fw)
            finally:
                kernel32.CloseHandle(ev_r)
                kernel32.CloseHandle(ev_w)
                kernel32.CloseHandle(hr)
                kernel32.CloseHandle(hw)
    return Reading("headset", state="sleep", source="2.4G 接收器", msg="耳机未开机或无响应")


# ════════════════════════════════════════════════════════════════════
#  设备 3：Xbox Elite 2 手柄（蓝牙 LE 精确电量 / Xbox 无线适配器 & USB XInput）
# ════════════════════════════════════════════════════════════════════
_KEY_BT_BAT = DEVPROPKEY(make_guid("{104EA319-6EE2-4701-BD47-8DDBF425BBE5}"), 2)
_KEY_IS_CONN = DEVPROPKEY(make_guid("{83DA6326-97A6-4088-9453-A1923F573B29}"), 15)
_KEY_NAME = DEVPROPKEY(make_guid("{B725F130-47EF-101A-A5F1-02608C9EEBAC}"), 10)
_KEY_FRIENDLY = DEVPROPKEY(make_guid("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 14)


def _setupdi_prop_str(h, did, key):
    pt, need = wt.ULONG(), wt.DWORD()
    setupapi.SetupDiGetDevicePropertyW(h, ctypes.byref(did), ctypes.byref(key), ctypes.byref(pt), None, 0, ctypes.byref(need), 0)
    if not need.value:
        return ""
    buf = ctypes.create_string_buffer(need.value)
    if setupapi.SetupDiGetDevicePropertyW(h, ctypes.byref(did), ctypes.byref(key), ctypes.byref(pt), buf, need.value, None, 0):
        return ctypes.wstring_at(buf)
    return ""


def _read_xbox_ble():
    """蓝牙 LE 连接时通过 DEVPKEY_Bluetooth_Battery 读取精确 0..100% 电量。"""
    h = setupapi.SetupDiGetClassDevsW(None, None, None, 0x06)  # DIGCF_ALLCLASSES | DIGCF_PRESENT
    if h in (None, INVALID_HANDLE_VALUE):
        return None
    try:
        i = 0
        while True:
            did = SP_DEVINFO_DATA()
            did.cbSize = ctypes.sizeof(did)
            if not setupapi.SetupDiEnumDeviceInfo(h, i, ctypes.byref(did)):
                break
            i += 1
            idbuf = ctypes.create_unicode_buffer(512)
            setupapi.SetupDiGetDeviceInstanceIdW(h, ctypes.byref(did), idbuf, 512, None)
            iid = idbuf.value
            if not iid.upper().startswith("BTHLE\\"):
                continue
            nm = _setupdi_prop_str(h, did, _KEY_FRIENDLY) or _setupdi_prop_str(h, did, _KEY_NAME)
            if "xbox" not in nm.lower() and "elite" not in nm.lower():
                continue
            pt, need = wt.ULONG(), wt.DWORD()
            conn = ctypes.c_byte(0)
            okc = setupapi.SetupDiGetDevicePropertyW(h, ctypes.byref(did), ctypes.byref(_KEY_IS_CONN),
                                                     ctypes.byref(pt), ctypes.byref(conn), 1, ctypes.byref(need), 0)
            if not (okc and conn.value != 0):
                continue
            bat = ctypes.c_ubyte(0)
            okb = setupapi.SetupDiGetDevicePropertyW(h, ctypes.byref(did), ctypes.byref(_KEY_BT_BAT),
                                                     ctypes.byref(pt), ctypes.byref(bat), 1, ctypes.byref(need), 0)
            if okb and bat.value <= 100:
                dlog(f"[gamepad] BLE connected {nm!r} bat={bat.value}%")
                return Reading("gamepad", True, int(bat.value), False, "蓝牙 LE",
                               state="ok", detail="蓝牙 GATT 精确电量")
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(h)
    return None


class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [("wButtons", wt.WORD), ("bLeftTrigger", ctypes.c_ubyte), ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", wt.DWORD), ("Gamepad", XINPUT_GAMEPAD)]


class XINPUT_BATTERY_INFORMATION(ctypes.Structure):
    _fields_ = [("BatteryType", ctypes.c_ubyte), ("BatteryLevel", ctypes.c_ubyte)]


_XINPUT = None


def _get_xinput():
    global _XINPUT
    if _XINPUT is not None:
        return _XINPUT
    for name in ("xinput1_4.dll", "xinput1_3.dll"):
        try:
            xi = ctypes.WinDLL(name)
            xi.XInputGetState.argtypes = [wt.DWORD, ctypes.POINTER(XINPUT_STATE)]
            xi.XInputGetState.restype = wt.DWORD
            xi.XInputGetBatteryInformation.argtypes = [wt.DWORD, ctypes.c_ubyte, ctypes.POINTER(XINPUT_BATTERY_INFORMATION)]
            xi.XInputGetBatteryInformation.restype = wt.DWORD
            _XINPUT = xi
            return xi
        except OSError:
            pass
    return None


# XInput 硬件 4 档映射（与 Xbox Game Bar 一致）
_XINPUT_LEVEL_MAP = {0: (10, "极低"), 1: (25, "偏低"), 2: (70, "中等"), 3: (100, "满电")}


def read_xbox_elite2(cfg=None):
    # 1. 优先蓝牙 LE（精确百分比）
    ble = _read_xbox_ble()
    if ble:
        return ble

    # 2. Xbox 无线适配器 / USB 有线（XInput 1.4）
    xi = _get_xinput()
    if not xi:
        return Reading("gamepad", state="error", msg="系统缺少 xinput1_4.dll")
    for u in range(4):
        st = XINPUT_STATE()
        if xi.XInputGetState(u, ctypes.byref(st)) != 0:
            continue
        bi = XINPUT_BATTERY_INFORMATION()
        rc = xi.XInputGetBatteryInformation(u, 0, ctypes.byref(bi))
        if rc == 0 and bi.BatteryType == 0:
            time.sleep(0.12)            # 刚唤醒时给驱动一点时间更新电量包
            rc = xi.XInputGetBatteryInformation(u, 0, ctypes.byref(bi))
        dlog(f"[gamepad] XInput[{u}] rc={rc} type={bi.BatteryType} level={bi.BatteryLevel}")
        if rc != 0:
            continue
        if bi.BatteryType == 1:         # BATTERY_TYPE_WIRED（USB 有线/充电中）
            pct, tag = _XINPUT_LEVEL_MAP.get(bi.BatteryLevel, (100, "充电中"))
            if bi.BatteryLevel == 0:
                pct, tag = 100, "有线供电"
            return Reading("gamepad", True, pct, True, "USB 有线",
                           state="ok", detail=f"有线充电中 · {tag}", approx=True)
        if bi.BatteryType in (2, 3):    # ALKALINE / NIMH（无线电池）
            pct, tag = _XINPUT_LEVEL_MAP.get(bi.BatteryLevel, (70, "中等"))
            return Reading("gamepad", True, pct, False, f"Xbox 适配器 · {tag}",
                           state="ok", detail="XInput 4 档电量（蓝牙模式可显示精确%）", approx=True)
        return Reading("gamepad", True, 100, False, "Xbox 适配器",
                       state="ok", detail="已连接（等待电量汇报）", approx=True)
    return Reading("gamepad", state="absent", msg="手柄未开机或未连接")


READERS = {
    "mouse": read_viper,
    "headset": read_hecate_g3,
    "gamepad": read_xbox_elite2,
}


def read_all(cfg=None):
    """并行读取三台设备，互不阻塞，返回 {dev_id: Reading}。"""
    cfg = cfg or load_config()
    out = {}
    threads = []

    def run_one(dev_id, fn):
        try:
            out[dev_id] = fn(cfg)
        except Exception as e:
            out[dev_id] = Reading(dev_id, state="error", msg=f"读取异常：{e}")

    for d in DEVICES:
        t = threading.Thread(target=run_one, args=(d["id"], READERS[d["id"]]), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=8)
    for d in DEVICES:
        out.setdefault(d["id"], Reading(d["id"], state="error", msg="读取超时"))
    return out


# ════════════════════════════════════════════════════════════════════
#  主题 / DPI
# ════════════════════════════════════════════════════════════════════
def _reg_dword(path, name, default=None):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return default


PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


def taskbar_is_light():
    return _reg_dword(PERSONALIZE, "SystemUsesLightTheme", 0) == 1


def apps_are_light():
    return _reg_dword(PERSONALIZE, "AppsUseLightTheme", 1) == 1


def accent_color():
    v = _reg_dword(r"Software\Microsoft\Windows\DWM", "AccentColor")   # 0xAABBGGRR
    if v is None:
        return (0, 120, 212)
    return (v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF)


def set_dpi_aware():
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))     # Per-Monitor V2
        return
    except Exception:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


# Win11 24H2/25H2 系统任务栏配色（绿 = 实测任务栏电池同款 #9FD89F）
FLUENT = {
    "dark": {"fg": (255, 255, 255), "ok": (159, 216, 159), "warn": (252, 225, 0), "bad": (255, 153, 164)},
    "light": {"fg": (0, 0, 0), "ok": (15, 123, 15), "warn": (157, 93, 0), "bad": (196, 43, 28)},
}


# ════════════════════════════════════════════════════════════════════
#  图标渲染：纯 Python 超采样矢量光栅化 + GDI 文字（无 Pillow）
# ════════════════════════════════════════════════════════════════════
def _rrect(x0, y0, x1, y1, r):
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))

    def inside(x, y):
        if x < x0 or x > x1 or y < y0 or y > y1:
            return False
        cx = min(max(x, x0 + r), x1 - r)
        cy = min(max(y, y0 + r), y1 - r)
        dx, dy = x - cx, y - cy
        return dx * dx + dy * dy <= r * r
    return inside


def _circle(cx, cy, r):
    r2 = r * r
    return lambda x, y: (x - cx) ** 2 + (y - cy) ** 2 <= r2


def _capsule(x0, y0, x1, y1, r):
    dx, dy = x1 - x0, y1 - y0
    l2 = dx * dx + dy * dy + 1e-12
    r2 = r * r

    def inside(x, y):
        t = max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / l2))
        px, py = x0 + t * dx, y0 + t * dy
        return (x - px) ** 2 + (y - py) ** 2 <= r2
    return inside


def _arc_stroke(cx, cy, rad, sw, y_max=None):
    r_out2 = (rad + sw / 2) ** 2
    r_in2 = max(0.0, rad - sw / 2) ** 2

    def inside(x, y):
        if y_max is not None and y > y_max:
            return False
        d2 = (x - cx) ** 2 + (y - cy) ** 2
        return r_in2 <= d2 <= r_out2
    return inside


def _mouse_shape(x0, y0, x1, y1, r_top, r_bot):
    """Win11「鼠标设置」轮廓（Segoe Fluent Icons E962）：顶部小圆角 + 底部半圆。"""
    r_top = max(0.0, min(r_top, (x1 - x0) / 2, (y1 - y0) / 2))
    r_bot = max(0.0, min(r_bot, (x1 - x0) / 2, (y1 - y0) / 2))

    def inside(x, y):
        if x < x0 or x > x1 or y < y0 or y > y1:
            return False
        if y < y0 + r_top:
            cx = min(max(x, x0 + r_top), x1 - r_top)
            cy = y0 + r_top
            return (x - cx) ** 2 + (y - cy) ** 2 <= r_top ** 2
        if y > y1 - r_bot:
            cx = min(max(x, x0 + r_bot), x1 - r_bot)
            cy = y1 - r_bot
            return (x - cx) ** 2 + (y - cy) ** 2 <= r_bot ** 2
        return True
    return inside


def _mouse_ops(x0, y0, x1, y1, sw, fg):
    w = x1 - x0
    r_top = w * 0.25
    r_bot = w * 0.50
    cx = (x0 + x1) / 2
    wheel_len = (y1 - y0) * 0.34
    return [
        (_mouse_shape(x0, y0, x1, y1, r_top, r_bot), fg),
        (_mouse_shape(x0 + sw, y0 + sw, x1 - sw, y1 - sw, max(0.3, r_top - sw), max(0.3, r_bot - sw)), None),
        (_rrect(cx - sw / 2, y0, cx + sw / 2, y0 + wheel_len, sw * 0.25), fg),
    ]


def _headset_ops(x0, y0, x1, y1, sw, fg):
    """Win11 风格空心头戴耳机轮廓（Segoe Fluent Headphones）。"""
    w, h = x1 - x0, y1 - y0
    cx = (x0 + x1) / 2
    rad = w / 2 - sw / 2
    cy = y0 + rad + sw / 2
    cup_y0 = cy - h * 0.04
    cup_y1 = y1
    cup_w = max(sw * 2.25, w * 0.26)
    cr = cup_w * 0.48
    return [
        (_arc_stroke(cx, cy, rad, sw, y_max=cup_y0 + sw), fg),
        (_rrect(x0, cup_y0, x0 + cup_w, cup_y1, cr), fg),
        (_rrect(x0 + sw, cup_y0 + sw, x0 + cup_w - sw, cup_y1 - sw, max(0.2, cr - sw)), None),
        (_rrect(x1 - cup_w, cup_y0, x1, cup_y1, cr), fg),
        (_rrect(x1 - cup_w + sw, cup_y0 + sw, x1 - sw, cup_y1 - sw, max(0.2, cr - sw)), None),
    ]


def _gamepad_ops(x0, y0, x1, y1, sw, fg):
    """Win11 / Xbox 风格空心手柄轮廓（握把 + 十字键 + 右侧圆点）。"""
    w, h = x1 - x0, y1 - y0
    mid_y = y0 + h * 0.62
    r_sh = h * 0.30
    top_out = _rrect(x0 + w * 0.04, y0, x1 - w * 0.04, mid_y, r_sh)
    lg_out = _capsule(x0 + w * 0.21, y0 + h * 0.30, x0 + w * 0.14, y1 - h * 0.17, h * 0.17)
    rg_out = _capsule(x1 - w * 0.21, y0 + h * 0.30, x1 - w * 0.14, y1 - h * 0.17, h * 0.17)
    top_in = _rrect(x0 + w * 0.04 + sw, y0 + sw, x1 - w * 0.04 - sw, mid_y - sw, max(0.2, r_sh - sw))
    lg_in = _capsule(x0 + w * 0.21, y0 + h * 0.30, x0 + w * 0.14, y1 - h * 0.17, max(0.1, h * 0.17 - sw))
    rg_in = _capsule(x1 - w * 0.21, y0 + h * 0.30, x1 - w * 0.14, y1 - h * 0.17, max(0.1, h * 0.17 - sw))

    def body_out(x, y):
        return top_out(x, y) or lg_out(x, y) or rg_out(x, y)

    def body_in(x, y):
        return top_in(x, y) or lg_in(x, y) or rg_in(x, y)

    dcx, dcy = x0 + w * 0.31, y0 + h * 0.36
    dl = max(1.0, w * 0.095)
    dt = max(0.5, sw * 0.42)
    br = max(0.65, sw * 0.48)
    return [
        (body_out, fg),
        (body_in, None),
        (_rrect(dcx - dl, dcy - dt, dcx + dl, dcy + dt, dt * 0.4), fg),
        (_rrect(dcx - dt, dcy - dl, dcx + dt, dcy + dl, dt * 0.4), fg),
        (_circle(x1 - w * 0.26, y0 + h * 0.28, br), fg),
        (_circle(x1 - w * 0.35, y0 + h * 0.43, br), fg),
    ]


def _poly(pts):
    def inside(x, y):
        c = False
        n = len(pts)
        j = n - 1
        for i in range(n):
            xi, yi = pts[i]
            xj, yj = pts[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi:
                c = not c
            j = i
        return c
    return inside


def _bolt(cx, cy, h):
    w = h * 0.62
    pts = [(0.62, 0.00), (0.10, 0.56), (0.45, 0.56), (0.36, 1.00), (0.90, 0.42), (0.55, 0.42), (0.68, 0.00)]
    return _poly([(cx + (px - 0.5) * w, cy + (py - 0.5) * h) for px, py in pts])


def rasterize(W, H, ops, ss=4):
    out = []
    inv = 1.0 / ss
    offs = [(i + 0.5) * inv for i in range(ss)]
    n = ss * ss
    for py in range(H):
        for px in range(W):
            ar = ag = ab = aa = 0.0
            for oy in offs:
                y = py + oy
                for ox in offs:
                    x = px + ox
                    col = None
                    for shape, c in ops:
                        if shape(x, y):
                            col = c
                    if col is not None:
                        a = col[3] / 255.0
                        ar += col[0] * a
                        ag += col[1] * a
                        ab += col[2] * a
                        aa += a
            if aa <= 0:
                out.append((0, 0, 0, 0))
            else:
                out.append((int(ar / aa + .5), int(ag / aa + .5), int(ab / aa + .5), int(aa / n * 255 + .5)))
    return out


def level_color(level, charging, pal):
    if charging:
        return pal["ok"]
    if level is not None and level <= 20:
        return pal["bad"]
    if level is not None and level <= 30:
        return pal["warn"]
    return pal["fg"]


def _blend_over(S, base, over, ox, oy, ow, oh):
    res = list(base)
    for y in range(oh):
        ty = oy + y
        if not (0 <= ty < S):
            continue
        for x in range(ow):
            tx = ox + x
            if not (0 <= tx < S):
                continue
            sr, sg, sb, sa = over[y * ow + x]
            if sa == 0:
                continue
            dr, dg, db, da = res[ty * S + tx]
            a = sa / 255.0
            da_f = da / 255.0
            oa = a + da_f * (1 - a)
            if oa > 0:
                res[ty * S + tx] = (
                    int((sr * a + dr * da_f * (1 - a)) / oa + .5),
                    int((sg * a + dg * da_f * (1 - a)) / oa + .5),
                    int((sb * a + db * da_f * (1 - a)) / oa + .5),
                    int(oa * 255 + .5),
                )
    return res


# ── GDI 文字渲染 ─────────────────────────────────────────────────────
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD), ("yHotspot", wt.DWORD),
                ("hbmMask", wt.HBITMAP), ("hbmColor", wt.HBITMAP)]


user32.GetDC.restype = wt.HDC
user32.GetDC.argtypes = [wt.HWND]
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p), HANDLE, wt.DWORD]
gdi32.CreateBitmap.restype = wt.HBITMAP
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wt.UINT, wt.UINT, ctypes.c_void_p]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.CreateFontW.restype = wt.HFONT
gdi32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wt.DWORD] * 8 + [wt.LPCWSTR]
gdi32.SetTextColor.argtypes = [wt.HDC, wt.COLORREF]
gdi32.SetBkMode.argtypes = [wt.HDC, ctypes.c_int]
gdi32.GetTextExtentPoint32W.argtypes = [wt.HDC, wt.LPCWSTR, ctypes.c_int, ctypes.POINTER(wt.SIZE)]
gdi32.GdiFlush.argtypes = []
user32.DrawTextW.argtypes = [wt.HDC, wt.LPCWSTR, ctypes.c_int, ctypes.POINTER(wt.RECT), wt.UINT]
user32.CreateIconIndirect.restype = wt.HICON
user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.DestroyIcon.argtypes = [wt.HICON]


def _dib(W, H):
    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(bmi)
    bmi.biWidth, bmi.biHeight = W, -H
    bmi.biPlanes, bmi.biBitCount = 1, 32
    bits = ctypes.c_void_p()
    hdc = user32.GetDC(None)
    hbm = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    user32.ReleaseDC(None, hdc)
    return hbm, bits


def render_text_fit(W, H, text, col, max_h, alpha_mul=1.0, weight=600):
    hbm, bits = _dib(W, H)
    mdc = gdi32.CreateCompatibleDC(None)
    old = gdi32.SelectObject(mdc, hbm)
    ctypes.memset(bits, 0, W * H * 4)
    fh = max(7, int(round(max_h)))
    sz = wt.SIZE()
    font = of = None
    while fh >= 7:
        font = gdi32.CreateFontW(-fh, 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 4, 0, "Segoe UI Variable Text Semibold")
        of = gdi32.SelectObject(mdc, font)
        gdi32.GetTextExtentPoint32W(mdc, text, len(text), ctypes.byref(sz))
        if sz.cx <= W:
            break
        gdi32.SelectObject(mdc, of)
        gdi32.DeleteObject(font)
        fh -= 1
    else:
        font = gdi32.CreateFontW(-7, 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 4, 0, "Segoe UI Variable Text Semibold")
        of = gdi32.SelectObject(mdc, font)
    gdi32.SetTextColor(mdc, 0xFFFFFF)
    gdi32.SetBkMode(mdc, 1)
    rc = wt.RECT(0, 0, W, H)
    user32.DrawTextW(mdc, text, -1, ctypes.byref(rc), 0x25 | 0x100)
    gdi32.GdiFlush()
    raw = ctypes.string_at(bits, W * H * 4)
    gdi32.SelectObject(mdc, of)
    gdi32.DeleteObject(font)
    gdi32.SelectObject(mdc, old)
    gdi32.DeleteDC(mdc)
    gdi32.DeleteObject(hbm)
    out = []
    for i in range(W * H):
        b, g, r, _ = raw[i * 4:i * 4 + 4]
        a = int(max(r, g, b) * alpha_mul)
        out.append(col + (a,) if a else (0, 0, 0, 0))
    return out


# ── 各设备图标渲染 ───────────────────────────────────────────────────
def draw_device_full(dev_id, S, light, dim=False):
    """纯大号 Win11 空心设备图标（鼠标 / 耳机 / 手柄）。"""
    pal = FLUENT["light" if light else "dark"]
    fg = pal["fg"] + (115 if dim else 255,)
    p = S / 24.0
    if dev_id == "mouse":
        ops = _mouse_ops(3.8 * p, 1.0 * p, 20.2 * p, 23.0 * p, max(1.15, 1.8 * p), fg)
    elif dev_id == "headset":
        ops = _headset_ops(2.0 * p, 2.5 * p, 22.0 * p, 21.5 * p, max(1.15, 1.8 * p), fg)
    else:
        ops = _gamepad_ops(1.0 * p, 4.5 * p, 23.0 * p, 19.5 * p, max(1.1, 1.7 * p), fg)
    return rasterize(S, S, ops, 4 if S <= 32 else 3)


def draw_dev_pct_icon(dev_id, S, level, charging, light, dim=False):
    """默认样式：上方精致 Win11 设备轮廓（充电时附带浅绿闪电）+ 下方清晰百分比。"""
    pal = FLUENT["light" if light else "dark"]
    a = 115 if dim else 255
    fg = pal["fg"] + (a,)
    col_rgb = level_color(level, charging, pal)
    p = S / 24.0
    if dev_id == "mouse":
        ops = _mouse_ops(7.8 * p, 0.4 * p, 16.2 * p, 11.8 * p, max(0.95, 1.25 * p), fg)
        bolt_x = 19.6 * p
    elif dev_id == "headset":
        ops = _headset_ops(6.0 * p, 0.6 * p, 18.0 * p, 11.5 * p, max(0.95, 1.32 * p), fg)
        bolt_x = 20.8 * p
    else:
        ops = _gamepad_ops(4.2 * p, 0.8 * p, 19.8 * p, 11.6 * p, max(0.95, 1.28 * p), fg)
        bolt_x = 21.6 * p
    if charging and not dim:
        ops.append((_bolt(bolt_x, 5.8 * p, 8.0 * p), pal["ok"] + (255,)))
    base = rasterize(S, S, ops, 4 if S <= 32 else 3)
    top_h = int(round(12 * p))
    txt_h = S - top_h
    txt_s = "--" if level is None else f"{level}%"
    txt = render_text_fit(S, txt_h, txt_s, col_rgb, max(8, round(12.2 * p)), 0.45 if dim else 1.0)
    return _blend_over(S, base, txt, 0, top_h, S, txt_h)


def draw_bt_single_icon(dev_id, S, level, charging, light, dim=False):
    """蓝牙耳机风格：左 Win11 空心设备轮廓 + 右竖向电池条。"""
    pal = FLUENT["light" if light else "dark"]
    a = 115 if dim else 255
    fg = pal["fg"] + (a,)
    col = level_color(level, charging, pal) + (a,)
    p = S / 24.0
    if dev_id == "mouse":
        ops = _mouse_ops(0.6 * p, 1.8 * p, 13.8 * p, 22.2 * p, max(1.05, 1.6 * p), fg)
        bx0, bx1 = 16.0 * p, 23.4 * p
    elif dev_id == "headset":
        ops = _headset_ops(0.5 * p, 4.0 * p, 14.5 * p, 20.5 * p, max(1.0, 1.5 * p), fg)
        bx0, bx1 = 16.4 * p, 23.5 * p
    else:
        ops = _gamepad_ops(0.2 * p, 5.8 * p, 15.0 * p, 18.4 * p, max(0.95, 1.35 * p), fg)
        bx0, bx1 = 16.6 * p, 23.6 * p
    by0, by1 = 4.5 * p, 21.6 * p
    bsw = max(1.0, 1.3 * p)
    ops.append((_rrect((bx0 + bx1) / 2 - 1.7 * p, by0 - 1.8 * p, (bx0 + bx1) / 2 + 1.7 * p, by0 + 0.5 * p, 0.7 * p), fg))
    ops.append((_rrect(bx0, by0, bx1, by1, 1.6 * p), fg))
    ops.append((_rrect(bx0 + bsw, by0 + bsw, bx1 - bsw, by1 - bsw, max(0.3, 0.7 * p)), None))
    gap = max(0.6, 0.75 * p)
    fx0, fx1, fy0, fy1 = bx0 + bsw + gap, bx1 - bsw - gap, by0 + bsw + gap, by1 - bsw - gap
    if level is not None and level > 0 and fy1 > fy0:
        ft = min(fy1 - max(1.0, 1.4 * p), fy1 - (fy1 - fy0) * level / 100.0)
        ops.append((_rrect(fx0, ft, fx1, fy1, 0.5 * p), col))
    if charging:
        cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
        ops.append((_bolt(cx, cy, 10.2 * p), None))
        ops.append((_bolt(cx, cy, 7.6 * p), fg))
    return rasterize(S, S, ops, 4 if S <= 32 else 3)


def draw_battery_pct_icon(S, level, charging, light, dim=False):
    """上方 Win11 同款电池（#9FD89F 充电绿 + 跨顶边闪电）+ 下方清晰百分比。"""
    pal = FLUENT["light" if light else "dark"]
    a = 115 if dim else 255
    fg = pal["fg"] + (a,)
    col_rgb = level_color(level, charging, pal)
    col = col_rgb + (a,)
    p = S / 24.0
    sw = max(1.0, 1.5 * p)
    x0, x1, y0, y1 = 1.8 * p, 20.6 * p, 1.2 * p, 10.6 * p
    ops = [
        (_rrect(x0, y0, x1, y1, 2.3 * p), fg),
        (_rrect(x0 + sw, y0 + sw, x1 - sw, y1 - sw, max(0.4, 2.3 * p - sw)), None),
        (_rrect(x1 - 0.5 * p, (y0 + y1) / 2 - 1.8 * p, x1 + 1.7 * p, (y0 + y1) / 2 + 1.8 * p, 0.8 * p), fg),
    ]
    gap = max(0.6, 1.0 * p)
    fx0, fx1, fy0, fy1 = x0 + sw + gap, x1 - sw - gap, y0 + sw + gap, y1 - sw - gap
    if level is not None and level > 0 and fx1 > fx0:
        fw = max(max(1.2, 1.6 * p), (fx1 - fx0) * level / 100.0)
        ops.append((_rrect(fx0, fy0, fx0 + fw, fy1, 0.8 * p), col))
    if charging:
        cx = (x0 + x1) / 2
        cy = y0 + (y1 - y0) * 0.34
        ops.append((_bolt(cx, cy, 10.4 * p), None))
        ops.append((_bolt(cx, cy, 7.8 * p), fg))
    base = rasterize(S, S, ops, 4 if S <= 32 else 3)
    top_h = int(round(11 * p))
    txt_h = S - top_h
    txt_s = "--" if level is None else f"{level}%"
    txt = render_text_fit(S, txt_h, txt_s, col_rgb, max(8, round(12.2 * p)), 0.45 if dim else 1.0)
    return _blend_over(S, base, txt, 0, top_h, S, txt_h)


def draw_pct_only_icon(S, level, charging, light, dim=False):
    pal = FLUENT["light" if light else "dark"]
    col = level_color(level, charging, pal)
    txt_s = "--" if level is None else f"{level}%"
    return render_text_fit(S, S, txt_s, col, round(S * 0.60), 0.45 if dim else 1.0)


def draw_horizontal_battery(W, H, level, charging, light, dim=False):
    """面板内联 Win11 系统同款横向电池。"""
    pal = FLUENT["light" if light else "dark"]
    a = 115 if dim else 255
    fg = pal["fg"] + (a,)
    col = level_color(level, charging, pal) + (a,)
    p = H / 16.0
    sw = max(1.1, 1.45 * p)
    x0, x1 = 1.0 * p, W - 3.2 * p
    y0, y1 = 2.5 * p, H - 2.5 * p
    ops = [
        (_rrect(x0, y0, x1, y1, 2.8 * p), fg),
        (_rrect(x0 + sw, y0 + sw, x1 - sw, y1 - sw, max(0.5, 2.8 * p - sw)), None),
        (_rrect(x1 - 0.4 * p, (y0 + y1) / 2 - 2.2 * p, x1 + 2.0 * p, (y0 + y1) / 2 + 2.2 * p, 0.9 * p), fg),
    ]
    gap = max(0.8, 1.1 * p)
    fx0, fx1, fy0, fy1 = x0 + sw + gap, x1 - sw - gap, y0 + sw + gap, y1 - sw - gap
    if level is not None and level > 0 and fx1 > fx0:
        fw = max(sw, (fx1 - fx0) * level / 100.0)
        ops.append((_rrect(fx0, fy0, fx0 + fw, fy1, 1.0 * p), col))
    if charging:
        cx = (x0 + x1) / 2
        cy = y0 + (y1 - y0) * 0.30
        bh = (y1 - y0) * 1.15
        ops.append((_bolt(cx, cy, bh + 2.6 * p), None))
        ops.append((_bolt(cx, cy, bh), fg))
    return rasterize(W, H, ops, 3)


def pixels_to_hicon(S, px):
    hbm, bits = _dib(S, S)
    data = bytearray(S * S * 4)
    for i, (r, g, b, a) in enumerate(px):
        pr = (r * a + 127) // 255
        pg = (g * a + 127) // 255
        pb = (b * a + 127) // 255
        data[i * 4:i * 4 + 4] = bytes((pb, pg, pr, a))
    ctypes.memmove(bits, bytes(data), len(data))
    row = ((S + 15) // 16) * 2
    mask_bits = ctypes.create_string_buffer(row * S)
    hmask = gdi32.CreateBitmap(S, S, 1, 1, mask_bits)
    ii = ICONINFO(True, 0, 0, hmask, hbm)
    hicon = user32.CreateIconIndirect(ctypes.byref(ii))
    gdi32.DeleteObject(hmask)
    gdi32.DeleteObject(hbm)
    return hicon


def png_bytes(W, H, px):
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        for x in range(W):
            raw += bytes(px[y * W + x])

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def write_app_icon(path=ICON_PATH):
    imgs = []
    for S in (16, 24, 32, 48, 64, 128):
        p = S / 24.0
        fg = (255, 255, 255, 255)
        grn = (159, 216, 159, 255)
        bg = (36, 36, 36, 255)
        sw = max(1.0, 1.5 * p)
        ops = [(_rrect(0.5 * p, 0.5 * p, 23.5 * p, 23.5 * p, 5.2 * p), bg)]
        ops += _mouse_ops(3.4 * p, 4.2 * p, 13.8 * p, 19.8 * p, sw, fg)
        bx0, bx1, by0, by1 = 15.6 * p, 20.8 * p, 6.2 * p, 19.2 * p
        bsw = max(0.9, 1.15 * p)
        ops.append((_rrect((bx0 + bx1) / 2 - 1.3 * p, by0 - 1.4 * p, (bx0 + bx1) / 2 + 1.3 * p, by0 + 0.4 * p, 0.5 * p), fg))
        ops.append((_rrect(bx0, by0, bx1, by1, 1.3 * p), fg))
        ops.append((_rrect(bx0 + bsw, by0 + bsw, bx1 - bsw, by1 - bsw, 0.5 * p), bg))
        ops.append((_rrect(bx0 + bsw + 0.6 * p, by0 + (by1 - by0) * 0.25, bx1 - bsw - 0.6 * p, by1 - bsw - 0.6 * p, 0.4 * p), grn))
        px = rasterize(S, S, ops, 4 if S <= 32 else 2)
        imgs.append((S, png_bytes(S, S, px)))
    head = struct.pack("<HHH", 0, 1, len(imgs))
    off = 6 + 16 * len(imgs)
    dirs, blobs = b"", b""
    for S, data in imgs:
        dirs += struct.pack("<BBBBHHII", S % 256, S % 256, 0, 0, 1, 32, len(data), off)
        off += len(data)
        blobs += data
    with open(path, "wb") as f:
        f.write(head + dirs + blobs)


# ════════════════════════════════════════════════════════════════════
#  开机自启（「启动」文件夹 .lnk，纯 ctypes COM）+ 桌面链接
# ════════════════════════════════════════════════════════════════════
CLSID_ShellLink = make_guid("{00021401-0000-0000-C000-000000000046}")
IID_IShellLinkW = make_guid("{000214F9-0000-0000-C000-000000000046}")
IID_IPersistFile = make_guid("{0000010B-0000-0000-C000-000000000046}")
ole32.CoInitialize.argtypes = [ctypes.c_void_p]
ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wt.DWORD,
                                   ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
ole32.CoCreateInstance.restype = ctypes.c_long
shell32.SHGetFolderPathW.argtypes = [wt.HWND, ctypes.c_int, HANDLE, wt.DWORD, wt.LPWSTR]


def _vcall(obj, idx, argtypes, *args):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[idx])
    return fn(obj, *args)


def _release(obj):
    if obj:
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(wt.ULONG, ctypes.c_void_p)(vtbl[2])(obj)


def shell_folder(csidl):
    buf = ctypes.create_unicode_buffer(260)
    shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    return buf.value


def startup_dir():
    return shell_folder(7)


def desktop_dir():
    return shell_folder(0x10)


def startup_lnk():
    return os.path.join(startup_dir(), APP_NAME + ".lnk")


def pythonw_exe():
    exe = sys.executable
    d = os.path.dirname(exe)
    cand = os.path.join(d, "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def self_cmd(*extra_args):
    """返回启动自身子进程（面板 / 调试窗口）的命令行，兼容源码和 PyInstaller 单文件 exe。"""
    if FROZEN:
        return [SCRIPT, *extra_args]
    return [pythonw_exe(), SCRIPT, *extra_args]


def self_lnk_target():
    """返回快捷方式 (target, args, icon)。"""
    if FROZEN:
        return SCRIPT, "", SCRIPT
    ico = ICON_PATH if os.path.exists(ICON_PATH) else ""
    return pythonw_exe(), f'"{SCRIPT}"', ico


def create_lnk(lnk, target, args="", workdir="", desc="", icon=""):
    ole32.CoInitialize(None)
    psl = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(CLSID_ShellLink), None, 1,
                                ctypes.byref(IID_IShellLinkW), ctypes.byref(psl))
    if hr < 0:
        raise OSError(f"CoCreateInstance 失败 0x{hr & 0xFFFFFFFF:08X}")
    ppf = ctypes.c_void_p()
    try:
        W = wt.LPCWSTR
        _vcall(psl, 20, [W], target)
        _vcall(psl, 11, [W], args)
        _vcall(psl, 9, [W], workdir)
        _vcall(psl, 7, [W], desc)
        if icon:
            _vcall(psl, 17, [W, ctypes.c_int], icon, 0)
        hr = _vcall(psl, 0, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                    ctypes.byref(IID_IPersistFile), ctypes.byref(ppf))
        if hr < 0:
            raise OSError("QueryInterface(IPersistFile) 失败")
        hr = _vcall(ppf, 6, [W, wt.BOOL], lnk, True)
        if hr < 0:
            raise OSError(f"保存快捷方式失败 0x{hr & 0xFFFFFFFF:08X}")
    finally:
        _release(ppf)
        _release(psl)


STARTUP_APPROVED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder"


def _approved_get(name):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_APPROVED) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def _approved_set(name, enabled):
    import winreg
    if enabled:
        data = b"\x02" + b"\x00" * 11
    else:
        ft = int((time.time() + 11644473600) * 10_000_000)
        data = b"\x03\x00\x00\x00" + struct.pack("<Q", ft)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, STARTUP_APPROVED) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_BINARY, data)


def autostart_enabled():
    if not os.path.exists(startup_lnk()):
        return False
    v = _approved_get(APP_NAME + ".lnk")
    return not (v and len(v) and (v[0] & 1))


def set_autostart(on):
    lnk = startup_lnk()
    name = APP_NAME + ".lnk"
    if on:
        if not FROZEN and not os.path.exists(ICON_PATH):
            try:
                write_app_icon()
            except Exception:
                pass
        target, args, ico = self_lnk_target()
        create_lnk(lnk, target, args, HERE, "无线外设电量托盘", ico)
        if _approved_get(name) is not None:
            _approved_set(name, True)
    else:
        if os.path.exists(lnk):
            _approved_set(name, False)


def make_desktop_links():
    desk = desktop_dir()
    msgs = []
    dlink = os.path.join(desk, APP_NAME)
    if not os.path.lexists(dlink):
        try:
            os.symlink(HERE, dlink, target_is_directory=True)
            msgs.append(f"目录符号链接：{dlink}")
        except OSError:
            r = subprocess.run(["cmd", "/c", "mklink", "/J", dlink, HERE],
                               capture_output=True, creationflags=0x08000000)
            msgs.append(f"目录 junction：{dlink}" if r.returncode == 0 else "目录链接创建失败")
    else:
        msgs.append(f"已存在：{dlink}")

    flink = os.path.join(desk, os.path.basename(LAUNCHER))
    flnk = os.path.join(desk, "启动电量托盘.lnk")
    if FROZEN:
        if not os.path.lexists(flnk):
            target, args, ico = self_lnk_target()
            create_lnk(flnk, target, args, HERE, "启动无线外设电量托盘", ico)
            msgs.append(f"桌面快捷方式：{flnk}")
        else:
            msgs.append(f"已存在：{flnk}")
    elif not os.path.lexists(flink) and not os.path.lexists(flnk):
        try:
            os.symlink(LAUNCHER, flink)
            msgs.append(f"文件符号链接：{flink}")
        except OSError:
            try:
                os.link(LAUNCHER, flink)
                msgs.append(f"文件硬链接：{flink}")
            except OSError:
                target, args, ico = self_lnk_target()
                create_lnk(flnk, target, args, HERE, "启动无线外设电量托盘", ico)
                msgs.append(f"跨盘启动快捷方式：{flnk}")
    else:
        msgs.append(f"已存在：{flink if os.path.lexists(flink) else flnk}")
    return msgs


def remove_desktop_links():
    desk = desktop_dir()
    for p in (os.path.join(desk, APP_NAME),
              os.path.join(desk, os.path.basename(LAUNCHER)),
              os.path.join(desk, "启动电量托盘.lnk")):
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                os.rmdir(p)
            elif os.path.lexists(p):
                os.unlink(p)
        except OSError:
            pass


# ════════════════════════════════════════════════════════════════════
#  托盘（纯 Win32，三设备独立托盘图标，SetTimer 驱动，后台并行读取）
# ════════════════════════════════════════════════════════════════════
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wt.HICON)]


user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SetTimer.restype = ctypes.c_size_t
user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p]
user32.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]
user32.FindWindowW.restype = wt.HWND
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.IsIconic.argtypes = [wt.HWND]
user32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
user32.RegisterWindowMessageW.restype = wt.UINT
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.AllowSetForegroundWindow.argtypes = [wt.DWORD]
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]

WM_DESTROY, WM_CLOSE, WM_TIMER, WM_SETTINGCHANGE = 0x0002, 0x0010, 0x0113, 0x001A
WM_DEVICECHANGE, WM_DPICHANGED, WM_ENDSESSION = 0x0219, 0x02E0, 0x0016
WM_LBUTTONUP, WM_RBUTTONUP, WM_APP = 0x0202, 0x0205, 0x8000
WM_TRAY = WM_APP + 1
WM_READ_DONE = WM_APP + 2
WM_RELOAD = WM_APP + 3
WM_REFRESH = WM_APP + 4
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 1, 2, 4, 0x10
TIMER_POLL, TIMER_DEVCHANGE, TIMER_RETRY = 1, 2, 3


def find_tray_hwnd():
    return user32.FindWindowW(TRAY_CLASS, None)


def bring_or_launch_panel():
    hw = user32.FindWindowW(None, PANEL_TITLE)
    if hw:
        if user32.IsIconic(hw):
            user32.ShowWindow(hw, 9)
        user32.SetForegroundWindow(hw)
        return
    user32.AllowSetForegroundWindow(0xFFFFFFFF)
    subprocess.Popen(self_cmd("--panel"), cwd=HERE, close_fds=True,
                     creationflags=0x08000000)


class Tray:
    def __init__(self):
        self.cfg = load_config()
        self.hwnd = None
        self.hicons = {}          # uid -> HICON
        self.added = set()        # 已成功 NIM_ADD 的 uid
        self.last = {}            # dev_id -> 最近一次成功读数
        self.latest = {}          # dev_id -> 最近一次读数
        self.reading = False
        self.result_q = queue.Queue()
        self.low_alerted = set()  # 已发过低电提醒的 dev_id
        self.retry_left = 15
        self._wndproc = WNDPROC(self._proc)
        self.msg_taskbar = user32.RegisterWindowMessageW("TaskbarCreated")

    def create(self):
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = TRAY_CLASS
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, TRAY_CLASS, APP_NAME, 0, 0, 0, 0, 0, None, None, hinst, None)
        self.update_icons()
        self.set_timer()
        self.start_read()

    def icon_size(self):
        try:
            dpi = user32.GetDpiForWindow(self.hwnd) or user32.GetDpiForSystem()
            return user32.GetSystemMetricsForDpi(49, dpi)
        except Exception:
            return user32.GetSystemMetrics(49) or 16

    def nid(self, uid, flags):
        n = NOTIFYICONDATAW()
        n.cbSize = ctypes.sizeof(n)
        n.hWnd = self.hwnd
        n.uID = uid
        n.uFlags = flags
        n.uCallbackMessage = WM_TRAY
        return n

    def _pick_lowest_dev(self):
        """从当前在线设备里挑出电量最低的（同电量时优先未充电的）；全离线则兜底最近读数最低或鼠标。"""
        enabled = self.cfg.get("enabled", {})
        online = []
        for idx, d in enumerate(DEVICES):
            did = d["id"]
            if not enabled.get(did, True):
                continue
            r = self.latest.get(did)
            if r is not None and r.ok and r.level is not None:
                online.append((r.level, 1 if r.charging else 0, idx, did))
        if online:
            online.sort()
            return online[0][3]
        cached = []
        for idx, d in enumerate(DEVICES):
            did = d["id"]
            if not enabled.get(did, True):
                continue
            last = self.last.get(did)
            if last is not None and last.level is not None:
                cached.append((last.level, 1 if last.charging else 0, idx, did))
        if cached:
            cached.sort()
            return cached[0][3]
        return "mouse"

    def tooltip_summary(self, active_id):
        """单图标模式下悬停一次查看全部三台设备电量，当前显示的最低电量设备标 ▶。"""
        lines = []
        latest_t = None
        for d in DEVICES:
            did = d["id"]
            r = self.latest.get(did)
            mark = "▶ " if did == active_id else ""
            if r is not None and r.ok:
                p = "~" if r.approx else ""
                ch = "⚡" if r.charging else ""
                lines.append(f"{mark}{d['short']}：{p}{r.level}%{ch}")
                if latest_t is None or r.time > latest_t:
                    latest_t = r.time
            elif r is not None:
                st = "休眠" if r.state == "sleep" else "未连接"
                lines.append(f"{mark}{d['short']}：{st}")
            else:
                lines.append(f"{mark}{d['short']}：读取中…")
        if latest_t:
            lines.append(f"更新于 {latest_t.strftime('%H:%M')}")
        return "\n".join(lines)[:127]

    def tooltip_for(self, dev_id):
        if self.cfg.get("tray_mode", "lowest") == "lowest":
            return self.tooltip_summary(dev_id)
        name = DEV_BY_ID[dev_id]["name"]
        r = self.latest.get(dev_id)
        last = self.last.get(dev_id)
        if last is None:
            return f"{name}：{r.msg if r else '读取中…'}"[:127]
        t = last.time.strftime("%H:%M")
        prefix = "约 " if last.approx else ""
        s = f"{name}：{prefix}{last.level}%"
        s += "（充电中）" if last.charging else ""
        s += f"\n{last.source} · 更新于 {t}"
        if r is not None and not r.ok:
            s += f"\n{r.msg}"
        return s[:127]

    def _delete_uid(self, uid):
        if uid in self.added:
            n = self.nid(uid, 0)
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(n))
            self.added.discard(uid)
        old = self.hicons.pop(uid, None)
        if old:
            user32.DestroyIcon(old)

    def _desired_icons(self):
        """返回 [(uid, dev_id, kind)]；lowest 模式严格只显示 1 个图标（uID=11）。"""
        style = self.cfg["style"]
        if self.cfg.get("tray_mode", "lowest") == "lowest":
            return [(11, self._pick_lowest_dev(), style)]
        enabled = self.cfg.get("enabled", {})
        specs = []
        # 倒序添加，使视觉上从左到右为：鼠标 → 耳机 → 手柄
        for d in reversed(DEVICES):
            dev_id = d["id"]
            if not enabled.get(dev_id, True):
                continue
            specs.append((d["uid_base"] + 1, dev_id, style))
        if not specs:
            specs.append((11, "mouse", style))
        return specs

    def update_icons(self):
        S = self.icon_size()
        light = taskbar_is_light()
        specs = self._desired_icons()
        want_uids = {uid for uid, _, _ in specs}

        for uid in list(self.added):
            if uid not in want_uids:
                self._delete_uid(uid)

        for uid, dev_id, kind in specs:
            r = self.latest.get(dev_id)
            last = self.last.get(dev_id)
            # 耳机/手柄离线时不沿用陈旧电量，直接显示离线 --；鼠标短暂休眠时保留最近读数但变淡
            if r is not None and not r.ok and (dev_id != "mouse" or r.state in ("absent", "error")):
                level, charging, dim = None, False, True
            else:
                level = last.level if last else None
                charging = bool(last and last.charging)
                dim = (last is None) or (r is not None and not r.ok)

            if kind == "dev_full":
                px = draw_device_full(dev_id, S, light, dim)
            elif kind == "bat_pct":
                px = draw_battery_pct_icon(S, level, charging, light, dim)
            elif kind == "bt_single":
                px = draw_bt_single_icon(dev_id, S, level, charging, light, dim)
            elif kind == "pct_only":
                px = draw_pct_only_icon(S, level, charging, light, dim)
            else:
                px = draw_dev_pct_icon(dev_id, S, level, charging, light, dim)

            new_ic = pixels_to_hicon(S, px)
            n = self.nid(uid, NIF_MESSAGE | NIF_ICON | NIF_TIP)
            n.hIcon = new_ic
            n.szTip = self.tooltip_for(dev_id)
            if uid not in self.added:
                if shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(n)):
                    self.added.add(uid)
            else:
                if not shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(n)):
                    if shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(n)):
                        self.added.add(uid)
                    else:
                        self.added.discard(uid)
            old = self.hicons.get(uid)
            if old:
                user32.DestroyIcon(old)
            self.hicons[uid] = new_ic

        if len(self.added) < len(specs) and self.retry_left > 0:
            user32.SetTimer(self.hwnd, TIMER_RETRY, 2000, None)
        else:
            user32.KillTimer(self.hwnd, TIMER_RETRY)

    def balloon(self, uid, title, text):
        if uid not in self.added:
            if not self.added:
                return
            uid = next(iter(self.added))
        n = self.nid(uid, NIF_INFO)
        n.szInfoTitle = title[:63]
        n.szInfo = text[:255]
        n.dwInfoFlags = 2
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(n))

    def set_timer(self):
        user32.SetTimer(self.hwnd, TIMER_POLL, int(self.cfg["interval"]) * 1000, None)

    def start_read(self):
        if self.reading:
            return
        self.reading = True
        cfg = dict(self.cfg)

        def work():
            res = read_all(cfg)
            self.result_q.put(res)
            user32.PostMessageW(self.hwnd, WM_READ_DONE, 0, 0)
        threading.Thread(target=work, daemon=True).start()

    def on_read_done(self):
        self.reading = False
        try:
            res = self.result_q.get_nowait()
        except queue.Empty:
            return
        thr = int(self.cfg.get("low_alert", 0) or 0)
        for dev_id, r in res.items():
            self.latest[dev_id] = r
            dlog(f"[{dev_id}]", r.state, r.level, r.charging, r.source, r.msg)
            if r.ok:
                self.last[dev_id] = r
                if thr and not r.charging and r.level <= thr:
                    if dev_id not in self.low_alerted:
                        self.low_alerted.add(dev_id)
                        d = DEV_BY_ID[dev_id]
                        self.balloon(d["uid_base"] + 1, "外设电量低", f"{d['name']} 剩余 {r.level}%，请及时充电")
                elif r.charging or r.level > thr + 5:
                    self.low_alerted.discard(dev_id)
        self.update_icons()

    def _proc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_TRAY:
                if lp == WM_LBUTTONUP:
                    self.start_read()
                elif lp == WM_RBUTTONUP:
                    bring_or_launch_panel()
                return 0
            if msg == WM_READ_DONE:
                self.on_read_done()
                return 0
            if msg == WM_TIMER:
                if wp == TIMER_POLL:
                    self.start_read()
                elif wp == TIMER_DEVCHANGE:
                    user32.KillTimer(hwnd, TIMER_DEVCHANGE)
                    self.start_read()
                elif wp == TIMER_RETRY:
                    user32.KillTimer(hwnd, TIMER_RETRY)
                    self.retry_left -= 1
                    self.update_icons()
                return 0
            if msg == WM_DEVICECHANGE:
                user32.SetTimer(hwnd, TIMER_DEVCHANGE, 2000, None)
                return 1
            if msg in (WM_SETTINGCHANGE, WM_DPICHANGED):
                self.update_icons()
                return 0
            if msg == WM_RELOAD:
                self.cfg = load_config()
                self.set_timer()
                self.update_icons()
                self.start_read()
                return 0
            if msg == WM_REFRESH:
                self.start_read()
                return 0
            if msg == self.msg_taskbar and msg:
                self.added.clear()
                self.update_icons()
                return 0
            if msg in (WM_CLOSE, WM_ENDSESSION) and (msg == WM_CLOSE or wp):
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.KillTimer(hwnd, TIMER_POLL)
                user32.KillTimer(hwnd, TIMER_DEVCHANGE)
                user32.KillTimer(hwnd, TIMER_RETRY)
                for uid in list(self.added | set(self.hicons)):
                    self._delete_uid(uid)
                user32.PostQuitMessage(0)
                return 0
        except Exception as e:
            dlog("WndProc 异常", e)
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def run(self):
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


def run_tray():
    mtx = kernel32.CreateMutexW(None, True, "Local\\RazerViperBatteryTray_Single")
    if ctypes.get_last_error() == 183:
        hw = find_tray_hwnd()
        if hw:
            user32.PostMessageW(hw, WM_REFRESH, 0, 0)
            return 0
        dlog("单实例锁被占用，但找不到托盘窗口")
        return 1
    set_dpi_aware()
    t = Tray()
    t.create()
    t.run()
    kernel32.CloseHandle(mtx)
    return 0


# ════════════════════════════════════════════════════════════════════
#  简洁面板（tkinter，Win11 蓝牙设备三卡片弹出面板）
# ════════════════════════════════════════════════════════════════════
def _hex(c):
    return "#%02x%02x%02x" % c


def _mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def fluent_icon_font(root):
    from tkinter import font as tkfont
    fams = set(tkfont.families(root))
    for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets"):
        if f in fams:
            return f
    return None


def ui_font(root, display=False):
    from tkinter import font as tkfont
    fams = set(tkfont.families(root))
    for f in (("Segoe UI Variable Display" if display else "Segoe UI Variable Text"), "Segoe UI", "Microsoft YaHei UI"):
        if f in fams:
            return f
    return "TkDefaultFont"


def dwm_style(hwnd, dark, rounded=True):
    try:
        dwm = ctypes.WinDLL("dwmapi")
        v = ctypes.c_int(1 if dark else 0)
        dwm.DwmSetWindowAttribute(wt.HWND(hwnd), 20, ctypes.byref(v), 4)
        if rounded:
            v = ctypes.c_int(2)
            dwm.DwmSetWindowAttribute(wt.HWND(hwnd), 33, ctypes.byref(v), 4)
    except Exception:
        pass


def work_area():
    rc = wt.RECT()
    user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rc), 0)
    return rc.left, rc.top, rc.right, rc.bottom


def _rgba_to_photo(tk, W, H, px, bg_hex):
    bg = tuple(int(bg_hex[i:i + 2], 16) for i in (1, 3, 5))
    img = tk.PhotoImage(width=W, height=H)
    rows = []
    for y in range(H):
        row = []
        for x in range(W):
            pr, pg, pb, pa = px[y * W + x]
            t = pa / 255.0
            row.append(_hex(_mix(bg, (pr, pg, pb), t)))
        rows.append("{" + " ".join(row) + "}")
    img.put(" ".join(rows))
    return img


class Panel:
    def __init__(self):
        import tkinter as tk
        self.tk = tk
        set_dpi_aware()
        self.cfg = load_config()
        self.dark = not apps_are_light()
        self.root = root = tk.Tk()
        root.title(PANEL_TITLE)
        root.withdraw()
        self.scale = root.winfo_fpixels("1i") / 96.0
        self.q = queue.Queue()
        self.reading = False
        self.readings = {}
        self.cards = {}
        self._had_focus = False
        self._imgs = []
        self.icon_font = fluent_icon_font(root)
        self.f_text = ui_font(root)
        self.f_disp = ui_font(root, True)
        self.colors()
        self.build()
        self.place()
        root.deiconify()
        root.after(10, self.after_map)
        self.refresh()
        root.after(100, self.pump)

    def px(self, v):
        return int(round(v * self.scale))

    def colors(self):
        if self.dark:
            self.c = dict(bg="#202020", card="#2b2b2b", chip="#343434", border="#3a3a3a",
                          text="#ffffff", sub="#c5c5c5", mute="#9a9a9a", track="#4a4a4a",
                          btn="#373737", ok="#9fd89f", warn="#fce100", bad="#ff99a4")
        else:
            self.c = dict(bg="#f3f3f3", card="#fbfbfb", chip="#efefef", border="#e5e5e5",
                          text="#1a1a1a", sub="#5d5d5d", mute="#8a8a8a", track="#d6d6d6",
                          btn="#fdfdfd", ok="#0f7b0f", warn="#9d5d00", bad="#c42b1c")
        acc = accent_color()
        if self.dark:
            acc = _mix(acc, (255, 255, 255), 0.35)
        self.c["accent"] = _hex(acc)
        self.c["on_accent"] = "#000000" if self.dark else "#ffffff"

    def build(self):
        tk, c, root = self.tk, self.c, self.root
        root.configure(bg=c["border"])
        outer = tk.Frame(root, bg=c["bg"])
        outer.pack(fill="both", expand=True, padx=1, pady=1)
        pad = self.px(16)

        # 标题行
        top = tk.Frame(outer, bg=c["bg"])
        top.pack(fill="x", padx=pad, pady=(self.px(13), self.px(8)))
        tk.Label(top, text="无线设备电量", bg=c["bg"], fg=c["text"],
                 font=(self.f_text, 11, "bold")).pack(side="left")
        close = tk.Label(top, text="\uE711" if self.icon_font else "×", bg=c["bg"], fg=c["sub"],
                         font=(self.icon_font or self.f_text, 9 if self.icon_font else 12), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.close())
        ref = tk.Label(top, text="\uE72C" if self.icon_font else "⟳", bg=c["bg"], fg=c["sub"],
                       font=(self.icon_font or self.f_text, 10), cursor="hand2")
        ref.pack(side="right", padx=(0, self.px(14)))
        ref.bind("<Button-1>", lambda e: self.refresh(notify_tray=True))

        # 三张蓝牙设备风格卡片
        for idx, d in enumerate(DEVICES):
            self._build_device_card(outer, d, pad, top_gap=0 if idx == 0 else self.px(7))

        # 设置卡片
        sett = tk.Frame(outer, bg=c["card"], highlightthickness=1, highlightbackground=c["border"])
        sett.pack(fill="x", padx=pad, pady=(self.px(8), 0))
        self._setting_row(sett, "托盘显示", self._mode_widget)
        self._sep(sett)
        self._setting_row(sett, "图标样式", self._style_widget)
        self._sep(sett)
        self._setting_row(sett, "刷新间隔", self._interval_widget)
        self._sep(sett)
        self._setting_row(sett, "开机自启", self._autostart_widget)

        # 底栏
        foot = tk.Frame(outer, bg=c["bg"])
        foot.pack(fill="x", padx=pad, pady=(self.px(11), self.px(13)))
        self._button(foot, "退出托盘", self.quit_tray).pack(side="left")
        self._button(foot, "调试", self.open_debug).pack(side="left", padx=(self.px(6), 0))
        self._button(foot, "立即刷新", lambda: self.refresh(notify_tray=True), accent=True).pack(side="right")

        root.bind("<Escape>", lambda e: self.close())
        root.bind("<FocusIn>", self._focus_in)
        root.bind("<FocusOut>", self._focus_out)

    def _build_device_card(self, parent, dev, pad, top_gap):
        tk, c = self.tk, self.c
        dev_id = dev["id"]
        card = tk.Frame(parent, bg=c["card"], highlightthickness=1, highlightbackground=c["border"])
        card.pack(fill="x", padx=pad, pady=(top_gap, 0))
        row = tk.Frame(card, bg=c["card"])
        row.pack(fill="x", padx=self.px(12), pady=(self.px(10), 0))

        box = self.px(38)
        glyph = tk.Canvas(row, width=box, height=box, bg=c["chip"], highlightthickness=1,
                          highlightbackground=c["border"])
        glyph.pack(side="left")

        info = tk.Frame(row, bg=c["card"])
        info.pack(side="left", fill="x", expand=True, padx=(self.px(10), 0))
        tk.Label(info, text=dev["name"], bg=c["card"], fg=c["text"], anchor="w",
                 font=(self.f_text, 10, "bold")).pack(fill="x")
        sub_row = tk.Frame(info, bg=c["card"])
        sub_row.pack(fill="x", pady=(self.px(2), 0))
        bat_mini = tk.Canvas(sub_row, width=self.px(24), height=self.px(15), bg=c["card"], highlightthickness=0)
        bat_mini.pack(side="left", padx=(0, self.px(5)))
        lbl_src = tk.Label(sub_row, text="读取中…", bg=c["card"], fg=c["sub"], anchor="w",
                           font=(self.f_text, 9))
        lbl_src.pack(side="left")

        lbl_pct = tk.Label(row, text="--", bg=c["card"], fg=c["mute"],
                           font=(self.f_disp, 20, "bold"))
        lbl_pct.pack(side="right")

        bar = tk.Canvas(card, height=self.px(5), bg=c["card"], highlightthickness=0)
        bar.pack(fill="x", padx=self.px(12), pady=(self.px(9), self.px(4)))
        bar.bind("<Configure>", lambda e, did=dev_id: self.draw_bar(did))

        lbl_state = tk.Label(card, text="", bg=c["card"], fg=c["mute"], anchor="w",
                             font=(self.f_text, 8))
        lbl_state.pack(fill="x", padx=self.px(12), pady=(0, self.px(9)))

        self.cards[dev_id] = {
            "glyph": glyph, "bat_mini": bat_mini, "src": lbl_src,
            "pct": lbl_pct, "bar": bar, "state": lbl_state,
        }

    def _sep(self, parent):
        self.tk.Frame(parent, bg=self.c["border"], height=1).pack(fill="x")

    def _setting_row(self, parent, label, make):
        tk, c = self.tk, self.c
        r = tk.Frame(parent, bg=c["card"])
        r.pack(fill="x", padx=self.px(12), pady=self.px(8))
        tk.Label(r, text=label, bg=c["card"], fg=c["text"], font=(self.f_text, 9)).pack(side="left")
        make(r).pack(side="right")

    def _button(self, parent, text, cmd, accent=False):
        tk, c = self.tk, self.c
        bg = c["accent"] if accent else c["btn"]
        fg = c["on_accent"] if accent else c["text"]
        b = tk.Label(parent, text=text, bg=bg, fg=fg, font=(self.f_text, 9),
                     padx=self.px(12), pady=self.px(4), cursor="hand2",
                     highlightthickness=0 if accent else 1, highlightbackground=c["border"])
        hover = _hex(_mix(tuple(int(bg[i:i + 2], 16) for i in (1, 3, 5)),
                          (255, 255, 255) if self.dark else (0, 0, 0), 0.08))
        b.bind("<Enter>", lambda e: b.configure(bg=hover))
        b.bind("<Leave>", lambda e: b.configure(bg=bg))
        b.bind("<Button-1>", lambda e: cmd())
        return b

    def _segmented(self, parent, options, current, on_pick):
        tk, c = self.tk, self.c
        f = tk.Frame(parent, bg=c["border"])
        labels = {}

        def paint():
            for k, l in labels.items():
                sel = k == current[0]
                l.configure(bg=c["accent"] if sel else c["btn"], fg=c["on_accent"] if sel else c["text"])

        for k, text in options:
            l = tk.Label(f, text=text, font=(self.f_text, 9), padx=self.px(7), pady=self.px(2), cursor="hand2")
            l.pack(side="left", padx=(0 if not labels else 1, 0), pady=1)
            labels[k] = l

            def pick(e, k=k):
                current[0] = k
                paint()
                on_pick(k)
            l.bind("<Button-1>", pick)
        paint()
        return f

    def _mode_widget(self, parent):
        return self._segmented(
            parent,
            [("lowest", "仅最低电量"), ("all", "全部设备")],
            [self.cfg.get("tray_mode", "lowest")],
            self.set_mode,
        )

    def _style_widget(self, parent):
        return self._segmented(
            parent,
            [("dev_pct", "图标+%"), ("bt_single", "蓝牙条"), ("bat_pct", "电池+%"), ("pct_only", "纯数字")],
            [self.cfg["style"]],
            self.set_style,
        )

    def _make_switch(self, parent, get_val, on_toggle):
        tk, c = self.tk, self.c
        f = tk.Frame(parent, bg=c["card"])
        lbl = tk.Label(f, text="", bg=c["card"], fg=c["sub"], font=(self.f_text, 9))
        lbl.pack(side="left", padx=(0, self.px(8)))
        w, h = self.px(38), self.px(19)
        cv = tk.Canvas(f, width=w, height=h, bg=c["card"], highlightthickness=0, cursor="hand2")
        cv.pack(side="left")

        def draw():
            cv.delete("all")
            on = bool(get_val())
            r = h / 2 - 1
            fill = c["accent"] if on else c["card"]
            outline = c["accent"] if on else c["sub"]
            x0, y0, x1, y1 = 1, 1, w - 2, h - 2
            for (cx, cy) in ((x0 + r, y0 + r), (x1 - r, y0 + r)):
                cv.create_oval(cx - r, cy - r, cx + r, cy + r, fill=fill, outline=outline, width=1)
            cv.create_rectangle(x0 + r, y0, x1 - r, y1, fill=fill, outline="")
            cv.create_line(x0 + r, y0, x1 - r, y0, fill=outline)
            cv.create_line(x0 + r, y1, x1 - r, y1, fill=outline)
            kr = self.px(4.8) if on else self.px(4.2)
            kx = x1 - r if on else x0 + r
            kc = c["on_accent"] if on else c["sub"]
            cv.create_oval(kx - kr, (h - 1) / 2 - kr, kx + kr, (h - 1) / 2 + kr, fill=kc, outline="")
            lbl.configure(text="开" if on else "关")

        def click(e):
            on_toggle()
            draw()
        cv.bind("<Button-1>", click)
        draw()
        return f

    def _autostart_widget(self, parent):
        def toggle():
            try:
                set_autostart(not autostart_enabled())
            except Exception as e:
                self.cards["mouse"]["state"].configure(text=f"设置开机自启失败：{e}")
        return self._make_switch(parent, autostart_enabled, toggle)

    def _interval_widget(self, parent):
        tk, c = self.tk, self.c
        f = tk.Frame(parent, bg=c["card"])
        cur = self.cfg["interval"]
        if cur not in INTERVAL_CHOICES:
            INTERVAL_CHOICES.append(cur)
            INTERVAL_CHOICES.sort()
        self.iv_idx = INTERVAL_CHOICES.index(cur)
        fnt = (self.icon_font, 8) if self.icon_font else (self.f_text, 10)
        left = tk.Label(f, text="\uE76B" if self.icon_font else "‹", bg=c["card"], fg=c["sub"], font=fnt,
                        cursor="hand2", padx=self.px(6))
        self.lbl_iv = tk.Label(f, text="", bg=c["card"], fg=c["text"], font=(self.f_text, 9), width=7)
        right = tk.Label(f, text="\uE76C" if self.icon_font else "›", bg=c["card"], fg=c["sub"], font=fnt,
                         cursor="hand2", padx=self.px(6))
        left.pack(side="left")
        self.lbl_iv.pack(side="left")
        right.pack(side="left")
        left.bind("<Button-1>", lambda e: self.step_interval(-1))
        right.bind("<Button-1>", lambda e: self.step_interval(1))
        self._show_iv()
        return f

    def _show_iv(self):
        s = INTERVAL_CHOICES[self.iv_idx]
        self.lbl_iv.configure(text=f"{s} 秒" if s < 60 else f"{s // 60} 分钟")

    def step_interval(self, d):
        self.iv_idx = max(0, min(len(INTERVAL_CHOICES) - 1, self.iv_idx + d))
        self._show_iv()
        self.cfg["interval"] = INTERVAL_CHOICES[self.iv_idx]
        self.save_and_notify()

    def set_mode(self, k):
        self.cfg["tray_mode"] = k
        self.save_and_notify()

    def set_style(self, k):
        self.cfg["style"] = k
        self.save_and_notify()

    def save_and_notify(self):
        disk = load_config()
        disk.update({
            "tray_mode": self.cfg.get("tray_mode", "lowest"),
            "style": self.cfg["style"],
            "interval": self.cfg["interval"],
        })
        save_config(disk)
        hw = find_tray_hwnd()
        if hw:
            user32.PostMessageW(hw, WM_RELOAD, 0, 0)

    def open_debug(self):
        subprocess.Popen(self_cmd("--debug-ui"), cwd=HERE, close_fds=True,
                         creationflags=0x08000000)
        self.close()

    def quit_tray(self):
        hw = find_tray_hwnd()
        if hw:
            user32.PostMessageW(hw, WM_CLOSE, 0, 0)
        self.close()

    def place(self):
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.update_idletasks()
        w = max(self.px(400), self.root.winfo_reqwidth() + self.px(8))
        h = self.root.winfo_reqheight()
        l, t, r, b = work_area()
        m = self.px(12)
        self.root.geometry(f"{w}x{h}+{r - w - m}+{max(t + m, b - h - m)}")

    def after_map(self):
        try:
            hwnd = user32.GetAncestor(wt.HWND(self.root.winfo_id()), 2)
            dwm_style(hwnd, self.dark)
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        self.root.focus_force()

    def _focus_in(self, e):
        self._had_focus = True

    def _focus_out(self, e):
        self.root.after(200, self._check_focus)

    def _check_focus(self):
        try:
            f = self.root.focus_displayof()
        except Exception:
            f = None
        if self._had_focus and f is None:
            self.close()

    def close(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def refresh(self, notify_tray=False):
        if notify_tray:
            hw = find_tray_hwnd()
            if hw:
                user32.PostMessageW(hw, WM_REFRESH, 0, 0)
        if self.reading:
            return
        self.reading = True
        for d in DEVICES:
            self.cards[d["id"]]["state"].configure(text="正在读取…")
        cfg = dict(self.cfg)

        def work():
            self.q.put(read_all(cfg))
        threading.Thread(target=work, daemon=True).start()

    def pump(self):
        try:
            while True:
                res = self.q.get_nowait()
                self.reading = False
                self.show_all(res)
        except queue.Empty:
            pass
        try:
            self.root.after(100, self.pump)
        except Exception:
            pass

    def level_col(self, lv):
        if lv is None:
            return self.c["mute"]
        return self.c["ok"] if lv >= 60 else self.c["warn"] if lv >= 30 else self.c["bad"]

    def show_all(self, res):
        self.readings = res
        self._imgs.clear()
        c = self.c
        light = not self.dark
        ms = self.px(24)
        box = self.px(38)
        bw, bh = self.px(24), self.px(15)

        for d in DEVICES:
            did = d["id"]
            r = res.get(did) or Reading(did)
            w = self.cards[did]
            if r.ok:
                prefix = "~" if r.approx else ""
                w["pct"].configure(text=f"{prefix}{r.level}%", fg=c["ok"] if r.charging else c["text"])
                st = "充电中" if r.charging else "已连接"
                w["src"].configure(text=f"{r.source} · {st}" if "·" not in r.source else r.source)
                extra = f" · {r.detail}" if r.detail else ""
                w["state"].configure(text=f"更新于 {r.time.strftime('%H:%M:%S')}{extra}")
                lv, ch, dim = r.level, r.charging, False
            else:
                w["pct"].configure(text="--", fg=c["mute"])
                w["src"].configure(text={"sleep": "休眠 / 未开机", "absent": "未连接"}.get(r.state, "读取失败"))
                w["state"].configure(text=r.msg)
                lv, ch, dim = None, False, True

            self.draw_bar(did)
            mpx = draw_device_full(did, ms, light, dim=dim)
            mimg = _rgba_to_photo(self.tk, ms, ms, mpx, c["chip"])
            self._imgs.append(mimg)
            w["glyph"].delete("all")
            w["glyph"].create_image(box // 2, box // 2, image=mimg)

            bpx = draw_horizontal_battery(bw, bh, lv, ch, light, dim=dim)
            bimg = _rgba_to_photo(self.tk, bw, bh, bpx, c["card"])
            self._imgs.append(bimg)
            w["bat_mini"].delete("all")
            w["bat_mini"].create_image(bw // 2, bh // 2, image=bimg)

    def draw_bar(self, did):
        cv = self.cards[did]["bar"]
        c = self.c
        cv.delete("all")
        w = cv.winfo_width()
        h = self.px(5)
        if w <= 2:
            return
        r = self.readings.get(did)
        lv = r.level if (r and r.ok) else None

        def pill(x0, x1, col):
            rr = h / 2
            if x1 - x0 < h:
                x1 = x0 + h
            cv.create_oval(x0, 0, x0 + h, h, fill=col, outline="")
            cv.create_oval(x1 - h, 0, x1, h, fill=col, outline="")
            cv.create_rectangle(x0 + rr, 0, x1 - rr, h, fill=col, outline="")
        pill(0, w, c["track"])
        if lv:
            pill(0, max(h, w * lv / 100), self.level_col(lv))

    def run(self):
        self.root.mainloop()


def run_panel():
    hw = user32.FindWindowW(None, PANEL_TITLE)
    if hw:
        user32.SetForegroundWindow(hw)
        return 0
    Panel().run()
    return 0


# ════════════════════════════════════════════════════════════════════
#  完整调试版
# ════════════════════════════════════════════════════════════════════
class DebugUI:
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk
        set_dpi_aware()
        self.tk, self.ttk = tk, ttk
        self.root = root = tk.Tk()
        root.title(DEBUG_TITLE)
        root.geometry("1100x720")
        self.devs = []
        top = ttk.Frame(root)
        top.pack(fill="x", padx=8, pady=6)
        self.only_known = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="只显示已知外设 (雷蛇/漫步者/Xbox)", variable=self.only_known,
                        command=self.scan).pack(side="left")
        ttk.Button(top, text="重新枚举", command=self.scan).pack(side="left", padx=6)
        ttk.Button(top, text="一键读全部三台电量", command=self.quick_read).pack(side="left")

        cols = ("vidpid", "mi", "col", "up", "u", "in", "out", "feat", "product", "path")
        self.tree = ttk.Treeview(root, columns=cols, show="headings", height=10)
        for cname, w, t in zip(cols, (90, 40, 40, 60, 60, 45, 45, 45, 200, 480),
                               ("VID:PID", "MI", "Col", "UsagePg", "Usage", "In", "Out", "Feat", "产品", "路径")):
            self.tree.heading(cname, text=t)
            self.tree.column(cname, width=w, stretch=cname == "path")
        self.tree.pack(fill="x", padx=8)

        f1 = ttk.LabelFrame(root, text="Feature Report")
        f1.pack(fill="x", padx=8, pady=6)
        ttk.Label(f1, text="Report ID").pack(side="left", padx=4)
        self.rid = ttk.Entry(f1, width=5)
        self.rid.insert(0, "00")
        self.rid.pack(side="left")
        ttk.Button(f1, text="转储 GetFeature", command=self.dump_feature).pack(side="left", padx=4)
        ttk.Button(f1, text="探测 Report ID (0~255)", command=self.probe_ids).pack(side="left", padx=4)
        ttk.Label(f1, text="原始 SetFeature（hex，含 Report ID）").pack(side="left", padx=(16, 4))
        self.raw = ttk.Entry(f1, width=40)
        self.raw.pack(side="left")
        ttk.Button(f1, text="发送", command=self.send_raw).pack(side="left", padx=4)

        f2 = ttk.LabelFrame(root, text="雷蛇命令（90 字节协议）")
        f2.pack(fill="x", padx=8)
        self.ent = {}
        cfg = load_config()
        bc = cfg["battery_cmd"]
        for k, label, val in (("tid", "Transaction ID", cfg["transaction_ids"][0]), ("cls", "Class", bc[0]),
                              ("cmd", "ID", bc[1]), ("size", "Size", bc[2]), ("args", "参数 hex", ""),
                              ("idx", "电量字节下标", str(cfg["value_index"])),
                              ("scale", "满量程", str(cfg["value_scale"]))):
            ttk.Label(f2, text=label).pack(side="left", padx=(6, 2))
            e = ttk.Entry(f2, width=14 if k == "args" else 5)
            e.insert(0, val)
            e.pack(side="left")
            self.ent[k] = e
        ttk.Button(f2, text="发送", command=self.send_razer).pack(side="left", padx=6)
        ttk.Button(f2, text="保存为标定", command=self.save_calib).pack(side="left")

        self.out = tk.Text(root, font=("Consolas", 10), wrap="none")
        self.out.pack(fill="both", expand=True, padx=8, pady=6)
        self.scan()

    def log(self, *a):
        self.out.insert("end", " ".join(str(x) for x in a) + "\n")
        self.out.see("end")

    def scan(self):
        self.tree.delete(*self.tree.get_children())
        all_devs = enum_hid(None)
        if self.only_known.get():
            self.devs = [d for d in all_devs if d.vid in (RAZER_VID, HECATE_VID, 0x045E)]
        else:
            self.devs = all_devs
        for i, d in enumerate(self.devs):
            self.tree.insert("", "end", iid=str(i), values=(
                f"{d.vid:04X}:{d.pid:04X}", d.mi, d.col, f"{d.usage_page:04X}", f"{d.usage:04X}",
                d.in_len, d.out_len, d.feat_len, d.product, d.path))
        self.log(f"枚举到 {len(self.devs)} 个 HID 接口")

    def sel(self):
        s = self.tree.selection()
        if not s:
            self.log("先在列表里选一个接口")
            return None
        return self.devs[int(s[0])]

    def _open(self, d):
        h, mode = hid_open_best(d.path)
        if not h:
            self.log("打开失败", ctypes.get_last_error())
        else:
            self.log(f"打开成功（access={mode}）")
        return h

    def dump_feature(self):
        d = self.sel()
        if not d:
            return
        n = max(d.feat_len, 2)
        h = self._open(d)
        if not h:
            return
        try:
            buf = (ctypes.c_ubyte * n)()
            buf[0] = int(self.rid.get(), 16)
            with hid_mutex():
                ok = hid.HidD_GetFeature(h, buf, n)
            self.log("GetFeature", "OK" if ok else f"失败 {ctypes.get_last_error()}", bytes(buf).hex(" "))
        finally:
            kernel32.CloseHandle(h)

    def probe_ids(self):
        d = self.sel()
        if not d:
            return
        n = max(d.feat_len, 2)
        h = self._open(d)
        if not h:
            return
        found = []
        try:
            with hid_mutex():
                for rid in range(256):
                    buf = (ctypes.c_ubyte * n)()
                    buf[0] = rid
                    if hid.HidD_GetFeature(h, buf, n):
                        found.append(rid)
        finally:
            kernel32.CloseHandle(h)
        self.log("可读的 Feature Report ID：", " ".join(f"{x:02X}" for x in found) or "无")

    def send_raw(self):
        d = self.sel()
        if not d:
            return
        data = bytes.fromhex(self.raw.get().replace(" ", ""))
        n = max(d.feat_len, len(data))
        h = self._open(d)
        if not h:
            return
        try:
            buf = (ctypes.c_ubyte * n)(*data.ljust(n, b"\0"))
            with hid_mutex():
                ok = hid.HidD_SetFeature(h, buf, n)
            self.log("SetFeature", "OK" if ok else f"失败 {ctypes.get_last_error()}")
        finally:
            kernel32.CloseHandle(h)

    def _razer_params(self):
        g = lambda k: int(self.ent[k].get(), 16)
        args = bytes.fromhex(self.ent["args"].get().replace(" ", "")) if self.ent["args"].get().strip() else b""
        return g("tid"), g("cls"), g("cmd"), g("size"), args

    def send_razer(self):
        d = self.sel()
        if not d:
            return
        if d.feat_len < 91:
            self.log(f"提示：该接口 Feature 长度 {d.feat_len}，雷蛇协议需要 91")
        tid, cls, cmd, size, args = self._razer_params()
        h = self._open(d)
        if not h:
            return
        try:
            with hid_mutex():
                st, resp = razer_transact(h, tid, cls, cmd, size, args)
        finally:
            kernel32.CloseHandle(h)
        self.log(f"tid={tid:02X} class={cls:02X} id={cmd:02X} → {st}")
        if resp:
            self.log("  回复:", resp[:16].hex(" "), "...")
            idx, scale = int(self.ent["idx"].get()), int(self.ent["scale"].get())
            self.log(f"  resp[{idx}] = {resp[idx]} → {round(resp[idx] * 100 / max(1, scale))}%")

    def save_calib(self):
        tid, cls, cmd, size, _ = self._razer_params()
        cfg = load_config()
        tids = [f"{tid:02X}"] + [t for t in cfg["transaction_ids"] if t.upper() != f"{tid:02X}"]
        cfg.update({"transaction_ids": tids, "battery_cmd": [f"{cls:02X}", f"{cmd:02X}", f"{size:02X}"],
                    "value_index": int(self.ent["idx"].get()), "value_scale": int(self.ent["scale"].get())})
        d = self.sel() if self.tree.selection() else None
        if d and d.pid not in RAZER_PIDS:
            extra = set(cfg.get("extra_pids", []))
            extra.add(f"{d.pid:04X}")
            cfg["extra_pids"] = sorted(extra)
        save_config(cfg)
        self.log("已保存到 config.json")
        hw = find_tray_hwnd()
        if hw:
            user32.PostMessageW(hw, WM_RELOAD, 0, 0)

    def quick_read(self):
        for d in DEVICES:
            r = READERS[d["id"]]()
            self.log(f"[{d['name']}] state={r.state} 电量={r.level} 充电={r.charging} 来源={r.source} {r.detail or r.msg}")

    def run(self):
        self.root.mainloop()


# ════════════════════════════════════════════════════════════════════
#  命令行
# ════════════════════════════════════════════════════════════════════
def cmd_once():
    res = read_all()
    any_ok = False
    for d in DEVICES:
        r = res[d["id"]]
        if r.ok:
            any_ok = True
            p = "~" if r.approx else ""
            ch = "（充电中）" if r.charging else ""
            dt = f" · {r.detail}" if r.detail else ""
            print(f"{d['name']}：{p}{r.level}%{ch}  [{r.source}{dt}]")
        else:
            print(f"{d['name']}：-- （{r.msg}）")
    return 0 if any_ok else 2


def cmd_scan():
    for d in enum_hid(None):
        if d.vid in (RAZER_VID, HECATE_VID, 0x045E):
            print(d)
    print("-" * 50)
    return cmd_once()


def cmd_install():
    write_app_icon()
    set_autostart(True)
    print("开机自启：", startup_lnk())
    for m in make_desktop_links():
        print(m)
    return 0


def cmd_uninstall():
    lnk = startup_lnk()
    if os.path.exists(lnk):
        os.remove(lnk)
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_APPROVED, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_NAME + ".lnk")
    except OSError:
        pass
    remove_desktop_links()
    print("已删除开机自启和桌面链接")
    return 0


def _attach_console_if_cli(argv):
    """单文件 GUI exe 在命令行带 --once/--scan/--debug 等参数时，附着到父控制台以便输出。"""
    if not FROZEN:
        return
    if not any(f in argv for f in ("--once", "--scan", "--install", "--uninstall", "--debug")):
        return
    try:
        if kernel32.AttachConsole(wt.DWORD(-1)):
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
    except Exception:
        pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    _attach_console_if_cli(argv)
    if "--once" in argv:
        return cmd_once()
    if "--scan" in argv:
        return cmd_scan()
    if "--panel" in argv:
        return run_panel()
    if "--debug-ui" in argv:
        DebugUI().run()
        return 0
    if "--install" in argv:
        return cmd_install()
    if "--uninstall" in argv:
        return cmd_uninstall()
    return run_tray()


if __name__ == "__main__":
    sys.exit(main() or 0)
