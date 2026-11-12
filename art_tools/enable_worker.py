"""Migrate the finished preview run to the authorized independent art worker."""
from pathlib import Path
import copy
import json
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from unattended.art_pipeline import atomic, read, recipe_overlay
from unattended.config import load_config
from unattended.engine import Engine
from unattended.art_worker import ArtWorker

RUNTIME = Path('C:/codex-dsh-work/zhuguo-art')
WORK = RUNTIME / 'workspace'
MAIN = REPO / 'unattended.local.json'
WORKER_CONFIG = RUNTIME / 'worker.json'

def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True, capture_output=True, text=True).stdout.strip()

def main():
    if WORKER_CONFIG.exists():
        worker = read(WORKER_CONFIG)
        worker['claude'].pop('critical_plan_reasoning_effort', None)
        atomic(WORKER_CONFIG, worker)
        finish_setup()
        return
    archive = RUNTIME / 'migration-history' / time.strftime('%Y%m%d-%H%M%S')
    archive.mkdir(parents=True)
    original = read(MAIN)
    before_binding = Engine(load_config(MAIN))._binding()
    atomic(archive / 'main-config-before.json', original)
    for name in ('state.json', 'heartbeat.json'):
        path = RUNTIME / name
        if path.exists():
            path.rename(archive / name)

    approved_file = RUNTIME / 'approved/HOUSE-COMMON-001.json'
    approved = read(approved_file)
    atomic(archive / 'approved-before.json', approved)
    recipe_file = WORK / 'art-jobs/TASK-ART-0001/recipe.json'
    fields = ('asset_id', 'revision', 'prompt', 'target_height_m', 'max_footprint_m', 'front_rotation_deg')
    recipe = {'version': 1, 'assets': [{k: a[k] for k in fields} for a in approved['assets']]}
    low = recipe['assets'][0]
    low.update(revision='v002', target_height_m=3.1, max_footprint_m=[5.0, 4.2], front_rotation_deg=90)
    low['prompt'] = (
        '沿用参考图的半写实手绘A画风与克制自然色彩，但制作真实贫困农户的小茅屋，不能把参考图的富裕木构建筑照搬。'
        '单层一至两间、宽约4.5米深约3.5米高约3米，小体量，不是整齐三开间别院。'
        '墙为局部露出竹编或枝条骨架的粗夯土抹泥墙，手工抹痕和自然不平整，局部修补；'
        '低矮简易双坡茅草顶，无翘角无瓦脊，茅草厚薄略不齐，有旧草与补换的新草色差、压草木杆及草绳；'
        '未经油漆的细木柱，简单板木门，小而实用的单个木窗或木板窗；'
        '直接落在矮石脚和夯土地基上，入口只有一块磨损踏石，无高台基、无宽石阶、无回廊、无雕花格窗、无精美栏杆。'
        '贫困但仍有人维护居住，不做废墟、恐怖破屋或夸张脏污。材料节俭、结构勉强但可信，细节仍须精细，避免光滑塑料质感。'
        '前偏45度俯视，门朝前清楚可见，四面完整，单体住宅纯白背景，柔和中性光，无人物、无院落道具、无文字水印。'
        '禁止宫殿、商铺、牌匾、油亮木柱、均匀金黄精修草顶和富裕农庄。')
    atomic(recipe_file, recipe)
    approved['recipe_file'] = str(recipe_file)
    atomic(approved_file, approved)
    recipe_overlay(approved)
    brief = read(WORK / 'art-brief.json')
    brief['active_recipe'] = str(recipe_file)
    brief['user_feedback'] = 'L1原样板过于富足，按真实贫困农户返工；未验收不入zhuguo，接受后自动入库。'
    atomic(WORK / 'art-brief.json', brief)
    atomic(Path(approved['artifacts_dir']) / 'art-brief.json', brief)
    (WORK / 'AGENTS.md').write_text(
        '# 无人值守美术工作区\n仅处理美术任务，不是zhuguo功能开发工作副本。\n'
        '使用Claude Opus 5.5，high思考。固定A风格，3000积分累计上限，仅L1/L4/L8及必要道具。\n'
        '可修改art-jobs/TASK-ART-0001/recipe.json中的受限字段。L1 v002已按用户贫困茅屋要求预填；'
        'L4/L8沿用v001，本次不重新生成它们。更改已生成提示词必须递增版本，不得绕过预算。\n'
        '仅执行控制器提供的固定preview命令；不得直接调用Tripo、读密钥、改账本或可信批次配置。\n'
        '本地展示/技术通过不等于用户美术验收；不得向游戏工程复制未验收资产。\n', encoding='utf-8')
    branch = 'unattended/art-preview'
    if git(WORK, 'branch', '--show-current') != branch:
        git(WORK, 'branch', '-m', branch)
    git(WORK, 'add', 'AGENTS.md', 'art-brief.json', 'art-jobs')
    if git(WORK, 'diff', '--cached', '--name-only'):
        git(WORK, 'commit', '-m', 'TASK-ART-0001: pin independent worker scope and humble cottage revision')

    project = Path('C:/codex-dsh-work/zhuguo-art-project')
    project.mkdir(exist_ok=True)
    if not (project / '.git').exists():
        git(project, 'init')
    worker = copy.deepcopy(original)
    for key in ('publication', 'milestone_checks', 'art_worker', 'notifications', 'calendar', 'burst'):
        worker.pop(key, None)
    worker.update(version=1, project_path=str(project), runtime_dir=str(RUNTIME), branch=branch,
                  base_ref='HEAD', push=False, checks=[], task_prefix='TASK-ART',
                  schedule={'always_on': True, 'timezone': 'Asia/Shanghai'}, step_timeout_seconds=7200,
                  goal='按版本化A风格和真实生活条件分批制作美术，独立验收后自动加入zhuguo资产库。')
    worker['claude'] = copy.deepcopy(read(RUNTIME / 'provider.json')['claude'])
    worker['claude'].pop('critical_plan_reasoning_effort', None)
    worker['claude'].update(model='claude-opus-5-5', reasoning_effort='high')
    worker['art_pipeline'] = {'config_file': str(approved_file), 'task_ids': ['TASK-ART-0001'],
                              'artifacts_dir': approved['artifacts_dir']}
    worker['art_promotion'] = {'target_config_file': str(MAIN), 'target_branch': 'main', 'push': False, 'remote': 'origin'}
    atomic(WORKER_CONFIG, worker)
    finish_setup()

