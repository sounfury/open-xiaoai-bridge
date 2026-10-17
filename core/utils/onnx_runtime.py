"""ONNX Runtime 加载工具：让 Windows 的 Sherpa 使用当前虚拟环境中的 DLL。"""

import importlib.util
import os
import sys
from pathlib import Path


_dll_directory = None
_runtime_dll = None


def prepare_onnxruntime() -> None:
    """导入 Sherpa 前预加载正确的 DLL，避免误用 System32 中的旧运行库。"""
    global _dll_directory, _runtime_dll
    if sys.platform != "win32" or _runtime_dll is not None:
        return

    import ctypes

    spec = importlib.util.find_spec("onnxruntime")
    if spec is None or spec.origin is None:
        raise RuntimeError("未安装 onnxruntime，请先安装项目依赖")
    dll_path = Path(spec.origin).parent / "capi" / "onnxruntime.dll"
    if not dll_path.is_file():
        raise RuntimeError(f"当前 Python 环境缺少 ONNX Runtime DLL：{dll_path}")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    kernel32.GetModuleFileNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
    ]
    handle = kernel32.GetModuleHandleW("onnxruntime.dll")
    if handle:
        loaded_path = ctypes.create_unicode_buffer(32768)
        if not kernel32.GetModuleFileNameW(handle, loaded_path, len(loaded_path)):
            raise ctypes.WinError(ctypes.get_last_error())
        if Path(loaded_path.value).resolve() != dll_path.resolve():
            raise RuntimeError(
                f"已加载其他 ONNX Runtime DLL：{loaded_path.value}；"
                f"请在导入 Sherpa 前调用 prepare_onnxruntime()，使用 {dll_path}"
            )

    # 保留目录句柄和 DLL 引用，确保后续 KWS / ASR 初始化始终使用同一运行库。
    _dll_directory = os.add_dll_directory(str(dll_path.parent))
    _runtime_dll = ctypes.WinDLL(str(dll_path))
