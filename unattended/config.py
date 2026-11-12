"""User-owned configuration. Loading never authenticates or starts an agent."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil

from .calendar import WorkCalendar
from . import gitops
from .storage import read_json, write_json

ROOT = Path(__file__).resolve().parent.parent
ART_CODING_MODEL = "claude-opus-5-5"
ART_CODING_REASONING_EFFORT = "high"


def validate_model_settings(settings: dict, role: str) -> None:
    if "model" in settings:
        model = settings["model"]
        if not isinstance(model, str) or not model.strip() or "\0" in model:
            raise ValueError(f"{role}.model 必须为非空模型标识。")
    if "reasoning_effort" in settings:
        choices = {
            "codex": {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"},
            "dsh": {"off", "low", "high", "max"},
            "claude": {"low", "medium", "high", "xhigh", "max", "ultracode"},
        }[role]
        effort = settings["reasoning_effort"]
        if not isinstance(effort, str) or effort not in choices:
            raise ValueError(f"{role}.reasoning_effort 必须为 " + ", ".join(sorted(choices)))


def _path(value, parent):
    path = Path(value).expanduser()
    return str((parent / path).resolve() if not path.is_absolute() else path.resolve())


def validate_art_pipeline_settings(settings: dict, *, project: Path, runtime: Path) -> dict:
    """Validate the opt-in bridge and its dedicated nonsecret configuration directory."""
    if type(settings) is not dict or set(settings) != {"config_file", "task_ids", "artifacts_dir"}:
        raise ValueError("art_pipeline 必须仅包含 config_file、task_ids、artifacts_dir。")
    task_ids = settings["task_ids"]
    if (type(task_ids) is not list or not 1 <= len(task_ids) <= 32 or
            any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", value)
                for value in task_ids) or len(set(task_ids)) != len(task_ids)):
        raise ValueError("art_pipeline.task_ids 必须为 1 至 32 个不重复的精确任务编号，不能使用通配符。")
    paths = {}
    for field in ("config_file", "artifacts_dir"):
        value = settings[field]
        if (not isinstance(value, str) or not value.strip() or
                any(character in value for character in "*?\r\n\t\0") or not Path(value).is_absolute() or
                ".." in Path(value).parts):
            raise ValueError(f"art_pipeline.{field} 必须为无通配符或控制字符的绝对路径。")
        paths[field] = Path(value).absolute()
    config_file, artifacts = paths["config_file"], paths["artifacts_dir"]
    approved = config_file.parent
    project, runtime = Path(project).absolute(), Path(runtime).absolute()
    workspace = runtime / "workspace"
    if config_file.suffix.lower() != ".json":
        raise ValueError("art_pipeline.config_file 必须为可信批次 JSON 文件。")
    if any(path == root or path.is_relative_to(root)
           for path in paths.values() for root in (project, workspace)):
        raise ValueError("美术批次配置与产物目录必须位于 Claude 可写工作副本和原项目之外。")
    if (config_file == artifacts or config_file.is_relative_to(artifacts) or
            runtime == artifacts or runtime.is_relative_to(artifacts) or
            project == artifacts or project.is_relative_to(artifacts)):
        raise ValueError("美术产物只读目录不能包含批次配置、运行目录或原项目。")
    if any(approved == root or approved.is_relative_to(root) or root.is_relative_to(approved)
           for root in (artifacts, project, workspace)) or runtime == approved or runtime.is_relative_to(approved):
        raise ValueError("美术批次配置必须位于与产物、项目和工作副本隔离的专用目录。")
    if approved.is_dir() and any(child != config_file for child in approved.iterdir()):
        raise ValueError("美术批次配置专用目录只能包含该非密钥配置 JSON。")
    return {**settings, **{key: str(value) for key, value in paths.items()}, "task_ids": list(task_ids)}


def validate_art_worker_settings(settings: dict, *, project: Path, runtime: Path) -> dict:
    """Route to a separate worker; loading the main config grants no new access."""
    if type(settings) is not dict or set(settings) != {"config_file"}:
        raise ValueError("art_worker 必须仅包含 config_file。")
    value = settings["config_file"]
    if (not isinstance(value, str) or not value.strip() or any(character in value for character in "*?\r\n\t\0")
            or not Path(value).is_absolute() or ".." in Path(value).parts or Path(value).suffix.lower() != ".json"):
        raise ValueError("art_worker.config_file 必须为无通配符或控制字符的绝对 JSON 文件路径。")
    path = Path(value).resolve()
    if any(path == root or path.is_relative_to(root) for root in (Path(project).resolve(), Path(runtime).resolve())):
        raise ValueError("独立美术 worker 配置必须位于主项目和主运行目录之外。")
    return {"config_file": str(path)}


def validate_art_promotion_settings(settings: dict, *, project: Path, runtime: Path) -> dict:
    """Validate an explicit integration policy without reading its target config."""
    if type(settings) is not dict or set(settings) != {"target_config_file", "target_branch", "push", "remote"}:
        raise ValueError("art_promotion 必须仅包含 target_config_file、target_branch、push、remote。")
    path = validate_art_worker_settings({"config_file": settings["target_config_file"]},
                                       project=project, runtime=runtime)["config_file"]
    branch = settings["target_branch"]
    if (not isinstance(branch, str) or branch == "HEAD" or len(branch) > 240 or
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", branch) or ".." in branch or
            branch.endswith(".") or any(not part or part.startswith(".") or part.endswith(".lock")
                                       for part in branch.split("/"))):
        raise ValueError("art_promotion.target_branch 必须为明确、安全的 Git 分支名。")
    remote = settings["remote"]
    if (type(settings["push"]) is not bool or not isinstance(remote, str) or
            len(remote) > 100 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", remote)):
        raise ValueError("art_promotion.push 必须为布尔值，remote 必须为安全的远端名称。")
    return {**settings, "target_config_file": path}


def validate_art_autonomy_settings(settings: dict) -> dict:
    if type(settings) is not dict or set(settings) != {"enabled", "max_cycles", "rework_window"}:
        raise ValueError("art_autonomy 必须仅包含 enabled、max_cycles、rework_window。")
    if type(settings["enabled"]) is not bool:
        raise ValueError("art_autonomy.enabled 必须为布尔值。")
    for key in ("max_cycles", "rework_window"):
        if type(settings[key]) is not int or not 1 <= settings[key] <= 5:
            raise ValueError("art_autonomy." + key + " 必须为 1..5 的整数。")
    return dict(settings)


def validate_task_replanning_settings(settings: dict) -> dict:
    if type(settings) is not dict or set(settings) != {"enabled", "max_cycles", "rework_window"}:
        raise ValueError("task_replanning 必须仅包含 enabled、max_cycles、rework_window。")
    if type(settings["enabled"]) is not bool:
        raise ValueError("task_replanning.enabled 必须为布尔值。")
    for key in ("max_cycles", "rework_window"):
        if type(settings[key]) is not int or not 1 <= settings[key] <= 5:
            raise ValueError("task_replanning." + key + " 必须为 1..5 的整数。")
    return dict(settings)


def validate_task_validation_policy(value: dict) -> dict:
    """A user-owned exception pins one existing environment failure, never a suite."""
    import re
    from pathlib import PurePosixPath, PureWindowsPath
    fields = {"task_id", "scope_sha256", "head_sha", "receipt_sha256", "deferred_tests"}
    if type(value) is not dict or set(value) != fields:
        raise ValueError("task_validation_policy 必须精确绑定 task_id、scope_sha256、head_sha、receipt_sha256、deferred_tests。")
    if not isinstance(value["task_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", value["task_id"]):
        raise ValueError("task_validation_policy.task_id 无效。")
    for key in ("scope_sha256", "receipt_sha256"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{64}", value[key]):
            raise ValueError("task_validation_policy." + key + " 必须为 SHA256。")
    if not isinstance(value["head_sha"], str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value["head_sha"]):
        raise ValueError("task_validation_policy.head_sha 必须为完整提交。")
    rows = value["deferred_tests"]
    if type(rows) is not list or len(rows) != 1:
        raise ValueError("task_validation_policy 仅允许一个精确的额外打包环境失败。")
    row = rows[0]
    if (type(row) is not dict or set(row) != {"name", "failure_kind", "evidence_path", "evidence_sha256", "reason"}
            or row["name"] != "ZhuGuoCity.M2.PackageIntegration" or row["failure_kind"] != "live_coding_active"):
        raise ValueError("仅支持 PackageIntegration 的已证实 Live Coding 环境失败。")
    path = row["evidence_path"]
    if (not isinstance(path, str) or not path.startswith("workspace/Saved/Evidence/")
            or any(ord(c) < 32 or c in '\\:*?"<>|' for c in path)
            or PureWindowsPath(path).is_absolute() or PurePosixPath(path).is_absolute()
            or any(p in ("", ".", "..") or p.endswith((" ", ".")) for p in path.split("/"))
            or not path.endswith("/package-uat.log")):
        raise ValueError("evidence_path 必须为 runtime 内固定 Saved/Evidence 下普通 package-uat.log 相对路径。")
    if not isinstance(row["evidence_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["evidence_sha256"]):
        raise ValueError("evidence_sha256 必须为 SHA256。")
    if not isinstance(row["reason"], str) or not 1 <= len(row["reason"].strip()) <= 2000:
        raise ValueError("延期说明 reason 必须为 1..2000 字。")
    return {**value, "deferred_tests": [dict(row)]}


def load_config(path: Path) -> dict:
    path = Path(path).expanduser().resolve()
    config = read_json(path)
    if config.get("version") != 1:
        raise ValueError("配置 version 必须为 1。")
    if not isinstance(config.get("goal"), str) or not config["goal"].strip():
        raise ValueError("必须配置具体项目目标 goal。")
    config["config_path"] = str(path)
    for name in ("project_path", "runtime_dir"):
        if not isinstance(config.get(name), str) or not config[name]:
            raise ValueError(f"缺少 {name}。")
        config[name] = _path(config[name], path.parent)
    project, runtime = Path(config["project_path"]), Path(config["runtime_dir"])
    if runtime == project or runtime.is_relative_to(project) or project.is_relative_to(runtime):
        raise ValueError("runtime_dir 与原项目目录不能相互包含。")
    if not isinstance(config.get("branch"), str) or not config["branch"].startswith("unattended/"):
        raise ValueError("branch 必须为 unattended/ 下的独立工作分支。")
    config.setdefault("schedule", {})
    WorkCalendar(config["schedule"], base_dir=path.parent)
    config.setdefault("checks", [])
    if "task_validation_policy" in config:
        config["task_validation_policy"] = validate_task_validation_policy(config["task_validation_policy"])
    config.setdefault("milestone_checks", [])
    for name in ("checks", "milestone_checks"):
        if not isinstance(config[name], list):
            raise ValueError(f"{name} 必须为列表。")
    if config["milestone_checks"] and "publication" not in config:
        raise ValueError("milestone_checks 需要配置里程碑发布策略。")
    ids = set()
    for check in config["checks"] + config["milestone_checks"]:
        if not isinstance(check, dict) or not isinstance(check.get("id"), str) or not check["id"] or check["id"] in ids:
            raise ValueError("检查 id 必须非空且唯一。")
        ids.add(check["id"])
        if not isinstance(check.get("argv"), list) or not check["argv"] or not all(isinstance(x, str) and x and "\0" not in x for x in check["argv"]):
            raise ValueError("检查 argv 必须为非空参数列表。")
        check.setdefault("timeout_seconds", 600)
        if type(check["timeout_seconds"]) not in (int, float) or not 1 <= check["timeout_seconds"] <= 86400:
            raise ValueError("检查 timeout_seconds 应在 1..86400。")
        for field in ("success_patterns", "failure_patterns"):
            if field in check and (not isinstance(check[field], list) or
                                   any(not isinstance(x, str) or not x or len(x) > 4096 for x in check[field])):
                raise ValueError(f"检查 {field} 必须为非空文字片段的列表。")
    for key, default, minimum, maximum in (("max_reworks", 3, 0, 20), ("max_failures", 3, 1, 20),
                                          ("poll_seconds", 30, 1, 60), ("step_timeout_seconds", 3600, 1, 86400)):
        config.setdefault(key, default)
        if type(config[key]) is not int or not minimum <= config[key] <= maximum:
            raise ValueError(f"{key} 必须为 {minimum}..{maximum} 的整数。")
    if "code_timeout_seconds" in config and (type(config["code_timeout_seconds"]) is not int or
                                             not 1 <= config["code_timeout_seconds"] <= 86400):
        raise ValueError("code_timeout_seconds 必须为 1..86400 的整数。")
    config.setdefault("close_policy", "strict")
    if config["close_policy"] not in {"finish_step", "strict"}:
        raise ValueError("close_policy 必须为 finish_step 或 strict。")
    config.setdefault("push", False)
    if type(config["push"]) is not bool:
        raise ValueError("push 必须为布尔值。")
    config.setdefault("remote", "origin")
    config.setdefault("task_prefix", "TASK-AUTO")
    import re
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,60}", config["task_prefix"]):
        raise ValueError("task_prefix 只能含字母数字下划线及连字符。")
    if "publication" in config:
        publication = config["publication"]
        if not isinstance(publication, dict) or set(publication) != {"mode", "milestone_id", "goal"}:
            raise ValueError("publication 必须仅包含 mode、milestone_id、goal 三个字段。")
        if publication["mode"] != "milestone":
            raise ValueError("publication.mode 仅支持 milestone。")
        milestone_id = publication["milestone_id"]
        if not isinstance(milestone_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,59}", milestone_id):
            raise ValueError("publication.milestone_id 必须为 1..60 位字母数字下划线或连字符，且以字母数字开头。")
        if not isinstance(publication["goal"], str) or not publication["goal"].strip():
            raise ValueError("publication.goal 必须明确填写阶段完整范围及完成条件。")
    coder = config.get("coding_provider", "dsh")
    if coder not in ("dsh", "claude"):
        raise ValueError("coding_provider 必须为 dsh 或 claude。")
    planner = config.setdefault("planning_provider", "codex")
    if planner not in ("codex", "claude"):
        raise ValueError("planning_provider 必须为 codex 或 claude。")
    for key in dict.fromkeys(("codex", planner, coder)):
        if not isinstance(config.get(key), dict):
            raise ValueError(f"缺少 {key} 配置。")
        validate_model_settings(config[key], key)
        if "command" in config[key]:
            command = config[key]["command"]
            if (not isinstance(command, list) or not command or
                    any(not isinstance(x, str) or not x or "\0" in x for x in command)):
                raise ValueError(f"{key}.command 必须为非空原生程序参数列表。")
            if Path(command[0]).suffix.lower() in {".cmd", ".bat", ".ps1"}:
                raise ValueError(f"{key}.command 请使用原生程序，或 node.exe 加 JS 入口。")
        for field in ("home", "desktop_install"):
            if config[key].get(field):
                config[key][field] = _path(config[key][field], path.parent)
        if "patches" in config[key]:
            if not isinstance(config[key]["patches"], list) or any(not isinstance(x, str) or not x for x in config[key]["patches"]):
                raise ValueError(f"{key}.patches 必须为路径列表。")
            config[key]["patches"] = [_path(x, path.parent) for x in config[key]["patches"]]
    if config["codex"].get("sandbox", "read-only") != "read-only":
        raise ValueError("Codex 规划和验收必须使用 read-only。")
    if config["codex"].get("auth_mode", "inherit") not in ("inherit", "api_key"):
        raise ValueError("codex.auth_mode 必须为 inherit 或 api_key。")
    subscription_fallback = config["codex"].get("subscription_fallback", False)
    if type(subscription_fallback) is not bool or (subscription_fallback and config["codex"].get("auth_mode") != "api_key"):
        raise ValueError("codex.subscription_fallback 必须为布尔值，且仅可在 auth_mode 为 api_key 时启用。")
    if "claude" in (planner, coder) and "max_output_bytes" in config["claude"]:
        limit = config["claude"]["max_output_bytes"]
        if type(limit) is not int or not 1024 <= limit <= 256 * 1024 * 1024:
            raise ValueError("claude.max_output_bytes 必须为 1024 至 268435456 之间的整数字节数。")
    if "claude" in (planner, coder):
        settings = config["claude"]
        if "critical_plan_reasoning_effort" in settings and "critical_plan_model" not in settings:
            raise ValueError("claude.critical_plan_reasoning_effort 需要配置 critical_plan_model。")
        if "critical_plan_model" in settings:
            if planner != "claude":
                raise ValueError("claude.critical_plan_model 需要 planning_provider 为 claude。")
            if "model" not in settings:
                raise ValueError("claude.critical_plan_model 需要显式配置日常规划及编码的 claude.model。")
            critical = {"model": settings["critical_plan_model"]}
            if "critical_plan_reasoning_effort" in settings:
                critical["reasoning_effort"] = settings["critical_plan_reasoning_effort"]
            try:
                validate_model_settings(critical, "claude")
            except ValueError as error:
                raise ValueError("Claude 关键规划模型配置无效。" + str(error)) from error
    if coder == "claude":
        planned_tests = config["claude"].get("planned_core_tests", False)
        if type(planned_tests) is not bool:
            raise ValueError("claude.planned_core_tests 必须为布尔值。")
        if planned_tests and (config["claude"].get("ue_preflight") is not True or
                              not {"ue-editor-build", "ue-task-validation"} <=
                              {check["id"] for check in config["checks"]}):
            raise ValueError("任务书核心测试需要 UE 编译预检与独立验证。")
    if coder == "dsh" and config["dsh"].get("permission_mode", "workspace-write") not in {"workspace-write", "danger-full-access"}:
        raise ValueError("DSH permission_mode 应为 workspace-write 或 danger-full-access。")
    if "art_pipeline" in config:
        if coder != "claude":
            raise ValueError("art_pipeline 任务执行入口需要 coding_provider 为 claude。")
        config["art_pipeline"] = validate_art_pipeline_settings(config["art_pipeline"], project=project, runtime=runtime)
    if "art_worker" in config:
        config["art_worker"] = validate_art_worker_settings(config["art_worker"], project=project, runtime=runtime)
    if "art_promotion" in config:
        if "art_pipeline" not in config or coder != "claude" or "art_worker" in config:
            raise ValueError("art_promotion 只能配置在独立美术 worker 中。")
        config["art_promotion"] = validate_art_promotion_settings(config["art_promotion"], project=project, runtime=runtime)
    if "art_autonomy" in config:
        if "art_pipeline" not in config or coder != "claude" or "art_worker" in config:
            raise ValueError("art_autonomy 只能配置在独立 Claude 美术执行器中。")
        config["art_autonomy"] = validate_art_autonomy_settings(config["art_autonomy"])
    if "task_replanning" in config:
        config["task_replanning"] = validate_task_replanning_settings(config["task_replanning"])
        if config["task_replanning"]["enabled"] and (
                planner != "claude" or coder != "claude" or "art_pipeline" in config or
                not config.get("claude", {}).get("critical_plan_model") or
                not config.get("claude", {}).get("critical_plan_reasoning_effort")):
            raise ValueError("task_replanning 需要主功能 Claude 规划/编码及明确的 critical_plan_model/critical_plan_reasoning_effort。")
    return config


def init_config(path: Path, project: Path, goal: str, **kwargs) -> dict:
    path, project = Path(path).expanduser().resolve(), Path(project).expanduser().resolve()
    if path.exists():
        raise ValueError("配置已存在，保留原文件；请编辑或选择其他路径。")
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("请提供具体开发目标。")
    gitops.head(project)
    identity = hashlib.sha256(os.path.normcase(str(project)).encode()).hexdigest()[:12]
    dsh_install = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/DeepSeek Harness"
    holidays = path.parent / "unattended-holidays.json"
    if not holidays.exists():
        write_json(holidays, read_json(ROOT / "unattended/calendars/china-2026.json"))
    config = {
        "version": 1, "project_path": str(project), "goal": goal.strip(),
        "runtime_dir": str(path.parent / ".unattended" / identity),
        "branch": "unattended/" + identity, "base_ref": "HEAD", "task_prefix": "TASK-AUTO",
        "schedule": {"timezone": "Asia/Shanghai", "weekday_start": "18:00", "weekday_end": "08:00",
                     "weekends_all_day": True, "holidays_file": str(holidays)},
        "close_policy": "strict", "poll_seconds": 30, "step_timeout_seconds": 3600,
        "max_reworks": 3, "max_failures": 3, "push": False, "remote": "origin", "checks": [],
        "codex": {"command": [shutil.which("codex.exe") or shutil.which("codex") or "codex"], "sandbox": "read-only"},
        "dsh": {"desktop_install": str(dsh_install), "home": str(Path.home() / ".dsh"),
                "patches": [str(ROOT / "dsh-account.patch.yml")], "verified_version": "0.2.1-rc.3",
                "model": "deepseek-flash", "permission_mode": "workspace-write"},
    }
    config.update(kwargs)
    write_json(path, config)
    return load_config(path)


def doctor(config: dict) -> dict:
    checks = {}
    try:
        checks["project_head"] = gitops.head(Path(config["project_path"]))
        checks["git_identity"] = all(gitops.git(Path(config["project_path"]), "config", "--get", x) for x in ("user.name", "user.email"))
    except (RuntimeError, OSError) as error:
        checks["project_error"] = str(error)
    # Import locally: provider validation imports this module's model settings.
    from .providers import ProviderError, _codex_api_key, _codex_command
    try:
        command = _codex_command(config["codex"].get("command"))
        checks["codex_available"] = bool(shutil.which(command[0]) or Path(command[0]).is_file())
    except (ProviderError, OSError, ValueError):
        # Doctor is an inventory, including during an incomplete desktop update.
        checks["codex_available"] = False
    api_mode = config["codex"].get("auth_mode") == "api_key"
    codex_auth_ready = not api_mode or bool(_codex_api_key(config.get("config_path")))
    if api_mode:
        checks["codex_api_key_configured"] = codex_auth_ready
        checks["codex_subscription_fallback_enabled"] = config["codex"].get("subscription_fallback", False)
    uses_claude = "claude" in (config.get("planning_provider", "codex"), config.get("coding_provider", "dsh"))
    if uses_claude:
        command = config["claude"].get("command", ["claude"])
        checks["claude_available"] = bool(shutil.which(command[0]) or Path(command[0]).is_file())
    if config.get("coding_provider", "dsh") == "claude":
        coder_ready = checks["claude_available"]
    else:
        dsh = config["dsh"]
        checks["dsh_available"] = bool(dsh.get("command") and (shutil.which(dsh["command"][0]) or Path(dsh["command"][0]).is_file())) or (Path(dsh.get("desktop_install", "")) / "DeepSeek Harness.exe").is_file()
        checks["dsh_home_exists"] = Path(dsh["home"]).is_dir() if dsh.get("home") else bool(dsh.get("command"))
        checks["dsh_patches_exist"] = all(Path(x).is_file() for x in dsh.get("patches", []))
        coder_ready = all(checks[key] for key in ("dsh_available", "dsh_home_exists", "dsh_patches_exist"))
    checks["independent_checks_configured"] = bool(config["checks"])
    from datetime import datetime, timezone
    checks["calendar"] = WorkCalendar(config["schedule"], base_dir=Path(config["config_path"]).parent).describe(datetime.now(timezone.utc))
    planner_ready = not uses_claude or checks["claude_available"]
    ready = bool(checks.get("project_head")) and codex_auth_ready and planner_ready and coder_ready and checks["calendar"]["calendar_covered"] and all(checks.get(x) for x in ("git_identity", "codex_available", "independent_checks_configured"))
    return {"status": "ready" if ready else "needs_setup", "ready": ready, "checks": checks,
            "note": "只读检查未调用模型，也不验证账号登录或额度；checks 需配置项目实际可执行的验收命令。"}
