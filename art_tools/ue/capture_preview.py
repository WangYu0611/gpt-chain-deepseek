"""Render fresh UE screenshots asynchronously, allowing editor ticks and shader compilation."""
import hashlib
import json
import math
import os
from pathlib import Path
import time
import traceback
import unreal

ROOT = Path(os.environ["ZG_ART_OUTPUT"])
ID = os.environ["ZG_ART_CAPTURE_ID"]
SHOTS = ROOT / "screenshots" / ID
SHOTS.mkdir(parents=True, exist_ok=True)
RECEIPT = ROOT / "receipts" / (ID + "-capture.json")
STARTED = time.time()
LEVEL = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
LEVEL.load_level("/Game/ArtPreview/Maps/HousingPreview")
unreal.EditorPythonScripting.set_keep_python_script_alive(True)
world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
for command in ("r.ScreenPercentage 100", "r.MotionBlurQuality 0", "r.DepthOfFieldQuality 0",
                "r.Streaming.FullyLoadUsedTextures 1", "r.TextureStreaming 0"):
    unreal.SystemLibrary.execute_console_command(world, command)

def aim(position, target):
    dx, dy, dz = (target[i]-position[i] for i in range(3))
    return unreal.Rotator(pitch=math.degrees(math.atan2(dz, math.hypot(dx,dy))),
                          yaw=math.degrees(math.atan2(dy,dx)), roll=0)

VIEWS = [
    ("01_all_levels", (300,-3100,2800), (0,0,100)),
    ("02_L1_courtyard", (-1100,-1450,1500), (-2000,0,100)),
    ("03_L4_courtyard", (900,-1450,1500), (0,0,100)),
    ("04_L8_courtyard", (2900,-1450,1500), (2000,0,100)),
    ("05_L1_details", (-1400,-900,1000), (-2050,80,180)),
    ("06_L4_details", (650,-950,1050), (0,100,230)),
    ("07_L8_details", (2850,-1100,1200), (2000,100,280)),
    ("08_rear_structure", (-300,3100,2800), (0,100,100)),
]
STATE = {"index":0,"phase":"warmup","after":time.monotonic()+45,"task":None,"shots":[]}
HANDLE = None

def finish(error=None):
    global HANDLE
    if HANDLE is not None:
        unreal.unregister_slate_post_tick_callback(HANDLE)
        HANDLE = None
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT.write_text(json.dumps({"status":"failed" if error else "passed", "error":error,
        "capture_id":ID,"started_epoch":STARTED,"completed_epoch":time.time(),"shots":STATE["shots"],
        "real_rhi":True,"image_content_validated":False,"human_visual_acceptance":"pending"},
        ensure_ascii=False,indent=2),encoding="utf-8")
    # ExecutePythonScript owns shutdown once keep-alive becomes false. Its public
    # API specifies either releasing keep-alive OR quit_editor; doing both sends
    # duplicate exit requests while this Slate callback is still unwinding.
    unreal.EditorPythonScripting.set_keep_python_script_alive(False)

def tick(delta):
    try:
        if time.time()-STARTED > 1200:
            raise RuntimeError("Timed out waiting for rendered screenshots")
        if time.monotonic()<STATE["after"]:
            return
        if STATE["index"]>=len(VIEWS):
            finish()
            return
        name, position, target = VIEWS[STATE["index"]]
        path = SHOTS/(name+".png")
        if STATE["phase"] in ("warmup","next"):
            # A single-plot view excludes neighboring examples, keeping the same
            # camera offset and illumination for a fair level-to-level comparison.
            selected = name.split("_")[1] if name.split("_")[1] in ("L1", "L4", "L8") else None
            for actor in unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors():
                label = actor.get_actor_label()
                actor_level = next((level for level in ("L1", "L4", "L8")
                                    if label.startswith("Plot_"+level+"_") or
                                    (label.startswith("ScaleFigure_") and "_"+level in label)), None)
                if actor_level:
                    actor.set_is_temporarily_hidden_in_editor(bool(selected and actor_level != selected))
            unreal.EditorLevelLibrary.set_level_viewport_camera_info(unreal.Vector(*position),aim(position,target))
            STATE.update(phase="settle",after=time.monotonic()+12)
        elif STATE["phase"]=="settle":
            STATE["task"]=unreal.AutomationLibrary.take_high_res_screenshot(1920,1080,str(path))
            STATE.update(phase="capture",after=time.monotonic()+3)
        elif STATE["phase"]=="capture":
            if not path.is_file() or path.stat().st_size < 10000:
                return
            if path.stat().st_mtime < STARTED:
                raise RuntimeError("Stale screenshot detected")
            data=path.read_bytes()
            if data[:8] != b"\x89PNG\r\n\x1a\n":
                raise RuntimeError("Invalid screenshot format")
            STATE["shots"].append({"name":name,"path":str(path),"size":len(data),
                                   "sha256":hashlib.sha256(data).hexdigest(),"camera_cm":position,"target_cm":target})
            unreal.log("ZG_ART_SHOT "+name)
            STATE.update(index=STATE["index"]+1,phase="next",after=time.monotonic()+1,task=None)
    except Exception:
        finish(traceback.format_exc())

HANDLE=unreal.register_slate_post_tick_callback(tick)
