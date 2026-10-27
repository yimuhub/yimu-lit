"""novel-bot 跨平台启动器（v0.0.3）。

任意 Win/Mac/Linux + 有 Python(>=3.9) 的电脑：
    python run.py            # 自动建 venv、装依赖、起服务
    python run.py -p 5000    # 指定端口
    python run.py --no-venv  # 用当前解释器直接跑（依赖需已装）
"""
import sys, os, subprocess, shutil, socket, argparse, venv

ROOT = os.path.dirname(os.path.abspath(__file__))
REQ = os.path.join(ROOT, "requirements.txt")
VENV = os.path.join(ROOT, ".venv")
DEFAULT_PORT = 46821

def _log(m): print(f"[novel-bot] {m}", flush=True)

def _port_configured():
    import json
    env = os.environ.get("NOVEL_BOT_PORT")
    if env and env.strip().isdigit():
        return int(env)
    cf = os.path.join(ROOT, "config.json")
    if os.path.exists(cf):
        try:
            p = json.load(open(cf, encoding="utf-8")).get("port")
            if p: return int(p)
        except Exception: pass
    return DEFAULT_PORT

def _port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port)); s.close(); return True
    except OSError:
        return False

def _kill_port(port):
    """杀掉占用指定端口的旧进程，让热更新时不必手动关闭。"""
    try:
        if os.name == "nt":
            # Windows: netstat 查 PID → taskkill
            r = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True)
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 5 and f":{port}" in parts[1] and parts[3] == "LISTENING":
                    pid = parts[4]
                    subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                    _log(f"  已终止 PID {pid}")
        else:
            # macOS/Linux: lsof 查 PID → kill -9（杀主进程 + 子进程）
            r = subprocess.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True)
            if r.stdout.strip():
                for pid in r.stdout.strip().split("\n"):
                    pid = pid.strip()
                    if pid:
                        subprocess.run(["kill", "-9", pid], capture_output=True)
                        _log(f"  已终止 PID {pid}")
            # uvicorn --reload 会派生子进程（reloader → worker），端口子进程可能不在 lsof 结果中
            # 用 pgrep 补杀残留的 uvicorn 子进程
            try:
                pg = subprocess.run(["pgrep", "-f", "uvicorn.*app:app"], capture_output=True, text=True)
                for pid in pg.stdout.strip().split("\n"):
                    pid = pid.strip()
                    if pid:
                        subprocess.run(["kill", "-9", pid], capture_output=True)
                        _log(f"  已终止 uvicorn 子进程 PID {pid}")
            except Exception:
                pass
    except Exception as e:
        _log(f"  自动关闭失败: {e}（请手动关闭占用端口 {port} 的进程）")

def _venv_python():
    return os.path.join(VENV, "Scripts", "python.exe") if os.name == "nt" else os.path.join(VENV, "bin", "python")

def _deps_in(py):
    """用指定解释器探测依赖，避免外层解释器误判后重建 .venv。"""
    return subprocess.run([py, "-c", "import fastapi, httpx"], capture_output=True).returncode == 0

def _ensure_venv():
    """缺 .venv 就建 + 装依赖；装好返回 True。"""
    py = _venv_python()
    if os.path.exists(os.path.join(VENV, "pyvenv.cfg")) and os.path.exists(py) and _deps_in(py):
        return True
    _log("首次运行：创建虚拟环境 .venv 并安装依赖（fastapi/uvicorn/httpx）…")
    venv.create(VENV, with_pip=True)
    subprocess.run([py, "-m", "pip", "install", "-r", REQ], check=True)
    _log("依赖安装完成。")
    return True

def _ensure_config():
    """git 式行为：config.json 未入库，首次运行从模板生成（相对路径、无密钥）。"""
    cf = os.path.join(ROOT, "config.json")
    ex = os.path.join(ROOT, "config.example.json")
    if not os.path.exists(cf) and os.path.exists(ex):
        shutil.copyfile(ex, cf)
        _log("首次运行：已生成 config.json（自包含 skill/、无密钥，请在网页或环境变量里配置 API key）。")
    os.makedirs(os.path.join(ROOT, "secrets"), exist_ok=True)

def _pick_python(no_venv=False):
    if no_venv:
        return sys.executable
    _ensure_venv()
    return _venv_python()

def main():
    ap = argparse.ArgumentParser(description="启动 novel-bot")
    ap.add_argument("-p", "--port", type=int, default=None, help="端口（默认取 config/46821）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-venv", action="store_true", help="不建 venv，用当前解释器")
    ap.add_argument("--reload", action="store_true", default=True, help="热更新：改代码/前端后自动重启（默认开启）")
    ap.add_argument("--no-reload", action="store_true", help="关闭热更新（生产模式）")
    a = ap.parse_args()
    if a.no_reload:
        a.reload = False
    port = a.port or _port_configured()
    if a.port and a.port:
        os.environ["NOVEL_BOT_PORT"] = str(a.port)

    _ensure_config()
    py = _pick_python(a.no_venv)
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        _log(f"⚠️ 正在监听 {a.host}（非回环地址）。默认 Host 白名单只放行本机，"
             f"如需局域网访问请显式设置 SHENBI_TRUSTED_HOSTS=<你的域名或IP>；"
             f"本应用只做单机本地鉴权，不建议直接暴露公网。")
    # 直接以子进程跑 uvicorn（用选定的解释器）；--reload 时 uvicorn 监听 app.py/static 变更自动重启
    cmd = [py, "-m", "uvicorn", "app:app", "--host", a.host, "--port", str(port), "--app-dir", ROOT]
    if a.reload:
        cmd += ["--reload", "--reload-dir", ROOT, "--reload-include", "*.py", "--reload-include", "static/*", "--reload-delay", "2"]
    if not _port_free(port):
        _log(f"⚠️ 端口 {port} 已被占用，尝试自动关闭旧进程…")
        import time
        for attempt in range(3):
            _kill_port(port)
            time.sleep(2 if attempt == 0 else 1.5)
            if _port_free(port):
                break
            _log(f"  第 {attempt+1} 次尝试后端口仍被占用{'，继续重试…' if attempt < 2 else '。'}")
        if not _port_free(port):
            _log(f"❌ 端口 {port} 仍被占用。可用 `-p 其它端口` 或 `NOVEL_BOT_PORT` 覆盖。")
            sys.exit(1)
        _log(f"✅ 旧进程已关闭，继续启动。")
    _log(f"🚀 启动 http://{a.host}:{port}  （python={os.path.basename(py)}）")
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        _log("已停止。")

if __name__ == "__main__":
    main()
