# -*- coding: utf-8 -*-
"""
视频按剧本顺序重命名工具（Web 界面版）
启动: python app.py → 自动打开浏览器 http://127.0.0.1:17888
"""
import json
import os
import re
import sys
import threading
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, render_template, request

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

app = Flask(__name__)

PORT = 17888
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".ts", ".m4v"}

# ---------------- 文本工具 ----------------

PUNCT_RE = re.compile(
    r"[\s，。！？、；：…—·~～,!?;:\.\-\'\"“”「」『』（）()\[\]【】<>|/*\\\n\r\t]"
)

try:
    from zhconv import convert as _zh_convert
except ImportError:
    def _zh_convert(t, _):
        return t


def clean_text(text: str) -> str:
    """繁转简 + 去标点，用于匹配和文件名"""
    return PUNCT_RE.sub("", _zh_convert(text, "zh-cn"))


# ---------------- 剧本解析 ----------------

# 引号台词（兼容中英文引号、直角引号、直引号）
QUOTE_RE = re.compile(r"[“「『]([^”」』]{2,})[”」』]|\"([^\"]{2,})\"|‘([^’]{2,})’")
# "说"字后面的引号台词（剧本格式：说（情绪：…）"台词"）
SAY_QUOTE_RE = re.compile(
    r"说\s*(?:[（(][^）)]*[)）])?\s*[“「『]([^”」』]{2,})[”」』]|"
    r"说\s*(?:[（(][^）)]*[)）])?\s*\"([^\"]{2,})\""
)


def _is_noise_line(line: str) -> bool:
    """元信息行：引用块、列表项、表格、加粗标记——不从中提取台词"""
    s = line.lstrip()
    return bool(s) and (s[0] in ">-*|#" or s.startswith("**"))


def extract_dialogs(lines):
    """提取台词：优先"说"字后的引号，其次普通引号（人名："台词"）"""
    dialogs = []
    for line in lines:
        if _is_noise_line(line):
            continue
        said = SAY_QUOTE_RE.findall(line)
        if said:
            for m in said:
                text = next(x for x in m if x).strip()
                if len(clean_text(text)) >= 2:
                    dialogs.append(text)
            continue
        for m in QUOTE_RE.findall(line):
            text = next(x for x in m if x).strip()
            if len(clean_text(text)) >= 2:
                dialogs.append(text)
    return dialogs


