"""Read-only host telemetry copied from canonical VPS1 Scene Builder.

Source: vps1-deploy-control/apps/scene-builder/part01/app.py (2026-11-19).
Copied helpers: _service_active, _read_cpu_percent, _vps_dashboard_metrics,
_dashboard_services. No Scene API call, credentials, or service mutations.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import os
import re
import shutil
import subprocess

def _service_active(name: str) -> bool:
    try:
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", name], timeout=2, check=False
        ).returncode == 0
    except Exception:
        return False

def _read_cpu_percent() -> float:
    def snap() -> tuple[int, int]:
        nums = [int(x) for x in Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]]
        return sum(nums), nums[3] + (nums[4] if len(nums) > 4 else 0)
    try:
        import time as _time
        t1,i1=snap(); _time.sleep(0.06); t2,i2=snap(); dt=max(1,t2-t1)
        return round(max(0.0,min(100.0,100.0*(dt-(i2-i1))/dt)),1)
    except Exception: return 0.0

def _vps_dashboard_metrics() -> dict[str, Any]:
    mem: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if ":" in line:
                k,v=line.split(":",1); mem[k]=int(v.strip().split()[0])*1024
    except Exception: pass
    total=int(mem.get("MemTotal",0)); available=int(mem.get("MemAvailable",0)); used=max(0,total-available); disk=shutil.disk_usage("/")
    try: uptime_seconds=float(Path("/proc/uptime").read_text().split()[0])
    except Exception: uptime_seconds=0
    days=int(uptime_seconds//86400); hours=int((uptime_seconds%86400)//3600); minutes=int((uptime_seconds%3600)//60)
    try: load1,load5,load15=os.getloadavg()
    except Exception: load1=load5=load15=0.0
    os_name="Ubuntu Linux"
    try:
        m=re.search(r'^PRETTY_NAME="?([^"\\n]+)',Path("/etc/os-release").read_text(encoding="utf-8"),re.M)
        if m: os_name=m.group(1)
    except Exception: pass
    gb=1024**3
    return {"hostname":os.uname().nodename,"os":os_name,"cpu_cores":os.cpu_count() or 1,"cpu_percent":_read_cpu_percent(),"load_1m":round(load1,2),"load_5m":round(load5,2),"load_15m":round(load15,2),"uptime_seconds":int(uptime_seconds),"uptime_human":f"{days}d {hours}h {minutes}m","ram":{"total_gb":round(total/gb,1),"used_gb":round(used/gb,1),"available_gb":round(available/gb,1),"percent":round((used/total*100) if total else 0,1)},"disk":{"mount":"/","total_gb":round(disk.total/gb,1),"used_gb":round(disk.used/gb,1),"free_gb":round(disk.free/gb,1),"percent":round((disk.used/disk.total*100) if disk.total else 0,1)},"server_time":datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")}

def _dashboard_services() -> list[dict[str, Any]]:
    # Fresh-VPS systemd units, not retired pre-reinstall legacy names.
    services=[
        ("vps1-scene-candidate.service","Scene Builder Web","Shared Scene login / dashboard"),
        ("vps1-production-auto-caption-studio.service","ACS Web","Caption API / queue"),
        ("vps1-production-acs-ingress.service","ACS Ingress","Drive intake pipeline"),
        ("vps1-production-acs-render.service","ACS Renderer","Caption rendering"),
        ("vps1-production-acs-compress.service","ACS Compressor","Video compression"),
        ("vps1-production-acs-upload.service","ACS Uploader","Drive upload"),
        ("vps1-production-voice-studio.service","Voice Studio","Voice API"),
        ("vps1-production-local-upload-manager.service","Auto Uploader","YouTube upload engine"),
        ("vps1-production-lum-worker.service","Uploader Worker","Upload queue processing"),
    ]
    return [{"name":n,"label":l,"role":r,"active":_service_active(n)} for n,l,r in services]

def system_dashboard_snapshot() -> dict[str, Any]:
    return {
        "ok": True,
        "generated_at_local": datetime.now().astimezone().strftime("%H:%M:%S"),
        "vps": _vps_dashboard_metrics(),
        "services": _dashboard_services(),
    }
