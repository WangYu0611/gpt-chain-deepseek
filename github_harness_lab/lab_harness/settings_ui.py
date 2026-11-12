"""Local repository settings window. Saving never runs a workflow or a model."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from .repository_setup import (
    RepositorySettings, build_setup_request, check_environment,
    export_setup_bundle, load_settings, normalize_repository,
    save_settings, validate_settings,
)

BG = "#F3F5F9"
INK = "#17263B"
MUTED = "#596A80"
BLUE = "#245BDD"


class SettingsWindow:
    def __init__(self, window: tk.Tk, root: Path):
        self.window, self.root = window, root.resolve()
        self.config_path = self.root / "config.local.json"
        self.loading = True
        self.dirty = False
        window.title("GitHub × DSH · 仓库设置")
        window.geometry("1040x900")
        window.minsize(930, 840)
        window.configure(bg=BG)
        window.option_add("*Font", "{Microsoft YaHei UI} 10")
        style = ttk.Style(window)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background="white")
        style.configure("TLabel", background=BG, foreground=INK)
        style.configure("Card.TLabel", background="white", foreground=INK)
        style.configure("Hint.TLabel", background="white", foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("TEntry", padding=8, fieldbackground="white", bordercolor="#CDD6E3")
        style.configure("TButton", padding=(15, 9), font=("Microsoft YaHei UI", 10), foreground=INK)
        style.configure("Primary.TButton", background=BLUE, foreground="white", borderwidth=0)
        style.map("Primary.TButton", background=[("active", "#194CC4"), ("pressed", "#143EA5")], foreground=[("disabled", "#DDE6FF")])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(22, 10), background="#E8EDF5")
        style.map("TNotebook.Tab", background=[("selected", "white")], foreground=[("selected", BLUE)])
        style.configure("Treeview", rowheight=38, font=("Microsoft YaHei UI", 10), background="white", fieldbackground="white", borderwidth=0)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"), padding=10)

        outer = ttk.Frame(window, padding=(26, 20, 26, 16))
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 16))
        ttk.Label(header, text="GitHub × DSH", font=("Segoe UI", 11, "bold"), foreground=BLUE).pack(anchor="w")
        title_row = ttk.Frame(header)
        title_row.pack(fill="x", pady=(4, 4))
        ttk.Label(title_row, text="仓库设置", font=("Microsoft YaHei UI", 23, "bold")).pack(side="left")
        tk.Label(title_row, text="  本地配置 · 未启用  ", bg="#E8EEFF", fg=BLUE, padx=10, pady=6).pack(side="right")
        ttk.Label(header, text="填写目标仓库，保存配置，再完成一次接入部署。", foreground=MUTED).pack(anchor="w")

        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill="both", expand=True)
        self.form_tab = ttk.Frame(self.tabs, style="Card.TFrame", padding=(24, 20))
        self.guide_tab = ttk.Frame(self.tabs, style="Card.TFrame", padding=(24, 20))
        self.check_tab = ttk.Frame(self.tabs, style="Card.TFrame", padding=(24, 20))
        self.tabs.add(self.form_tab, text="仓库配置")
        self.tabs.add(self.guide_tab, text="接入与使用")
        self.tabs.add(self.check_tab, text="本机检查")
        self.fields = {key: tk.StringVar() for key in ("repository", "default_branch", "trusted_actor", "source_repository")}
        self._build_form()
        self._build_guide()
        self._build_checks()

        footer = ttk.Frame(outer, padding=(0, 12, 0, 0))
        footer.pack(side="bottom", fill="x", before=self.tabs)
        self.status = tk.StringVar(value="尚未保存")
        ttk.Label(footer, textvariable=self.status, foreground=BLUE, wraplength=960).pack(anchor="w")
        ttk.Label(footer, text="配置文件：" + str(self.config_path), foreground=MUTED, font=("Microsoft YaHei UI", 8), wraplength=960).pack(anchor="w", pady=(6, 0))
        self.tabs.bind("<<NotebookTabChanged>>", self._tab_changed)
        for variable in self.fields.values():
            variable.trace_add("write", self._changed)
        window.protocol("WM_DELETE_WINDOW", self._close)
        self._load()

    def _build_form(self):
        frame = self.form_tab
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="GitHub 仓库地址", style="Card.TLabel", font=("Microsoft YaHei UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        self.repo_entry = ttk.Entry(frame, textvariable=self.fields["repository"])
        self.repo_entry.grid(row=1, column=0, sticky="ew", pady=(7, 4))
        ttk.Label(frame, text="支持 https://github.com/用户名/仓库名、用户名/仓库名 或 Git SSH 地址。", style="Hint.TLabel").grid(row=2, column=0, sticky="w")

        pair = ttk.Frame(frame, style="Card.TFrame")
        pair.grid(row=3, column=0, sticky="ew", pady=(18, 16))
        pair.columnconfigure(0, weight=1)
        pair.columnconfigure(1, weight=1)
        ttk.Label(pair, text="默认分支", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(pair, text="允许派工的 GitHub 账号", style="Card.TLabel").grid(row=0, column=1, sticky="w", padx=(20, 0))
        ttk.Entry(pair, textvariable=self.fields["default_branch"]).grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Entry(pair, textvariable=self.fields["trusted_actor"]).grid(row=1, column=1, sticky="ew", padx=(20, 0), pady=(6, 0))
        ttk.Label(pair, text="一般为 main；请与仓库实际设置一致。", style="Hint.TLabel").grid(row=2, column=0, sticky="w", pady=(4, 0))
        ttk.Label(pair, text="填写账号名称；不需要密码、Token 或 API Key。", style="Hint.TLabel").grid(row=2, column=1, sticky="w", padx=(20, 0), pady=(4, 0))

        ttk.Label(frame, text="本地项目目录（选填）", style="Card.TLabel").grid(row=4, column=0, sticky="w")
        directory = ttk.Frame(frame, style="Card.TFrame")
        directory.grid(row=5, column=0, sticky="ew", pady=(6, 4))
        directory.columnconfigure(0, weight=1)
        ttk.Entry(directory, textvariable=self.fields["source_repository"]).grid(row=0, column=0, sticky="ew")
        ttk.Button(directory, text="选择文件夹…", command=self._browse).grid(row=0, column=1, padx=(10, 0))
        ttk.Label(frame, text="选择该仓库的本地代码目录；尚未下载时可以留空。", style="Hint.TLabel").grid(row=6, column=0, sticky="w")

        ttk.Label(frame, text="希望这个项目做什么（选填）", style="Card.TLabel").grid(row=7, column=0, sticky="w", pady=(16, 6))
        self.goal = tk.Text(frame, height=3, wrap="word", font=("Microsoft YaHei UI", 10), bg="#FAFBFE", fg=INK, relief="solid", borderwidth=1, padx=10, pady=8, undo=True)
        self.goal.grid(row=8, column=0, sticky="nsew")
        frame.rowconfigure(8, weight=1)
        self.goal.bind("<<Modified>>", self._goal_changed)

        note = tk.Label(frame, text="保存后状态为「待接入」。此窗口不会创建 Issue、调用 DSH 或启动 Runner。", bg="#EFF4FF", fg="#365586", anchor="w", padx=12, pady=10, font=("Microsoft YaHei UI", 9))
        note.grid(row=9, column=0, sticky="ew", pady=(16, 14))
        buttons = ttk.Frame(frame, style="Card.TFrame")
        buttons.grid(row=10, column=0, sticky="ew")
        ttk.Button(buttons, text="保存配置", style="Primary.TButton", command=self._save).pack(side="left")
        ttk.Button(buttons, text="生成通用部署包", command=self._export).pack(side="left", padx=10)
        ttk.Button(buttons, text="打开仓库", command=self._open_repo).pack(side="left")
        ttk.Button(buttons, text="重新读取", command=self._load).pack(side="right")

    def _build_guide(self):
        frame = self.guide_tab
        ttk.Label(frame, text="从配置到使用", style="Card.TLabel", font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        instructions = (
            "1. 在「仓库配置」里填写地址并保存。\n"
            "2. 生成通用部署包，或复制下方请求发回当前 Codex 对话。真实启动前再确认。\n"
            "3. 接入完成后，网页 GPT 发 Issue → 本地 DSH 执行 → 提交报告 → GPT 发下一项。\n\n"
            "启动任务与报告任务必须使用独立网页对话。本机需要开机并运行 Runner。\n"
            "执行器由多个项目共用；换仓库只生成新配置。当前支持两轮文档交接，代码开发尚未启用。"
        )
        ttk.Label(frame, text=instructions, style="Hint.TLabel", wraplength=880, justify="left").pack(anchor="w", pady=(12, 18))
        ttk.Label(frame, text="接入请求预览", style="Card.TLabel", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        preview_frame = ttk.Frame(frame, style="Card.TFrame")
        preview_frame.pack(fill="both", expand=True, pady=(8, 14))
        self.request_preview = tk.Text(preview_frame, wrap="word", height=10, bg="#FAFBFE", fg=INK, relief="solid", borderwidth=1, padx=12, pady=10, font=("Microsoft YaHei UI", 10))
        preview_scroll = ttk.Scrollbar(preview_frame, command=self.request_preview.yview)
        preview_scroll.pack(side="right", fill="y")
        self.request_preview.configure(yscrollcommand=preview_scroll.set)
        self.request_preview.pack(fill="both", expand=True)
        actions = ttk.Frame(frame, style="Card.TFrame")
        actions.pack(fill="x")
        ttk.Button(actions, text="保存并复制接入请求", style="Primary.TButton", command=self._copy_request).pack(side="left")
        ttk.Button(actions, text="打开 ChatGPT 网页任务", command=lambda: self._open_url("https://chatgpt.com/scheduled")).pack(side="left", padx=10)
        ttk.Button(actions, text="查看上次测试记录", command=self._show_result).pack(side="right")

    def _build_checks(self):
        frame = self.check_tab
        ttk.Label(frame, text="本机准备情况", style="Card.TLabel", font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        ttk.Label(frame, text="只检查本地文件和可用程序；不会登录 GitHub、读取密钥或发出模型请求。", style="Hint.TLabel", wraplength=880).pack(anchor="w", pady=(10, 18))
        self.checks = ttk.Treeview(frame, columns=("name", "status", "detail"), show="headings", selectmode="browse", height=7)
        for key, title, width in (("name", "检查项", 160), ("status", "状态", 110), ("detail", "说明", 580)):
            self.checks.heading(key, text=title)
            self.checks.column(key, width=width, minwidth=80, stretch=(key == "detail"))
        self.checks.pack(fill="both", expand=True)
        self.check_detail = tk.StringVar(value="检查后选择任一项，可在这里查看完整说明。")
        ttk.Label(frame, textvariable=self.check_detail, style="Hint.TLabel", wraplength=850, justify="left").pack(fill="x", pady=(12, 0))
        self.checks.bind("<<TreeviewSelect>>", self._check_selected)
        self.checks.tag_configure("ok", foreground="#166449")
        self.checks.tag_configure("warn", foreground="#946320")
        self.checks.tag_configure("info", foreground=MUTED)
        ttk.Button(frame, text="检查本机", style="Primary.TButton", command=self._check).pack(anchor="w", pady=(16, 0))

    def _read(self):
        return validate_settings(RepositorySettings(**{key: var.get() for key, var in self.fields.items()}, project_goal=self.goal.get("1.0", "end-1c")))

    def _set(self, settings):
        self.loading = True
        for key, var in self.fields.items():
            value = getattr(settings, key)
            var.set("https://github.com/" + value if key == "repository" else value)
        self.goal.delete("1.0", "end")
        self.goal.insert("1.0", settings.project_goal)
        self.goal.edit_modified(False)
        self.loading = False
        self.dirty = False

    def _load(self):
        if not self.loading and self.dirty and not messagebox.askyesno("重新读取", "放弃窗口中的未保存修改，重新读取配置？", parent=self.window):
            return
        try:
            settings = load_settings(self.root)
            self._set(settings)
            self.status.set("已读取配置 · 待接入部署" if self.config_path.exists() else "已预填上次测试仓库。可以直接更换地址，再保存。")
        except (ValueError, OSError, TypeError) as error:
            self._set(RepositorySettings())
            self.status.set("原配置读取失败，文件未覆盖：" + str(error))
        self._preview()

    def _changed(self, *_):
        if not self.loading:
            self.dirty = True
            self.status.set("有未保存的更改")

    def _goal_changed(self, _):
        if self.goal.edit_modified():
            self._changed()
            self.goal.edit_modified(False)

    def _browse(self):
        path = filedialog.askdirectory(title="选择仓库的本地项目目录", parent=self.window)
        if path:
            self.fields["source_repository"].set(path)

    def _save(self):
        try:
            settings = self._read()
            self.config_path = save_settings(self.root, settings)
            self._set(settings)
            self.status.set("已保存：" + settings.repository + " · 待接入部署，未启动任何任务")
            self._preview()
            return settings
        except (ValueError, OSError, TypeError) as error:
            messagebox.showerror("配置未保存", str(error), parent=self.window)
            return None

    def _preview(self):
        try:
            text = build_setup_request(self._read(), self.config_path)
        except (ValueError, OSError, TypeError) as error:
            text = "请先在「仓库配置」里填写有效配置。\n\n" + str(error)
        self.request_preview.configure(state="normal")
        self.request_preview.delete("1.0", "end")
        self.request_preview.insert("1.0", text)
        self.request_preview.configure(state="disabled")

    def _tab_changed(self, _):
        if hasattr(self, "request_preview") and self.tabs.select() == str(self.guide_tab):
            self._preview()

    def _copy_request(self):
        settings = self._save()
        if settings is not None:
            self.window.clipboard_clear()
            self.window.clipboard_append(build_setup_request(settings, self.config_path))
            self.status.set("接入请求已复制。粘贴回当前 Codex 对话即可；本次没有发送。")

    def _export(self):
        try:
            settings = self._read()
            output = export_setup_bundle(self.root, settings)
            self._set(settings)
            self._preview()
            self.status.set("已生成本地接入资料：" + str(output))
            self.tabs.select(self.guide_tab)
            messagebox.showinfo("通用部署包已生成", "配置已保存。\n\n资料目录：\n" + str(output) + "\n\ngeneric 子目录包含通用执行器、项目配置、工作流与网页任务草案。全部只保存在本机，尚未部署或启用。", parent=self.window)
        except (ValueError, OSError, TypeError) as error:
            messagebox.showerror("未生成接入资料", str(error), parent=self.window)

    def _check(self):
        try:
            results = check_environment(self.root, self._read())
        except (ValueError, OSError, TypeError) as error:
            messagebox.showerror("检查未完成", str(error), parent=self.window)
            return
        for child in self.checks.get_children():
            self.checks.delete(child)
        labels = {"ok": "已找到", "warn": "待准备", "info": "说明"}
        for item in results:
            self.checks.insert("", "end", values=(item["name"], labels[item["status"]], item["detail"]), tags=(item["status"],))
        self.status.set("本机检查完成；GitHub 权限与运行状态需要在接入时联网核验。")

    def _check_selected(self, _):
        selected = self.checks.selection()
        if selected:
            values = self.checks.item(selected[0], "values")
            self.check_detail.set(values[0] + "：" + values[2])

    def _open_url(self, url):
        try:
            if not webbrowser.open(url):
                raise OSError("系统没有可用的默认浏览器。请手动打开：" + url)
        except OSError as error:
            messagebox.showerror("无法打开网页", str(error), parent=self.window)

    def _open_repo(self):
        try:
            self._open_url("https://github.com/" + normalize_repository(self.fields["repository"].get()))
        except ValueError as error:
            messagebox.showerror("仓库地址无效", str(error), parent=self.window)

    def _show_result(self):
        path = self.root / "docs" / "SERIAL_TWO_ROUND_RESULT.md"
        try:
            content = path.read_text(encoding="utf-8-sig")
        except OSError as error:
            messagebox.showerror("无法读取测试记录", str(error), parent=self.window)
            return
        dialog = tk.Toplevel(self.window)
        dialog.title("上次测试记录 · 2026-10-28")
        dialog.geometry("920x650")
        text = tk.Text(dialog, wrap="word", font=("Microsoft YaHei UI", 10), padx=20, pady=16)
        scrollbar = ttk.Scrollbar(dialog, command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        text.insert("1.0", content)
        text.configure(state="disabled")

    def _close(self):
        if self.dirty:
            answer = messagebox.askyesnocancel("未保存的配置", "关闭前保存配置吗？", parent=self.window)
            if answer is None or (answer and self._save() is None):
                return
        self.window.destroy()


def main(argv=None, *, root=None):
    parser = argparse.ArgumentParser(description="GitHub × DSH local repository settings")
    parser.add_argument("--config-root", type=Path, default=root or Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    window = tk.Tk()
    SettingsWindow(window, args.config_root)
    window.mainloop()


if __name__ == "__main__":
    main()
