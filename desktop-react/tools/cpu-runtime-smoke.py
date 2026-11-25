"""Windows acceptance check for the bundled Vulkan loader and actual CPU ICD.

This checks driver discovery/device enumeration, not Concat frame correctness.
The native engine integration test supplies the separate rendering evidence.
"""
import ctypes as C
import os
import sys
from pathlib import Path


def main():
    if sys.platform != 'win32':
        raise RuntimeError('Windows runtime check requires Windows')
    root = Path(sys.argv[1]).resolve()
    os.environ['VK_DRIVER_FILES'] = str(root / 'lvp_icd.x86_64.json')
    os.environ['VK_LOADER_DEBUG'] = 'error,warn'
    loader = C.WinDLL(str(root / 'vulkan-1.dll'))
    C.WinDLL(str(root / 'vulkan_lvp.dll'))

    class InstanceInfo(C.Structure):
        _fields_ = [('sType', C.c_uint32), ('pNext', C.c_void_p),
                    ('flags', C.c_uint32), ('pApplicationInfo', C.c_void_p),
                    ('enabledLayerCount', C.c_uint32), ('ppEnabledLayerNames', C.c_void_p),
                    ('enabledExtensionCount', C.c_uint32), ('ppEnabledExtensionNames', C.c_void_p)]

    create = loader.vkCreateInstance
    create.argtypes = [C.POINTER(InstanceInfo), C.c_void_p, C.POINTER(C.c_void_p)]
    create.restype = C.c_int32
    destroy = loader.vkDestroyInstance
    destroy.argtypes = [C.c_void_p, C.c_void_p]
    destroy.restype = None
    enumerate_devices = loader.vkEnumeratePhysicalDevices
    enumerate_devices.argtypes = [C.c_void_p, C.POINTER(C.c_uint32), C.c_void_p]
    enumerate_devices.restype = C.c_int32
    instance = C.c_void_p()
    status = create(C.byref(InstanceInfo(sType=1)), None, C.byref(instance))
    if status != 0:
        raise RuntimeError(f'Bundled CPU ICD could not create a Vulkan instance: VkResult {status}')
    try:
        count = C.c_uint32()
        status = enumerate_devices(instance, C.byref(count), None)
        if status != 0 or count.value < 1:
            raise RuntimeError(f'CPU Vulkan device enumeration failed: VkResult {status}, devices {count.value}')
        print(f'PASS app-private CPU Vulkan instance and {count.value} device(s)', flush=True)
    finally:
        destroy(instance, None)


if __name__ == '__main__':
    main()