def detect_prefixes(script_path: str):
    """扫描剧本标题，检测镜头标识前缀（如 P / 剧情 / 小节），返回 [{prefix, count}] 按数量降序"""
    try:
        lines = Path(script_path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    counter = {}
    for line in lines:
        m = re.match(r"^\s*#{1,6}\s*([A-Za-z\u4e00-\u9fa5]{1,4})\s*(\d+)\s*[｜|:：\s·（(]", line)
        if m:
            counter[m.group(1)] = counter.get(m.group(1), 0) + 1
    return sorted(({"prefix": k, "count": v} for k, v in counter.items()), key=lambda x: -x["count"])


def parse_script(script_path: str, prefix: str):
    """
    按镜头前缀解析剧本 -> 镜头列表
    每个镜头: {"num": 序号, "label": "P01", "title": 标题, "title_short": 短标题, "text": 清洗台词全文}
    """
    prefix = (prefix or "").strip()
    if not prefix:
        return [], "未指定镜头标识前缀"
    header_re = re.compile(
        rf"^\s*#{{1,6}}\s*{re.escape(prefix)}\s*(\d+)\s*(.*)$", re.IGNORECASE
    )
    raw = Path(script_path).read_text(encoding="utf-8", errors="ignore")
    lines = raw.splitlines()

    sections = []
    current_num, current_title, current_lines = None, "", []

    def flush():
        nonlocal current_num, current_title, current_lines
        if current_num is not None:
            dialogs = extract_dialogs(current_lines)
            seen, uniq = set(), []
            for d in dialogs:
                key = clean_text(d)
                if key and key not in seen:
                    seen.add(key)
                    uniq.append(d)
            short = re.sub(r"（约?\d+[^）]*）", "", current_title).strip("｜|:： ")
            sections.append({
                "num": current_num,
                "label": f"{prefix}{current_num:02d}",
                "title": current_title,
                "title_short": f"{prefix}{current_num:02d} {short}"[:30],
                "dialogs": uniq,
                "text": "".join(clean_text(t) for t in uniq),
            })
        current_num, current_title, current_lines = None, "", []

    for line in lines:
        m = header_re.match(line)
        if m:
            flush()
            current_num = int(m.group(1))
            current_title = line.strip().lstrip("#").strip()
        elif current_num is not None:
            current_lines.append(line)
    flush()
    return sections, None


# ---------------- 匹配 ----------------

def section_score(video_text: str, sec_text: str):
    """
    分数 = partial_ratio × 长度惩罚
    partial_ratio 容忍 ASR 错字；长度惩罚防止"小节全文是识别文本子串"的错配
    """
    if not video_text or not sec_text:
        return 0.0, 0.0
    p = _fuzz().partial_ratio(video_text, sec_text)
    len_penalty = min(1.0, len(sec_text) / len(video_text) * 1.5)
    return p * len_penalty, _fuzz().ratio(video_text, sec_text)


_FUZZ = None


def _fuzz():
    global _FUZZ
    if _FUZZ is None:
        from rapidfuzz import fuzz
        _FUZZ = fuzz
    return _FUZZ


def match_sections(video_text: str, sections, topn=2):
    scored = []
    for i, sec in enumerate(sections):
        if sec["text"]:
            score, ratio = section_score(video_text, sec["text"])
            scored.append((i, score, ratio))
    scored.sort(key=lambda x: (-x[1], -x[2]))
    return scored[:topn]


# ---------------- 语音识别 ----------------

_MODEL = None
_MODEL_NAME = "small"


def get_model():
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        _MODEL = WhisperModel(_MODEL_NAME, device="cpu", compute_type="int8")
    return _MODEL


def transcribe_video(video_path: Path):
    """识别视频台词，返回 (清洗拼接文本, 首句原文)"""
    model = get_model()
    segments, _ = model.transcribe(str(video_path), language="zh", vad_filter=True, beam_size=5)
    texts = [seg.text.strip() for seg in segments if seg.text.strip()]
    joined = clean_text("".join(texts))
    first = texts[0] if texts else ""
    return joined, first


def find_videos(folder: Path):
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in VIDEO_EXTS and p.is_file())


# ---------------- 设置持久化 ----------------

SETTINGS_FILE = Path(__file__).parent / "settings.json"


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


@app.get("/api/settings")
def api_get_settings():
    return jsonify({"ok": True, "settings": load_settings()})


@app.post("/api/settings")
def api_save_settings():
    data = (request.json or {}).get("settings", {})
    saved = load_settings()
    saved.update({k: v for k, v in data.items() if v is not None})
    try:
        SETTINGS_FILE.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        return jsonify({"ok": True})
    except OSError as e:
        return jsonify({"ok": False, "msg": str(e)})


# ---------------- 全局状态 ----------------

STATE = {
    "status": "idle",  # idle / running / done / error
    "stage": "",       # 当前阶段说明
    "progress": {"current": 0, "total": 0, "file": ""},
    "error": "",
    "script_path": "",
    "video_dir": "",
    "sections": [],
    "results": [],
    "rename_log": {},
}


# ---------------- 页面 ----------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------- API ----------------

@app.post("/api/detect_prefix")
def api_detect_prefix():
    script = (request.json or {}).get("script", "").strip('" ')
    if not script or not Path(script).is_file():
        return jsonify({"ok": False, "msg": "剧本文件不存在"})
    return jsonify({"ok": True, "suggestions": detect_prefixes(script)})


