# -*- coding: utf-8 -*-
"""
视频按剧本顺序重命名工具（Web 界面版）
支持多部剧本 + 多个视频文件夹；改名后视频移入「视频所在文件夹/剧本名」子文件夹
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
app.config["TEMPLATES_AUTO_RELOAD"] = True  # 模板改动后刷新浏览器即可生效，无需重启程序

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

# 镜头序号：纯数字（01）或复合（29-1 / 29.2 / 29—3）
_NUM = r"\d+(?:[-–—.]\d+)*"
# 标题候选行：# 号可有可无；前缀(1-4字) + 序号 + 分隔符（｜ : ： 空格 · （ ( 或行尾）
# 序号后必须跟分隔符，避免把"第29段""第16章"这类正文误判为标题
_HEADER_CAND_RE = re.compile(
    rf"^\s*#{{0,6}}\s*([A-Za-z\u4e00-\u9fa5]{{1,4}})\s*({_NUM})(?=$|[\s｜|:：·（(])"
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
    """
    扫描剧本标题行（Markdown # 标题或裸文本行均可），检测镜头标识前缀。
    序号会重复出现的（如每场内部"镜头1、镜头2"逐场重新编号）判定为子级单位，排在后面；
    序号全唯一的前缀（如 剧情29-1…40-3、P01…P11）优先推荐。
    """
    try:
        lines = Path(script_path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    cand = {}
    for line in lines:
        m = _HEADER_CAND_RE.match(line)
        if m:
            cand.setdefault(m.group(1), []).append(m.group(2))
    ranked = []
    for pre, nums in cand.items():
        if len(nums) < 2:
            continue
        ranked.append({"prefix": pre, "count": len(set(nums)),
                       "unique": len(set(nums)) == len(nums)})
    ranked.sort(key=lambda x: (-x["unique"], -x["count"]))
    # unique: 序号全局唯一（主级镜头单位）；False = 序号重复（如逐场重新编号的"镜头1"，子级单位）
    return [{"prefix": x["prefix"], "count": x["count"], "unique": x["unique"]} for x in ranked]


def _canon_num(num_str: str) -> str:
    """纯整数补零两位（'1'->'01'）；复合序号原样（'29-1'）"""
    return num_str.zfill(2) if num_str.isdigit() else num_str


def parse_script(script_path: str, prefix: str):
    """
    按镜头前缀解析剧本 -> 镜头列表
    标题行支持 Markdown # 与裸文本；序号支持复合形式（如 剧情29-1）
    前缀支持多个（逗号/空格/顿号/斜杠分隔，如 "P,剧情"）——
    适用于一个剧本文件里混用多种镜头标题格式的情形（如第11集用 P01、第12集用 剧情12-1）
    每个镜头: {"num": 序号串, "label": "P01/剧情29-1", "title": 标题, "title_short": 短标题, "text": 清洗台词全文}
    """
    prefixes = [p for p in re.split(r"[,，、/|;；\s]+", (prefix or "").strip()) if p]
    if not prefixes:
        return [], "未指定镜头标识前缀"
    # 多前缀按长度降序排列，避免短前缀抢占长前缀（如"剧情"与"剧"）
    alt = "|".join(re.escape(p) for p in sorted(prefixes, key=len, reverse=True))
    header_re = re.compile(
        rf"^\s*#{{0,6}}\s*({alt})\s*({_NUM})\s*(.*)$", re.IGNORECASE
    )
    try:
        raw = Path(script_path).read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        return [], f"剧本文件读取失败: {e}"
    lines = raw.splitlines()

    sections = []
    cur_prefix, current_num, current_title, current_lines = "", None, "", []

    def flush():
        nonlocal cur_prefix, current_num, current_title, current_lines
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
                "label": f"{cur_prefix}{current_num}",
                "title": current_title,
                "title_short": f"{cur_prefix}{current_num} {short}"[:30],
                "dialogs": uniq,
                "text": "".join(clean_text(t) for t in uniq),
            })
        cur_prefix, current_num, current_title, current_lines = "", None, "", []

    for line in lines:
        m = header_re.match(line)
        if m:
            flush()
            cur_prefix = m.group(1)          # 实际匹配到的前缀（支持多前缀混用）
            current_num = _canon_num(m.group(2))
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
    "scripts": [],     # [{path, name, stem, prefix, sections}]
    "video_dirs": [],
    "results": [],     # 每个视频: {file, path, dir, recognized, clean, best(全局镜头索引), score, status}
    "rename_log": {},  # {新绝对路径: 旧绝对路径}
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
    scripts_in = data.get("scripts") or []    # [{path, prefix}]
    videos_in = data.get("videos") or []      # [文件夹路径]

    if not scripts_in:
        return jsonify({"ok": False, "msg": "请至少选择一部剧本"})
    if not videos_in:
        return jsonify({"ok": False, "msg": "请至少选择一个视频文件夹"})
    if STATE["status"] == "running":
        return jsonify({"ok": False, "msg": "正在识别中，请稍候"})

    seen, scripts = set(), []
    for s in scripts_in:
        path = (s.get("path") or "").strip('" ')
        prefix = (s.get("prefix") or "").strip()
        if not path:
            continue
        if path.lower() in seen:
            continue
        seen.add(path.lower())
        if not Path(path).is_file():
            return jsonify({"ok": False, "msg": f"剧本文件不存在: {path}"})
        if not prefix:
            return jsonify({"ok": False, "msg": f"剧本「{Path(path).name}」未填写镜头标识前缀"})
        scripts.append({"path": path, "prefix": prefix})

    seen, video_dirs = set(), []
    for v in videos_in:
        v = (v or "").strip('" ')
        if not v or v.lower() in seen:
            continue
        seen.add(v.lower())
        if not Path(v).is_dir():
            return jsonify({"ok": False, "msg": f"视频文件夹不存在: {v}"})
        video_dirs.append(v)

    if not scripts or not video_dirs:
        return jsonify({"ok": False, "msg": "请填写有效的剧本和视频文件夹"})

    STATE.update(status="running", error="", scripts=[], video_dirs=video_dirs,
                 results=[], rename_log={}, stage="解析剧本...",
                 progress={"current": 0, "total": 0, "file": ""})
    threading.Thread(target=_analyze_worker, args=(scripts, video_dirs), daemon=True).start()
    return jsonify({"ok": True})


def _analyze_worker(scripts_in, video_dirs):
    try:
        # 1. 解析每部剧本
        scripts = []
        for s in scripts_in:
            sections, err = parse_script(s["path"], s["prefix"])
            if err or not sections:
                name = Path(s["path"]).name
                STATE.update(status="error",
                             error=f"剧本「{name}」：{err or '未找到任何镜头，请检查镜头标识前缀'}")
                return
            p = Path(s["path"])
            scripts.append({"path": str(p), "name": p.name, "stem": p.stem,
                            "prefix": s["prefix"], "sections": sections})
        STATE["scripts"] = scripts

        # 全局镜头列表（各剧本镜头按顺序拼接，全局索引用于匹配与下拉展示）
        all_sections = []
        for si, sc in enumerate(scripts):
            for sec in sc["sections"]:
                all_sections.append({**sec, "script_idx": si})

        # 2. 收集所有文件夹中的视频（去重）
        video_files, seen = [], set()
        for vd in video_dirs:
            for vp in find_videos(Path(vd)):
                key = str(vp).lower()
                if key not in seen:
                    seen.add(key)
                    video_files.append(vp)
        if not video_files:
            STATE.update(status="error", error="所有文件夹中都没有视频文件")
            return

        STATE["stage"] = "加载语音模型（首次运行需下载，请耐心等待）..."
        get_model()

        # 3. 逐个识别并与全部剧本的所有镜头匹配
        total = len(video_files)
        STATE["progress"] = {"current": 0, "total": total, "file": ""}
        results = []
        for i, vp in enumerate(video_files, 1):
            STATE.update(stage="语音识别中", progress={"current": i, "total": total, "file": vp.name})
            try:
                text, first = transcribe_video(vp)
            except Exception as e:
                text, first = "", f"(识别失败: {e})"
            base = {"file": vp.name, "path": str(vp), "dir": str(vp.parent), "recognized": first}
            if not text:
                results.append({**base, "clean": "", "best": -1, "score": 0, "status": "nospeech"})
                continue
            top = match_sections(text, all_sections, topn=2)
            best, score = (top[0][0], top[0][1]) if top else (-1, 0)
            second = top[1][1] if len(top) > 1 else 0
            # ok: 达到阈值 或 略低但大幅领先次选
            status = "ok" if (score >= 60 or (score >= 45 and score - second >= 15)) else "low"
            if best == -1:
                status = "low"
            results.append({**base, "clean": text, "best": best, "score": round(score), "status": status})
        STATE["results"] = results
        STATE.update(status="done", stage="识别完成")
    except Exception as e:
        STATE.update(status="error", error=str(e))


@app.get("/api/status")
def api_status():
    return jsonify({k: STATE[k] for k in ("status", "stage", "progress", "error")})


@app.get("/api/results")
def api_results():
    scripts = [{"name": sc["name"], "stem": sc["stem"]} for sc in STATE["scripts"]]
    sections = []
    for si, sc in enumerate(STATE["scripts"]):
        for sec in sc["sections"]:
            sections.append({"script_idx": si, "num": sec["num"],
                             "label": sec["label"], "title_short": sec["title_short"]})
    return jsonify({"ok": STATE["status"] == "done", "scripts": scripts, "sections": sections,
                    "results": STATE["results"], "video_dirs": STATE["video_dirs"]})


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
    scripts = STATE["scripts"]
    done, skipped, log = [], [], {}
    per_dir_log = {}  # 源文件夹 -> {剧本名/新文件名: 旧文件名}（落盘用）

    for it in items:
        old_path = (it.get("old") or "").strip()
        new_name = (it.get("new") or "").strip()
        script_idx = it.get("script_idx", -1)
        old = Path(old_path)
        if not old.is_file():
            skipped.append({"file": old_path, "reason": "原文件不存在"})
            continue
        if not new_name:
            skipped.append({"file": old.name, "reason": "未填写新文件名，保持原名"})
            continue
        # 目标文件夹：指定了剧本 -> 视频所在文件夹/剧本名（不带后缀）；未指定 -> 原地改名
        if isinstance(script_idx, int) and 0 <= script_idx < len(scripts):
            target_dir = old.parent / scripts[script_idx]["stem"]
            rel_key = f"{scripts[script_idx]['stem']}/{new_name}"
        elif script_idx == -1:
            target_dir = old.parent
            rel_key = new_name
        else:
            skipped.append({"file": old.name, "reason": "剧本索引无效"})
            continue
        target = target_dir / new_name
        if new_name == old.name and target_dir == old.parent:
            continue  # 无变化
        if target.exists():
            skipped.append({"file": old.name, "reason": f"目标文件已存在: {rel_key}"})
            continue
        try:
            target_dir.mkdir(exist_ok=True)
            old.rename(target)
            done.append({"old": str(old), "new": str(target), "rel": rel_key})
            per_dir_log.setdefault(str(old.parent), {})[rel_key] = old.name
        except Exception as e:
            skipped.append({"file": old.name, "reason": str(e)})

    # 落盘校验：确认改名真实生效（防止被安全软件/同步盘/沙箱回滚导致"假成功"）
    verified = []
    for x in done:
        if Path(x["new"]).is_file() and not Path(x["old"]).exists():
            verified.append(x)
            log[x["new"]] = x["old"]
        else:
            skipped.append({"file": Path(x["old"]).name,
                            "reason": f"改名未生效（被系统回滚或拦截）: {x['rel']}"})
    STATE["rename_log"] = log

    # 每个涉及的源文件夹写一份 rename_log.json（应用重启后仍可撤销）
    for dir_str, entries in per_dir_log.items():
        try:
            lp = Path(dir_str) / "rename_log.json"
            merged = json.loads(lp.read_text(encoding="utf-8")) if lp.is_file() else {}
            merged.update({k: v for k, v in entries.items()
                           if str(Path(dir_str) / k) in log})  # 只记录校验通过的
            lp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass

    ok = bool(verified) or not done
    return jsonify({"ok": ok, "done": verified, "skipped": skipped})


@app.post("/api/rollback")
def api_rollback():
    # 日志来源：内存 + 各视频文件夹下的 rename_log.json（重启后恢复用）
    log = dict(STATE["rename_log"])
    log_files = []
    for vd in STATE.get("video_dirs", []):
        lp = Path(vd) / "rename_log.json"
        if lp.is_file():
            log_files.append(lp)
            try:
                for rel_new, old_name in json.loads(lp.read_text(encoding="utf-8")).items():
                    log.setdefault(str(Path(vd) / rel_new), str(Path(vd) / old_name))
            except (OSError, json.JSONDecodeError):
                pass
    if not log:
        return jsonify({"ok": False, "msg": "没有可撤销的改名记录"})

    restored, failed, folder_names = [], [], set()
    for new_abs, old_abs in log.items():
        src, dst = Path(new_abs), Path(old_abs)
        folder_names.add(src.parent.name)
        if src.is_file() and not dst.exists():
            try:
                src.rename(dst)
                restored.append({"old": old_abs, "new": new_abs})
            except OSError:
                failed.append(new_abs)
        else:
            failed.append(new_abs)

    STATE["rename_log"] = {}
    # 删除日志文件 + 清理空的剧本名子文件夹
    for lp in log_files:
        try:
            lp.unlink()
        except OSError:
            pass
    for vd in STATE.get("video_dirs", []):
        for name in folder_names:
            p = Path(vd) / name
            if p.is_dir():
                try:
                    if not any(p.iterdir()):
                        p.rmdir()
                except OSError:
                    pass
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
