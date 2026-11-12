"""Promote user-approved art into the source game's asset library, never live code.

Only an accepted, hash-bound worker history authorizes this consumer. Staging is
isolated, joins are fast-forward only, and the existing game controller lock wins.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import time
import uuid

from . import gitops
from .art_pipeline import load_config as load_batch, require_free_project, verify_review
from .config import load_config
from .storage import atomic_replace, lock, read_json, write_json


class PromotionWait(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _under(root, relative):
    value = PurePosixPath(str(relative).replace("\\", "/"))
    if value.is_absolute() or not value.parts or any(part in {"..", "."} or ":" in part for part in value.parts):
        raise ValueError("Unsafe asset-relative path")
    path = Path(root).joinpath(*value.parts)
    if not path.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError("Asset path escapes its root")
    return path, value.as_posix()


def approved_payload(request, history):
    """Return only the current approved version, with all source bytes checked."""
    if (request.get("kind") != "approved_art" or history.get("status") != "accepted" or
            history.get("visual_decision", {}).get("decision") != "accepted" or
            request.get("approval") != history.get("visual_decision") or
            request.get("technical_review") != history.get("technical_review") or
            history.get("technical_review", {}).get("decision") != "accepted"):
        raise ValueError("Promotion requires persisted human and technical acceptance")
    frozen = history["task"]["art_request"]
    for key in ("config_file", "config_sha256", "artifacts_dir"):
        if request.get(key) != frozen.get(key):
            raise ValueError("Promotion differs from the approved task configuration")
    if digest(request["config_file"]) != request["config_sha256"]:
        raise ValueError("Approved batch configuration changed")
    cfg = load_batch(request["config_file"])
    root = Path(cfg["artifacts_dir"]).resolve()
    for name, file in (("review_package", root / "review-package.json"),
                       ("independent_check", root / "independent-check.json")):
        if (Path(request[name]).resolve() != file or request[name + "_sha256"] != history[name + "_sha256"] or
                digest(file) != request[name + "_sha256"]):
            raise ValueError("Promotion evidence no longer matches the accepted package")
    verified = verify_review(cfg, persist=False)
    if verified["review_package_sha256"] != request["review_package_sha256"]:
        raise ValueError("Promotion package changed during verification")
    report = read_json(root / "review-package.json")
    receipt = read_json(root / "independent-check.json")
    if receipt.get("status") != "passed" or receipt.get("review_package_sha256") != request["review_package_sha256"]:
        raise ValueError("Accepted independent check does not bind this package")
    if request.get("batch_id") != cfg["batch_id"] or request.get("approved_levels") != cfg["approved_levels"]:
        raise ValueError("Promotion scope differs from approval")
    imported = report["ue_import"]
    base = imported.get("asset_base", "")
    if not re.fullmatch(r"/Game/ArtPreview/Versions/V_[0-9a-f]{16}", base):
        raise ValueError("Promotion needs one immutable versioned UE asset base")
    prefix = "Content/" + base.removeprefix("/Game/") + "/"
    source = root / "UE/HousingPreview"
    files = []
    seen = set()
    for entry in imported["ue_files"]:
        path, relative = _under(source, entry["path"])
        if not relative.startswith(prefix):
            continue  # Never copy preview maps or older versions from the report.
        if not relative.endswith(".uasset") or relative in seen:
            raise ValueError("Approved version contains an unsupported or duplicate package")
        if digest(path) != entry["sha256"]:
            raise ValueError("An approved UE package changed")
        seen.add(relative)
        files.append({"path": relative, "source": str(path), "sha256": entry["sha256"], "size": path.stat().st_size})
    version_root = source / prefix
    if not files or {p.relative_to(source).as_posix() for p in version_root.rglob("*.uasset")} != seen:
        raise ValueError("Current asset version is incomplete or has unrecorded packages")
    mapping = imported.get("level_to_house")
    if not isinstance(mapping, dict) or set(mapping) != set(cfg["approved_levels"]):
        raise ValueError("Approved level mapping is incomplete")
    for object_path in mapping.values():
        if not isinstance(object_path, str) or not object_path.startswith(base + "/"):
            raise ValueError("Approved level references an external asset version")
        package = "Content/" + object_path.removeprefix("/Game/").split(".", 1)[0] + ".uasset"
        if package not in seen:
            raise ValueError("Approved house package is missing")
    manifest = Path(report["manifest"])
    if not manifest.resolve().is_relative_to(root):
        raise ValueError("Approved layout manifest is outside public artifacts")
    data = read_json(manifest)
    batch_id = cfg["batch_id"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", batch_id):
        raise ValueError("Unsafe batch identifier")
    # Keep the full digest in the catalog, with a checked short directory name
    # so atomic Windows writes stay below legacy path-length limits.
    catalog_root = f"Content/Data/ArtApproved/{batch_id}/P_{request['review_package_sha256'][:16]}"
    catalog = {"version": 1, "batch_id": batch_id, "status": "approved_asset_library",
               "review_package_sha256": request["review_package_sha256"], "approval": request["approval"],
               "asset_base": base, "levels": mapping, "manifest_sha256": digest(manifest),
               "manifest": catalog_root + "/assets_manifest.json", "coordinate_space": "preview_world_cm",
               "gameplay_binding": "not_modified", "plots": data.get("plots", []),
               "instances": data.get("instances", []),
               "ue_files": [{k: item[k] for k in ("path", "sha256", "size")} for item in files]}
    return {"files": files, "catalog": catalog, "catalog_path": catalog_root + "/catalog.json",
            "manifest_path": catalog_root + "/assets_manifest.json", "manifest_source": str(manifest)}


class ArtPromotion:
    def __init__(self, worker_config):
        self.config = worker_config
        self.runtime = Path(worker_config["runtime_dir"]).resolve()
        settings = worker_config.get("art_promotion")
        if not isinstance(settings, dict) or set(settings) != {"target_config_file", "target_branch", "push", "remote"}:
            raise ValueError("art_promotion requires target_config_file, target_branch, push, remote")
        if not isinstance(settings["target_config_file"], str) or not Path(settings["target_config_file"]).is_absolute():
            raise ValueError("Promotion target configuration must be absolute")
        target = load_config(Path(settings["target_config_file"]))
        self.target = Path(target["project_path"]).resolve()
        self.target_runtime = Path(target["runtime_dir"]).resolve()
        if (self.target == self.target_runtime or self.target.is_relative_to(self.target_runtime) or
                self.target_runtime.is_relative_to(self.target) or self.target == self.runtime or
                self.target.is_relative_to(self.runtime) or self.runtime.is_relative_to(self.target)):
            raise ValueError("Promotion target must be the independent source game, never a runtime workspace")
        if type(settings["push"]) is not bool or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", settings["remote"]):
            raise ValueError("Invalid promotion publication settings")
        self.settings = settings

    def _queue(self):
        path = self.runtime / "promotion-requests.json"
        queue = read_json(path) if path.exists() else {"version": 1, "requests": []}
        if queue.get("version") != 1 or not isinstance(queue.get("requests"), list):
            raise ValueError("Invalid promotion queue")
        return queue

    def _update(self, request_id, **values):
        with lock(self.runtime / "promotion-requests.lock"):
            queue = self._queue()
            item = next(item for item in queue["requests"] if item["id"] == request_id)
            item.update(values, updated_epoch=time.time())
            write_json(self.runtime / "promotion-requests.json", queue)
            return item

    def _target_ready(self, payload=None):
        if gitops.git(self.target, "symbolic-ref", "--short", "HEAD") != self.settings["target_branch"]:
            raise PromotionWait("waiting_target_branch", "Target game branch differs from trusted promotion policy")
        if gitops.git(self.target, "status", "--porcelain=v1", "--untracked-files=no"):
            raise PromotionWait("waiting_target_clean", "Target game has tracked changes; nothing was cleaned or overwritten")
        if payload is not None:
            roots = (PurePosixPath("Content/" + payload["catalog"]["asset_base"].removeprefix("/Game/")),
                     PurePosixPath(payload["catalog_path"]).parent)
            # Include ignored user files too: Git checkout may overwrite those.
            # Limit the search to the two approved namespaces, not Saved/Binaries.
            untracked = gitops.git(self.target, "ls-files", "--others", "--directory", "-z", "--",
                                   *(root.as_posix() for root in roots)).split("\0")
            if any(path and any(PurePosixPath(path) == root or PurePosixPath(path).is_relative_to(root)
                                or root.is_relative_to(PurePosixPath(path)) for root in roots) for path in untracked):
                raise PromotionWait("waiting_target_clean", "Untracked user files overlap approved asset destinations; nothing was overwritten")
        projects = list(self.target.glob("*.uproject"))
        if len(projects) != 1:
            raise ValueError("Promotion target requires exactly one UE project")
        try:
            require_free_project(projects[0])
        except RuntimeError as error:
            raise PromotionWait("waiting_target_editor", "Target editor is open or its ownership could not be verified") from error

    @staticmethod
    def _copy_checked(source, target, expected):
        if digest(source) != expected:
            raise ValueError("Source changed while staging approved content")
        if target.exists():
            if digest(target) != expected:
                raise ValueError("An immutable approved asset path already has different bytes")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".copy-" + uuid.uuid4().hex[:8])
        try:
            shutil.copyfile(source, temporary)
            if digest(temporary) != expected:
                raise ValueError("Staged content hash mismatch")
            if target.exists():
                if digest(target) != expected:
                    raise ValueError("Destination appeared with different bytes during staging")
            else:
                atomic_replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _stage(self, request, payload, base, receipt_path):
        stage = self.runtime / "promotion-staging" / request["id"][-24:] / base[:12]
        stage.parent.mkdir(parents=True, exist_ok=True)
        branch = "task/" + request["source_task_id"] + "-IMPORT"
        if not stage.exists():
            gitops.git(self.target, "clone", "--no-hardlinks", "--no-checkout", "--", str(self.target), str(stage), timeout=600)
            gitops.git(stage, "checkout", "-b", branch, base, timeout=300)
            for key in ("user.name", "user.email"):
                gitops.git(stage, "config", key, gitops.git(self.target, "config", "--get", key))
        if gitops.git(stage, "symbolic-ref", "--short", "HEAD") != branch:
            raise ValueError("Promotion staging branch changed")
        gitops.git(stage, "lfs", "version")
        gitops.git(stage, "lfs", "install", "--local")
        allowed = []
        for entry in payload["files"]:
            dest, name = _under(stage, entry["path"])
            self._copy_checked(entry["source"], dest, entry["sha256"])
            if gitops.git(stage, "check-attr", "filter", "--", name).split(": ")[-1] != "lfs":
                raise ValueError("UE packages must already be covered by the target's Git LFS policy")
            allowed.append(name)
        manifest, name = _under(stage, payload["manifest_path"])
        self._copy_checked(payload["manifest_source"], manifest, payload["catalog"]["manifest_sha256"])
        allowed.append(name)
        catalog, name = _under(stage, payload["catalog_path"])
        if catalog.exists() and read_json(catalog) != payload["catalog"]:
            raise ValueError("Immutable approved catalog path already differs")
        write_json(catalog, payload["catalog"])
        allowed.append(name)
        gitops.git(stage, "add", "--", *allowed, timeout=300)
        staged = set(gitops.git(stage, "diff", "--cached", "--name-only", "-z").split("\0")) - {""}
        if not staged <= set(allowed):
            raise ValueError("Promotion stage contains changes outside approved content")
        if staged:
            gitops.git(stage, "commit", "-m", request["source_task_id"], timeout=300)
        candidate = gitops.head(stage)
        changed = set(gitops.git(stage, "diff", "--name-only", "-z", base, candidate).split("\0")) - {""}
        if not changed <= set(allowed) or not gitops.clean(stage):
            raise ValueError("Promotion commit contains unexpected changes")
        gitops.git(stage, "lfs", "fsck", timeout=300)
        receipt = {"version": 1, "request_id": request["id"], "base_sha": base, "candidate_sha": candidate,
                   "staging": str(stage), "target": str(self.target), "target_branch": self.settings["target_branch"],
                   "review_package_sha256": request["review_package_sha256"], "files": allowed,
                   "status": "staged", "gameplay_binding": "not_modified"}
        write_json(receipt_path, receipt)  # Durable candidate before any target write.
        return receipt

    def _transfer_lfs(self, receipt, payload):
        stage = Path(receipt["staging"])
        source_git, target_git = gitops.common_dir(stage), gitops.common_dir(self.target)
        for entry in payload["files"]:
            pointer = gitops.git(stage, "show", receipt["candidate_sha"] + ":" + entry["path"])
            expected = f"version https://git-lfs.github.com/spec/v1\noid sha256:{entry['sha256']}\nsize {entry['size']}"
            if pointer != expected:
                raise ValueError("Approved package was not committed as its verified LFS object")
            oid = entry["sha256"]
            relative = Path("lfs/objects") / oid[:2] / oid[2:4] / oid
            self._copy_checked(source_git / relative, target_git / relative, oid)

    def _validate_candidate(self, receipt, request, payload):
        for field in ("base_sha", "candidate_sha"):
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", receipt.get(field, "")):
                raise ValueError("Invalid promotion commit identity")
        expected = self.runtime / "promotion-staging" / request["id"][-24:] / receipt["base_sha"][:12]
        stage = Path(receipt["staging"])
        if stage.resolve() != expected.resolve() or receipt.get("target_branch") != self.settings["target_branch"]:
            raise ValueError("Promotion staging receipt is outside its bound workspace")
        if gitops.head(stage) != receipt["candidate_sha"] or not gitops.clean(stage):
            raise ValueError("Promotion staging commit or files changed")
        gitops.git(stage, "merge-base", "--is-ancestor", receipt["base_sha"], receipt["candidate_sha"])
        revision_range = receipt["base_sha"] + ".." + receipt["candidate_sha"]
        if gitops.git(stage, "rev-list", "--merges", revision_range):
            raise ValueError("Promotion commit cannot merge unrelated work")
        allowed = {item["path"] for item in payload["files"]} | {payload["catalog_path"], payload["manifest_path"]}
        changed = set(gitops.git(stage, "diff", "--name-only", "-z", receipt["base_sha"], receipt["candidate_sha"]).split("\0")) - {""}
        if not changed <= allowed or set(receipt.get("files", [])) != allowed:
            raise ValueError("Promotion candidate contains non-approved changes")
        catalog = json.loads(gitops.git(stage, "show", receipt["candidate_sha"] + ":" + payload["catalog_path"]))
        if catalog != payload["catalog"]:
            raise ValueError("Promotion catalog no longer matches the accepted package")
        manifest, _ = _under(stage, payload["manifest_path"])
        if digest(manifest) != payload["catalog"]["manifest_sha256"]:
            raise ValueError("Promotion layout manifest no longer matches accepted bytes")

    def _execute(self, request):
        if not re.fullmatch(r"art-promotion-[0-9a-f]{24}", request.get("id", "")):
            raise ValueError("Invalid promotion request identity")
        state = read_json(self.runtime / "state.json")
        history = next((item for item in state.get("art_history", []) if item["id"] == request["source_task_id"]
                        and item["review_package_sha256"] == request["review_package_sha256"]), None)
        if history is None:
            raise ValueError("Promotion request has no accepted worker history")
        receipt_path = self.runtime / "promotions" / request["id"] / "receipt.json"
        with ExitStack() as stack:
            try:
                stack.enter_context(lock(gitops.common_dir(self.target) / "unattended-controller.lock"))
            except (OSError, RuntimeError) as error:
                raise PromotionWait("waiting_target_busy", "Functional controller owns the source project; promotion remains queued") from error
            self._target_ready()
            payload = approved_payload(request, history)
            self._target_ready(payload)
            current = gitops.head(self.target)
            receipt = read_json(receipt_path) if receipt_path.exists() else None
            if receipt and (receipt.get("request_id") != request["id"] or receipt.get("target") != str(self.target) or
                            receipt.get("review_package_sha256") != request["review_package_sha256"]):
                raise ValueError("Promotion receipt belongs to another target or approval")
            if receipt and current == receipt["candidate_sha"]:
                pass  # Previous process joined successfully before writing its receipt.
            else:
                if receipt and current != receipt["base_sha"]:
                    receipt = None  # Re-stage add-only content on the new clean target HEAD.
                if receipt is None:
                    receipt_path.parent.mkdir(parents=True, exist_ok=True)
                    receipt = self._stage(request, payload, current, receipt_path)
                self._validate_candidate(receipt, request, payload)
                self._target_ready(payload)
                if gitops.head(self.target) != receipt["base_sha"]:
                    raise PromotionWait("waiting_target_advanced", "Target changed while staging; rebuild on its next safe HEAD")
                self._transfer_lfs(receipt, payload)
                gitops.git(self.target, "fetch", "--no-tags", "--", receipt["staging"], receipt["candidate_sha"], timeout=300)
                gitops.git(self.target, "merge", "--ff-only", "--no-edit", receipt["candidate_sha"], timeout=300)
            if (gitops.head(self.target) != receipt["candidate_sha"] or
                    gitops.git(self.target, "status", "--porcelain=v1", "--untracked-files=no")):
                raise ValueError("Target did not reach the exact clean asset-library commit")
            for entry in payload["files"]:
                target_file, _ = _under(self.target, entry["path"])
                if digest(target_file) != entry["sha256"]:
                    raise ValueError("Joined target LFS content differs from approved bytes")
            if self.settings["push"]:
                publication = gitops.publish(self.target, {"remote": self.settings["remote"],
                                            "branch": self.settings["target_branch"]}, receipt["candidate_sha"])
            else:
                publication = {"local_only": True}
            receipt.update(status="integrated", publication=publication, integrated_epoch=time.time())
            write_json(receipt_path, receipt)
            return {"status": "integrated", "receipt": str(receipt_path), "head_sha": receipt["candidate_sha"],
                    "target_project": str(self.target), "gameplay_binding": "not_modified"}

    def step(self):
        with lock(self.runtime / "art-promotion.lock"):
            request = next((item for item in self._queue()["requests"] if item.get("status") not in {"integrated", "blocked"}), None)
            if request is None:
                return {"status": "idle"}
            try:
                result = self._execute(request)
            except PromotionWait as error:
                result = {"status": error.status, "message": str(error)}
            except Exception as error:
                # Never expose remote URLs or private ledger data in dashboard errors.
                message = re.sub(r"https?://\S+", "[remote]", str(error))[:1000]
                result = {"status": "blocked", "message": type(error).__name__ + ": " + message}
            self._update(request["id"], **result)
            return {"request_id": request["id"], **result}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("command", choices=("step",))
    args = parser.parse_args(argv)
    result = ArtPromotion(load_config(args.config)).step()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
