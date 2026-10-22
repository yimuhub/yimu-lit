"""novel-bot 跨平台启动器（v0.0.3）。

任意 Win/Mac/Linux + 有 Python(>=3.9) 的电脑：
    python run.py            # 自动建 venv、装依赖、起服务
    python run.py -p 5000    # 指定端口
    python run.py --no-venv  # 用当前解释器直接跑（依赖需已装）
"""
import sys, os, subprocess, importlib, shutil, socket, argparse, venv

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
    try:
        s.bind(("127.0.0.1", port)); s.close(); return True
    except OSError:
        return False

def _ensure_venv():
    """缺 .venv 就建 + 装依赖；装好返回 True。"""
    if os.path.exists(os.path.join(VENV, "pyvenv.cfg")) and importlib.util.find_spec("fastapi") \
       and importlib.util.find_spec("httpx"):
        return True
    _log("首次运行：创建虚拟环境 .venv 并安装依赖（fastapi/uvicorn/httpx）…")
    venv.create(VENV, with_pip=True)
    if os.name == "nt":
        pip = os.path.join(VENV, "Scripts", "python.exe")
    else:
        pip = os.path.join(VENV, "bin", "python")
    subprocess.run([pip, "-m", "pip", "install", "-r", REQ], check=True)
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
    if os.name == "nt":
        return os.path.join(VENV, "Scripts", "python.exe")
    return os.path.join(VENV, "bin", "python")

def main():
    ap = argparse.ArgumentParser(description="启动 novel-bot")
    ap.add_argument("-p", "--port", type=int, default=None, help="端口（默认取 config/46821）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-venv", action="store_true", help="不建 venv，用当前解释器")
    ap.add_argument("--reload", action="store_true", help="热更新：改代码/前端后自动重启（开发用）")
    a = ap.parse_args()
    port = a.port or _port_configured()
    if a.port and a.port:
        os.environ["NOVEL_BOT_PORT"] = str(a.port)

    _ensure_config()
    py = _pick_python(a.no_venv)
    # 直接以子进程跑 uvicorn（用选定的解释器）；--reload 时 uvicorn 监听 app.py/static 变更自动重启
    cmd = [py, "-m", "uvicorn", "app:app", "--host", a.host, "--port", str(port), "--app-dir", ROOT]
    if a.reload:
        cmd += ["--reload", "--reload-dir", ROOT, "--reload-include", "*.py", "--reload-include", "static/*"]
    if not _port_free(port):
        _log(f"⚠️ 端口 {port} 已被占用。可用 `-p 其它端口` 或 `NOVEL_BOT_PORT` 覆盖。")
    _log(f"🚀 启动 http://{a.host}:{port}  （python={os.path.basename(py)}）")
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        _log("已停止。")

if __name__ == "__main__":
    main()
