"""Initialize the explicitly authorized HOUSE-COMMON-001 preview, without spending credits."""
from pathlib import Path
import hashlib
import json
import shutil

REPO = Path(__file__).resolve().parents[1]
RUNTIME = Path("C:/codex-dsh-work/zhuguo-art")
ROOT = RUNTIME / "batches/HOUSE-COMMON-001"
PROTOTYPE = Path("C:/Users/wy331/Documents/AncientCivilianTest")


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    config_path = RUNTIME / "approved/HOUSE-COMMON-001.json"
    if config_path.exists():
        print("Existing approved batch retained:", config_path)
        return
    ROOT.mkdir(parents=True, exist_ok=True)
    workspace = RUNTIME / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    references = []
    for source, filename in ((PROTOTYPE / "tripo/bld_a_img/generated_image_url.png", "A_style_concept.png"),
                             (PROTOTYPE / "work/ue_styles/shots/A/city.png", "A_style_UE_city.png")):
        target = ROOT / "references" / filename
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(source, target)
        references.append({"path": str(target), "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                           "role": "User confirmed semi-realistic hand-painted A style; improve craft and detail"})
    style = ("以参考图的半写实手绘A风格、灰瓦与温润木色为共同基准，制作精美的古代中国城市建造游戏住宅资产。"
             "保持真实可信的木构、材料厚度、榫卯、檐口和门窗比例；精细但克制，色块清楚，木纹瓦片细节完整，"
             "柔和均匀无强投影的中性光，细节不要模糊，不画夸张墨线。架空中国古代民居，融合先秦的朴素构架、唐宋木作和明代院宅细节。"
             "参考图只用于画风与材质，请彻底改为下述住宅，不能保留商铺、招牌或柜台。")
    framing = ("单体住宅建筑主体，正面偏45度斜俯视，完整入画，纯白背景，无人物、无文字、无水印、无地面、无树木、无围墙、无院落杂物；"
               "正门朝前并清楚可见，正门高约2米；四面建筑完整，屋顶结构合理。 --no 商铺, 招牌, 文字, 水印, 宫殿, 仙侠, 巨大飞檐, 过曝, 裁切")
    specs = [
        ("house_l1", "L1", 4.2, [8, 6], "低等级普通住宅：精心维护的单层三间小农舍，夯土墙体，深褐木柱，浅麦色整齐茅草屋顶，屋脊简洁，无翘角；"
         "小型木格窗，结实的木门，石砌墙脚和两级石阶，门前窄木檐；宽约6.5米深约4.5米。材料朴素但工艺真实，整洁有人居住，不破败，不贫民窟。"),
        ("house_l4", "L4", 5.0, [10, 8], "中等级普通住宅：单层三开间正屋带一侧小附属房，形成紧凑L形轮廓，青灰筒瓦硬山顶，檐口微弯克制，"
         "褐色木柱与浅米灰抹墙，木格门窗，石台基，屋前短廊和木栏，清楚的瓦片、柱础与木作节点，宽约9米深约6.5米。普通富足人家，不是商铺或府邸。"),
        ("house_l8", "L8", 6.2, [12, 10], "高等级普通住宅：精致但不过度奢华的两层中央厅屋与较低侧厢连接的紧凑住宅主体，层次分明，"
         "青灰瓦悬山屋顶与适度挑檐，暖深木作、米白墙、青砖墙脚，二层细木栏杆、格扇窗和楼下廊檐，石台阶与精致柱础；"
         "宽约11米深约8米，高约6米。中央厅屋明显高于侧房，优雅实用的普通民居，不用红金配色，不用宫殿式斗拱或多重夸张飞檐。"),
    ]
    assets = [{"asset_id": ident, "level": level, "revision": "v001", "target_height_m": height,
               "max_footprint_m": footprint, "front_rotation_deg": 90, "prompt": style+desc+framing}
              for ident, level, height, footprint, desc in specs]
    cfg = {"version": 1, "batch_id": "HOUSE-COMMON-001", "phase": "preview", "budget_limit": 3000,
           "approved_levels": ["L1", "L4", "L8"], "remaining_levels_require_user_acceptance": True,
           "authorization": "2026-11-06 user approved total 3000 credits; only low/mid/high housing and necessary props before visual confirmation",
           "artifacts_dir": str(ROOT), "private_state_dir": str(RUNTIME / "private/HOUSE-COMMON-001"),
           "key_file": str(PROTOTYPE / "secrets/tripo_key.txt"),
           "blender": "C:/Program Files/Blender Foundation/Blender 5.2/blender.exe",
           "engine": "C:/Program Files/Epic Games/UE_5.8", "references": references,
           "style_reference_task_id": json.loads((PROTOTYPE / "tripo/tasks/bld_a_img.json").read_text(encoding="utf-8"))["task_id"],
           "assets": assets}
    save(config_path, cfg)
    public = {k: cfg[k] for k in ("batch_id", "phase", "budget_limit", "approved_levels", "authorization", "references", "assets")}
    public["plot_size_m"] = 16
    public["scope_note"] = "Adjustable showcase plot only; existing M3 gameplay footprint remains unchanged. Low/mid/high preview is part of the eventual eight-level batch."
    save(workspace / "art-brief.json", public)
    save(ROOT / "art-brief.json", public)
    (workspace / "AGENTS.md").write_text(
        "# 住宅美术预览任务\n本目录仅供TASK-ART-0001独立Claude会话使用，不是正在运行的M3游戏工作副本。\n"
        "用户确认半写实手绘A，更精细美观；固定大地块；仅L1/L4/L8和必要生活道具；全批3000积分上限。\n"
        "只能通过调度器给出的固定preview命令制作。不得读密钥、改私有账本或批次配置；不启动其余五级。\n"
        "读取art-brief.json，执行固定入口并检查公共交付回执。真实RHI展示和技术通过不代替用户视觉确认。\n",
        encoding="utf-8")
    original = json.loads((REPO / "unattended.local.json").read_text(encoding="utf-8-sig"))
    claude = dict(original["claude"])
    for key in ("visual_preflight", "test_preflight", "instructions", "plan_instructions", "critical_plan_model"):
        claude.pop(key, None)
    claude.update(ue_preflight=False, planned_core_tests=False)
    provider = {"runtime_dir": str(RUNTIME), "project_path": str(workspace), "coding_provider": "claude",
                "claude": claude, "art_pipeline": {"config_file": str(config_path),
                "task_ids": ["TASK-ART-0001"], "artifacts_dir": str(ROOT)}}
    save(RUNTIME / "provider.json", provider)
    print("Created approved preview configuration:", config_path)


if __name__ == "__main__":
    main()
