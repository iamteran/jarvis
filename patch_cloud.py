from pathlib import Path

p = Path('jarvis/tools.py')
s = p.read_text(encoding='utf-8')

old = '''    def _focus_app(self, name: str) -> str:
        from .skills import screen
        return screen.activate(name)




def _short(arguments: dict) -> str:'''

patch = '''    def _focus_app(self, name: str) -> str:
        from .skills import screen
        return screen.activate(name)

    def _cloud(self, action: str, cmd: str = "", port: int = 0, name: str = "", cwd: str = "") -> str:
        import subprocess, shutil

        box = (name or "jarvis").strip()
        act = (action or "").strip().lower()
        exe = shutil.which("instabox") or r"C:\\Users\\Brian\\AppData\\Roaming\\npm\\instabox.cmd"
        try:
            if act == "status":
                proc = subprocess.run([exe, "ls"], capture_output=True, text=True, timeout=60)
                out = (proc.stdout or "") + (proc.stderr or "")
                if box in out and "Running" in out:
                    return f"Box {box} is running."
                return f"Box status:\\n{out.strip()[:600]}"
            if act == "start":
                proc = subprocess.run([exe, "start", box], capture_output=True, text=True, timeout=300)
                out = (proc.stdout or "") + (proc.stderr or "")
                return out.strip()[:600] or "Done."
            if act == "stop":
                proc = subprocess.run([exe, "stop", box], capture_output=True, text=True, timeout=300)
                out = (proc.stdout or "") + (proc.stderr or "")
                return out.strip()[:600] or "Done."
            if act == "files":
                path = (cwd or cmd or "/data/home").strip()
                proc = subprocess.run([exe, "files", box, path], capture_output=True, text=True, timeout=120)
                out = (proc.stdout or "") + (proc.stderr or "")
                return out.strip()[:900]
            if act == "publish":
                if not port:
                    return "Port number required, sir."
                proc = subprocess.run([exe, "port", "publish", box, str(int(port))], capture_output=True, text=True, timeout=120)
                out = (proc.stdout or "") + (proc.stderr or "")
                return out.strip()[:600]
            if act == "exec":
                if not cmd:
                    return "Command required, sir."
                args = [exe, "exec", box]
                if cwd:
                    args += ["--cwd", cwd]
                args += ["--", cmd]
                proc = subprocess.run(args, capture_output=True, text=True, timeout=610)
                out = (proc.stdout or "").strip()
                err = (proc.stderr or "").strip()
                text = out or err or ""
                if proc.returncode == 0:
                    return f"Done. Output:\\n{text[:900]}" if text else "Done, sir. Command completed successfully."
                msg = text or f"exit code {proc.returncode}"
                return f"Command failed ({proc.returncode}): {msg[:700]}"
            if act == "ssh_cmd":
                if not cmd:
                    return "Command required, sir."
                full = f"cd {cwd} && {cmd}" if cwd else cmd
                proc = subprocess.run([exe, "ssh", box, "--", "bash", "-lc", full], capture_output=True, text=True, timeout=3600)
                out = (proc.stdout or "").strip()
                err = (proc.stderr or "").strip()
                text = out or err or ""
                if proc.returncode == 0:
                    return f"Done. Output:\\n{text[:900]}" if text else "Done, sir. Command completed successfully."
                msg = text or f"exit code {proc.returncode}"
                return f"Command failed ({proc.returncode}): {msg[:700]}"
            return "Unknown action. Use exec, status, files, publish, ssh_cmd, start, or stop."
        except subprocess.TimeoutExpired:
            return "That box command timed out, sir. For long jobs use ssh_cmd or run in background."
        except Exception as exc:  # noqa: BLE001
            return f"I couldn't reach the box: {exc}"


def _short(arguments: dict) -> str:'''

if old not in s:
    print('not found')
    raise SystemExit(1)
p.write_text(s.replace(old, patch), encoding='utf-8')
print('ok')