@app.post("/api/analyze")
def api_analyze():
    data = request.json or {}
    script = (data.get("script") or "").strip('" ')
    videos = (data.get("videos") or "").strip('" ')
    prefix = (data.get("prefix") or "").strip()

    if not script or not Path(script).is_file():
        return jsonify({"ok": False, "msg": f"剧本文件不存在: {script}"})
    if not videos or not Path(videos).is_dir():
        return jsonify({"ok": False, "msg": f"视频文件夹不存在: {videos}"})
    if not prefix:
        return jsonify({"ok": False, "msg": "请填写镜头标识前缀（如 P 或 剧情）"})
    if STATE["status"] == "running":
        return jsonify({"ok": False, "msg": "正在识别中，请稍候"})

    STATE.update(status="running", error="", script_path=script, video_dir=videos,
                 sections=[], results=[], stage="解析剧本...",
                 progress={"current": 0, "total": 0, "file": ""})
    threading.Thread(target=_analyze_worker, args=(script, videos, prefix), daemon=True).start()
    return jsonify({"ok": True})


def _analyze_worker(script, videos, prefix):
    try:
        sections, err = parse_script(script, prefix)
        if err or not sections:
            STATE.update(status="error", error=err or "剧本中未找到镜头，请检查镜头标识前缀")
            return
        STATE["sections"] = sections

        video_files = find_videos(Path(videos))
        if not video_files:
            STATE.update(status="error", error="视频文件夹中没有视频文件")
            return

        STATE["stage"] = "加载语音模型（首次运行需下载，请耐心等待）..."
        get_model()

        total = len(video_files)
        STATE["progress"] = {"current": 0, "total": total, "file": ""}
        results = []
        for i, vp in enumerate(video_files, 1):
            STATE.update(stage="语音识别中", progress={"current": i, "total": total, "file": vp.name})
            try:
                text, first = transcribe_video(vp)
            except Exception as e:
                text, first = "", f"(识别失败: {e})"
            if not text:
                results.append({"file": vp.name, "path": str(vp), "recognized": first,
                                "clean": "", "best": -1, "score": 0, "status": "nospeech"})
                continue
            top = match_sections(text, sections, topn=2)
            best, score = (top[0][0], top[0][1]) if top else (-1, 0)
            second = top[1][1] if len(top) > 1 else 0
            # ok: 达到阈值 或 略低但大幅领先次选
            status = "ok" if (score >= 60 or (score >= 45 and score - second >= 15)) else "low"
            if best == -1:
                status = "low"
            results.append({"file": vp.name, "path": str(vp), "recognized": first,
                            "clean": text, "best": best, "score": round(score), "status": status})
        STATE["results"] = results
        STATE.update(status="done", stage="识别完成")
    except Exception as e:
        STATE.update(status="error", error=str(e))


@app.get("/api/status")
def api_status():
    return jsonify({k: STATE[k] for k in ("status", "stage", "progress", "error")})


@app.get("/api/results")
def api_results():
    sections = [{k: sec[k] for k in ("num", "label", "title_short", "text")}
                for sec in STATE["sections"]]
    return jsonify({"ok": STATE["status"] == "done", "sections": sections,
                    "results": STATE["results"], "video_dir": STATE["video_dir"]})


@app.post("/api/open")
def api_open():
    path = (request.json or {}).get("path", "")
    if path and Path(path).is_file():
        try:
            os.startfile(path)  # Windows 系统默认播放器
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "msg": str(e)})
    return jsonify({"ok": False, "msg": "文件不存在"})


