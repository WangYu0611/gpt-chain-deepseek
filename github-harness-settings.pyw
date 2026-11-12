"""Double-click entry for the GitHub harness repository settings."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "github_harness_lab"))

if __name__ == "__main__":
    try:
        from lab_harness.settings_ui import main
        main(root=ROOT / "github_harness_lab")
    except Exception as error:
        import traceback
        folder = ROOT / "github_harness_lab" / ".runtime"
        folder.mkdir(parents=True, exist_ok=True)
        log = folder / "settings-window-error.log"
        log.write_text(traceback.format_exc(), encoding="utf-8")
        try:
            import tkinter as tk
            from tkinter import messagebox
            window = tk.Tk()
            window.withdraw()
            messagebox.showerror("GitHub 仓库设置未能启动", str(error) + "\n\n详细信息：" + str(log))
            window.destroy()
        except Exception:
            pass
        raise SystemExit(1)
