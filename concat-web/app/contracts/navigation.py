"""Canonical paths. Exactly three top-level shell sections."""
from dataclasses import dataclass

@dataclass(frozen=True)
class Page:
    key: str
    title: str
    path: str
    area: str
    description: str

DASHBOARD = (Page("dashboard", "Dashboard", "/dashboard", "dashboard", "Render and upload overview"),)
CONCAT = (
    Page("production", "Production", "/concat/production", "concat", "Rendering production queue"),
    Page("media", "Media", "/concat/media", "concat", "Verified rendered media"),
    Page("gpu-config", "GPU Config", "/concat/gpu-config", "concat", "Owner GPU connection controls"),
)
UPLOADER = (
    Page("sheets-sync", "Sheets Sync", "/concat-uploader/sheets-sync", "uploader", "Metadata sheet mapping"),
    Page("channels", "Channels", "/concat-uploader/channels", "uploader", "Channel Manager and Accounts & Tokens"),
    Page("upload-engine", "Upload Engine", "/concat-uploader/upload-engine", "uploader", "Video upload admission"),
    Page("queue", "Queue", "/concat-uploader/queue", "uploader", "Manual upload queue"),
)
ADMIN = (Page("admin", "Admin", "/admin", "admin", "Administration and security"),)
PAGES = DASHBOARD + CONCAT + UPLOADER + ADMIN
PAGE_BY_KEY = {p.key: p for p in PAGES}