@app.post("/api/rename")
def api_rename():
    if STATE["status"] != "done":
        return jsonify({"ok": False, "msg": "尚未完成识别"})
    items = (request.json or {}).get("items", [])
    video_dir = Path(STATE["video_dir"])
    done, skipped, log = [], [], {}
    for it in items:
        old_name, new_name = (it.get("old") or "").strip(), (it.get("new") or "").strip()
        old = video_dir / old_name
        if not old.is_file():
            skipped.append({"file": old_name, "reason": "原文件不存在"})
            continue
        if not new_name:
            skipped.append({"file": old_name, "reason": "未填写新文件名，保持原名"})
            continue
        if new_name == old_name:
            continue
        target = video_dir / new_name
        if target.exists():
            skipped.append({"file": old_name, "reason": f"目标文件名已存在: {new_name}"})
            continue
        try:
            old.rename(target)
            done.append({"old": old_name, "new": new_name})
        except Exception as e:
            skipped.append({"file": old_name, "reason": str(e)})
    # 落盘校验：确认改名真实生效（防止被安全软件/同步盘/沙箱回滚导致"假成功"）
    verified = []
    for x in done:
        if (video_dir / x["new"]).is_file() and not (video_dir / x["old"]).exists():
            verified.append(x)
            log[x["new"]] = x["old"]
        else:
            skipped.append({"file": x["old"], "reason": f"改名未生效（被系统回滚或拦截）: {x['new']}"})
    STATE["rename_log"] = log
    if log:
        try:
            (video_dir / "rename_log.json").write_text(
                json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    ok = bool(verified) or not done
    return jsonify({"ok": ok, "done": verified, "skipped": skipped})


@app.post("/api/rollback")
def api_rollback():
    video_dir = Path(STATE["video_dir"])
    log_path = video_dir / "rename_log.json"
    log = STATE["rename_log"]
    if not log and log_path.is_file():
        log = json.loads(log_path.read_text(encoding="utf-8"))
    if not log:
        return jsonify({"ok": False, "msg": "没有可撤销的改名记录"})
    restored, failed = [], []
    for new, old in log.items():
        src, dst = video_dir / new, video_dir / old
        if src.is_file() and not dst.exists():
            src.rename(dst)
            restored.append({"old": old, "new": new})
        else:
            failed.append(new)
    STATE["rename_log"] = {}
    if log_path.is_file():
        log_path.unlink()
    return jsonify({"ok": True, "restored": restored, "failed": failed})


@app.get("/api/browse")
def api_browse():
    """服务端目录浏览（供前端路径选择弹窗）。支持粘贴路径跳转。"""
    p = (request.args.get("path") or "").strip().strip('"')
    mode = request.args.get("mode", "dir")  # dir=选文件夹 file=选剧本文件
    if not p:
        import string
        drives = [f"{c}:\\" for c in string.ascii_uppercase if Path(f"{c}:\\").exists()]
        return jsonify({"ok": True, "current": "此电脑", "parent": "", "dirs": drives, "files": [], "target_file": ""})
    # "F:" -> "F:\"
    if len(p) == 2 and p[1] == ":":
        p += "\\"
    path = Path(p)
    if not path.exists():
        return jsonify({"ok": False, "msg": f"路径不存在: {p}"})
    # 粘贴的是文件 -> 跳到所在目录并标记该文件（file 模式下选中它）
    target_file = ""
    if path.is_file():
        target_file = path.name
        path = path.parent
    dirs, files = [], []
    try:
        for it in sorted(path.iterdir(), key=lambda x: x.name.lower()):
            if it.is_dir() and not it.name.startswith((".", "$")):
                dirs.append(it.name)
            elif mode == "file" and it.suffix.lower() in {".md", ".txt"}:
                files.append(it.name)
    except (PermissionError, OSError):
        pass
    parent = str(path.parent) if str(path.parent) != str(path) else ""
    return jsonify({"ok": True, "current": str(path), "parent": parent,
                    "dirs": dirs, "files": files, "target_file": target_file})


if __name__ == "__main__":
    print(f"视频按剧本顺序重命名工具已启动: http://127.0.0.1:{PORT}")
    threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
