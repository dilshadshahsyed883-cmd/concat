from __future__ import annotations

import signal
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from desktop_core.modules.database import Database
from desktop_core.modules.path_utils import DB_PATH as PC_DB_PATH

from .auto_engine_v6 import V6Store, generate_queue, parse_schedule_times, target_channels, channel_profile_scheduler_tick

STOP=False
GRACE_MINUTES=6
SCHEDULE_TZ=ZoneInfo("Asia/Karachi")


def _sig(_s,_f):
    global STOP
    STOP=True


for s in (signal.SIGTERM,signal.SIGINT):
    signal.signal(s,_sig)


def check_profile(db:Database,store:V6Store,profile:dict) -> int:
    channel_result=channel_profile_scheduler_tick(db,store,profile)
    if channel_result>=0:
        return channel_result
    if int(profile.get("enabled") or 0)!=1:
        return 0
    if int(profile.get("schedule_enabled") or 0)!=1:
        return 0
    if str(profile.get("work_mode") or "")!="auto":
        return 0
    now=datetime.now(SCHEDULE_TZ)
    times=parse_schedule_times(profile.get("schedule_times"))[:max(1,int(profile.get("daily_upload_count") or 1))]
    channels=target_channels(db,profile)
    scope=str(profile.get("upload_scope") or "per_channel")
    per_alarm_jobs=len(channels) if scope=="per_channel" else 1
    if per_alarm_jobs<=0:
        return 0
    policy=str(profile.get("missed_schedule_policy") or "run_missed_today")
    created_total=0
    for idx,alarm in enumerate(times,1):
        h,m=[int(x) for x in alarm.split(":")]
        alarm_dt=now.replace(hour=h,minute=m,second=0,microsecond=0)
        if alarm_dt>now:
            continue
        delay=max(0,int((now-alarm_dt).total_seconds()//60))
        run_key=f"{now.date().isoformat()} slot-{idx} {alarm}"
        if db.auto_engine_run_exists(int(profile["id"]),run_key):
            continue
        if delay>GRACE_MINUTES and policy=="skip_missed":
            db.record_auto_engine_run(int(profile["id"]),run_key,0)
            db.add_log(None,"INFO",f"Skipped late Auto Engine alarm by policy: {profile.get('category_name')} {alarm}",None)
            continue
        if delay>GRACE_MINUTES and policy=="ask":
            store.add_missed(int(profile["id"]),run_key,alarm,delay)
            continue
        if delay<=GRACE_MINUTES or policy=="run_missed_today":
            try:
                result=generate_queue(db,store,profile,run_key=run_key,max_jobs_override=per_alarm_jobs)
                created=int(result.get("created") or 0)
                created_total+=created
                if created:
                    store.set_engine(running=True,pause_after_current=False)
                    db.add_log(None,"INFO",f"Auto Engine alarm {alarm}: created {created} job(s).",None)
            except Exception as exc:
                # Consume the scheduler slot only when claim occurred inside generate_queue.
                # A hard validation failure remains visible and retryable on next scheduler pass.
                db.add_log(None,"ERROR",f"Auto Engine scheduler profile {profile.get('category_name')} {alarm}: {exc}",None)
    return created_total


def main() -> None:
    db=Database(PC_DB_PATH)
    store=V6Store()
    last_trash_day=None
    while not STOP:
        try:
            store.heartbeat("scheduler")
            if int(store.engine().get("scheduler_armed") or 0):
                for profile in db.list_auto_engine_profiles():
                    check_profile(db,store,profile)
            today=datetime.now(SCHEDULE_TZ).date().isoformat()
            if today!=last_trash_day:
                store.purge_trash(7)
                last_trash_day=today
        except Exception as exc:
            store.heartbeat("scheduler",str(exc))
        for _ in range(30):
            if STOP: break
            time.sleep(1)


if __name__=="__main__":
    main()