def finish_setup():
    original = read(MAIN)
    before_binding = Engine(load_config(MAIN))._binding()
    verified = load_config(WORKER_CONFIG)
    art = ArtWorker(verified)
    art._workspace()
    art.enqueue('TASK-ART-0001', '普通住宅三级样板：贫困茅屋返工',
        '用户要求初始茅屋符合贫困人家的真实材料和工艺。检查已预填的L1 v002配方，必要时在受限文件内完善；'
        'L4/L8 v001复用，不重新生成。完成固定preview、Blender和独立UE展示后提交技术检查与用户按批次验收。'
        '实际累计预算仍为3000，不以花满为目标。未验收不得进入zhuguo，接受后交自动入库消费者处理。')
    original['art_worker'] = {'config_file': str(WORKER_CONFIG)}
    atomic(MAIN, original)
    if Engine(load_config(MAIN))._binding() != before_binding:
        original.pop('art_worker', None)
        atomic(MAIN, original)
        raise RuntimeError('Main controller binding changed; main config restored')
    print(json.dumps({'status': 'ready', 'worker_config': str(WORKER_CONFIG),
                      'main_binding_unchanged': True,
                      'task': 'TASK-ART-0001', 'model': 'claude-opus-5-5', 'effort': 'high'}, ensure_ascii=False))

if __name__ == '__main__':
    main()
