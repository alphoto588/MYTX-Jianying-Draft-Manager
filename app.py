#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
#  剪映草稿历史版本管理器 · Jianying Draft History Manager
# ------------------------------------------------------------
#  公众号：爱折腾的摄影大叔（lovephoto588）
#
#  Copyright (c) 2026 爱折腾的摄影大叔（lovephoto588）
#  本项目基于 MIT 协议开源，详见仓库根目录的 LICENSE 文件。
# ============================================================
"""
剪映草稿历史版本管理器 (Jianying Draft History Manager)
========================================================

一个零依赖的本地小工具（仅需 Python 3.8+ 标准库）：

  1. 扫描剪映草稿根目录下的所有草稿项目；
  2. 解析每个草稿 .backup/ 目录中的历史版本（save / close / load / 恢复快照）；
  3. 在网页前端列出全部历史版本（时间、类型、大小、与当前版差异）；
  4. 用户点击「恢复此版本」后，工具会：
       - 检测剪映是否正在运行（运行中则拒绝操作）；
       - 自动把当前 draft_content.json 备份到 .backup/（命名带时间戳，可随时回退）；
       - 用所选历史版本覆盖 draft_content.json。

用法：
  python app.py                # 默认端口 8620
  python app.py --port 9000    # 指定端口
  启动后用浏览器打开 http://127.0.0.1:8620

草稿加密说明：剪映专业版草稿内容为加密存储，本工具只做文件级版本管理，
不涉及解密；解密由剪映读取 draft_content.json 时使用 crypto_key_store.dat 自动完成。

版权与许可：
  Copyright (c) 2026 爱折腾的摄影大叔（lovephoto588）
  本项目基于 MIT 协议开源。MIT 协议允许自由使用、修改、再分发与商用，
  但需保留上述版权声明与许可声明。
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

# ---------------------------------------------------------------- 常量

TOOL_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT = TOOL_DIR.parent          # 默认为剪映草稿根目录（本工具的同级目录）
DRAFT_CONTENT = "draft_content.json"
BACKUP_DIR_NAME = ".backup"
RECYCLE_BIN = ".recycle_bin"            # 剪映回收站（误删草稿暂存处）

RE_JY_BAK = re.compile(r"^(\d{14})_([0-9a-f]+)\.(save|close|load)\.bak$")   # 剪映自动生成的备份
RE_RESTORE_BAK = re.compile(r"^(\d{14})_restore_.*\.bak$")                # 本工具产生的恢复快照

# 各版本类型 -> (显示名, 文字色, 背景色)
KIND_LABEL = {
    "save":    ("自动保存", "#2563eb", "#eff6ff"),
    "close":   ("关闭备份", "#d97706", "#fffbeb"),
    "load":    ("打开备份", "#64748b", "#f1f5f9"),
    "restore": ("恢复快照", "#7c3aed", "#f5f3ff"),
    "prev":    ("上一版本", "#059669", "#ecfdf5"),
    "current": ("当前版本", "#059669", "#ecfdf5"),
}

RESTORE_LOCK = threading.Lock()

# ---------------------------------------------------------------- 工具函数


def find_default_root() -> Path:
    """推断剪映草稿根目录：优先取环境变量，其次取工具所在目录的上级。"""
    env = os.environ.get("JIANYING_DRAFTS_ROOT")
    if env and Path(env).is_dir():
        return Path(env)
    if DEFAULT_ROOT.is_dir():
        return DEFAULT_ROOT
    return TOOL_DIR


def md5_of(path: Path, chunk: int = 1 << 20) -> str:
    """计算文件的 MD5（分块读取，避免大文件一次性载入内存）。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def fmt_size(n: int) -> str:
    """把字节数格式化为人类易读的 B / KB / MB 文本。"""
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024)
    return "%.2f MB" % (n / 1024 / 1024)


def fmt_time(ts: float) -> str:
    """时间戳 -> 本地时间字符串（YYYY-MM-DD HH:MM:SS）。"""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def is_jianying_running() -> bool:
    """检测剪映专业版进程是否在运行（仅 Windows 有意义）。"""
    if os.environ.get("JY_TOOL_SKIP_PROCESS_CHECK") == "1":
        return False  # 仅供自动化测试使用
    if not sys.platform.startswith("win"):
        return False
    for image in ("JianyingPro.exe", "Jianying.exe", "剪映.exe"):
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq %s" % image, "/NH"],
                capture_output=True, text=True, timeout=6,
            )
            if out.returncode == 0 and image.lower() in (out.stdout or "").lower():
                return True
        except Exception:
            pass
    return False


def is_draft_dir(p: Path) -> bool:
    """判断目录是否为剪映草稿项目（含 draft_content.json 即可）。"""
    return p.is_dir() and (p / DRAFT_CONTENT).is_file()


SKIP_DIR_NAMES = {
    "windows", "program files", "program files (x86)", "program files (x86)",
    "$recycle.bin", "system volume information", "$windows.~bt", "$windows.~ws",
    "msocache", "perflogs", "common files", "windowsapps", "windows.old",
    "appdata\\local\\temp", "node_modules", ".git",
}


def _drives() -> list:
    """列出本机所有存在的盘符根目录（如 D:\\）。"""
    out = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        p = Path(letter + ":/")
        if p.is_dir():
            out.append(p)
    return out


def _draft_root_count(p: Path) -> int:
    """统计某目录下一级子目录中草稿项目的数量（用于判断它是否是草稿根目录）。"""
    n = 0
    try:
        for child in p.iterdir():
            if child.is_dir() and (child / DRAFT_CONTENT).is_file():
                n += 1
    except OSError:
        pass
    return n


def _is_draft_root(p: Path) -> bool:
    """判定目录是否为「草稿根目录」：本身不是草稿，但下面有草稿。"""
    return p.is_dir() and (p / DRAFT_CONTENT).is_file() is False and _draft_root_count(p) > 0


def discover_roots(deep: bool = False, timeout: float = 60.0) -> dict:
    """自动发现剪映草稿根目录。

    quick: 检查常见位置（工具同级、用户目录、各盘符根的 JianyingPro Drafts 等）
    deep : 在上述基础上遍历盘符（限深 5 层、跳过系统目录），兜底非常规位置
    """
    found = {}

    def add(p: Path, source: str):
        try:
            rp = p.resolve()
        except OSError:
            return
        key = str(rp)
        if key in found or not _is_draft_root(rp):
            return
        found[key] = {"path": key, "count": _draft_root_count(rp), "source": source}

    # ---- quick: 常见候选 ----
    home = Path.home()
    quick = [TOOL_DIR.parent]
    quick += [home / r for r in [
        "JianyingPro Drafts",
        "Documents/JianyingPro Drafts",
        "Desktop/JianyingPro Drafts",
        "Videos/JianyingPro Drafts",
        "Documents", "Desktop", "Videos",
        "AppData/Local/JianyingPro Drafts",
        "AppData/Roaming/JianyingPro Drafts",
    ]]
    localapp = os.environ.get("LOCALAPPDATA")
    if localapp:
        quick += [
            Path(localapp) / "JianyingPro Drafts",
            Path(localapp) / "JianyingPro/User Data/Projects",
        ]
    for d in _drives():
        quick.append(d / "JianyingPro Drafts")
    for p in quick:
        add(p, "常见位置")

    if not deep:
        return {"roots": list(found.values()), "deep": False, "timed_out": False}

    # ---- deep: 遍历盘符 ----
    deadline = time.time() + max(3.0, timeout)
    timed_out = False
    for drive in _drives():
        for root, dirs, _files in os.walk(drive):
            if time.time() > deadline:
                timed_out = True
                break
            rp = Path(root)
            try:
                rel = rp.relative_to(drive)
                depth = 0 if str(rel) == "." else len(rel.parts)
            except ValueError:
                depth = 99
            if depth >= 5:
                dirs[:] = []
            dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIR_NAMES and not d.startswith("$")]
            if (rp / DRAFT_CONTENT).is_file():
                add(rp.parent, "回收站" if rp.name == RECYCLE_BIN else "深度扫描")
                dirs[:] = []  # 草稿内部不再深入
        if timed_out:
            break
    return {"roots": list(found.values()), "deep": True, "timed_out": timed_out}


def list_drafts(root: Path):
    """列出根目录下的所有草稿项目（含剪映回收站 .recycle_bin 中的草稿，回收站排在最后）。"""
    normal, recycled = [], []  # [(dir, display_name)]

    def add_dir(d: Path, name: str, bucket: list):
        if is_draft_dir(d):
            bucket.append((d, name))

    add_dir(root, root.name, normal)
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            if child.name.startswith((".", "_")) and child.name != RECYCLE_BIN:
                continue
            add_dir(child, child.name, normal)
            if child.name == RECYCLE_BIN:
                for gc in sorted(child.iterdir()):
                    if gc.is_dir() and is_draft_dir(gc):
                        add_dir(gc, "🗑 " + gc.name + "（回收站）", recycled)

    result = []
    for d, name in normal + recycled:
        content = d / DRAFT_CONTENT
        try:
            st = content.stat()
            result.append({
                "name": name,
                "path": str(d),
                "current_size": st.st_size,
                "current_mtime": st.st_mtime,
                "current_md5": md5_of(content),
                "backup_count": len(list((d / BACKUP_DIR_NAME).glob("*.bak"))) if (d / BACKUP_DIR_NAME).is_dir() else 0,
            })
        except OSError:
            continue
    return result


def parse_bak_name(fname: str):
    """解析备份文件名 -> (kind, ts_str, hash)；无法识别返回 None。

    文件名格式：
      剪映自身备份：YYYYMMDDHHMMSS_<hash8>.save.bak / .close.bak / .load.bak
      本工具快照：  YYYYMMDDHHMMSS_restore_<hash8>.save.bak
    """
    m = RE_RESTORE_BAK.match(fname)
    if m:
        return ("restore", m.group(1), "")
    m = RE_JY_BAK.match(fname)
    if m:
        return (m.group(3), m.group(1), m.group(2))
    return None


def list_versions(draft_dir: Path):
    """收集某草稿的全部历史版本，按时间倒序。"""
    draft_dir = draft_dir.resolve()
    current = draft_dir / DRAFT_CONTENT
    cur_md5 = md5_of(current) if current.is_file() else ""
    cur_size = current.stat().st_size if current.is_file() else 0
    cur_mtime = current.stat().st_mtime if current.is_file() else 0

    entries = []
    if current.is_file():
        # 先把「当前版本」本身作为一个条目放进列表，方便前端高亮与对比
        entries.append({
            "file": DRAFT_CONTENT,
            "kind": "current",
            "label": KIND_LABEL["current"][0],
            "time": cur_mtime,
            "time_str": fmt_time(cur_mtime),
            "size": cur_size,
            "md5": cur_md5,
            "is_current": True,
            "delta_pct": 0.0,
            "in_backup_dir": False,
        })

    def add(file_path: Path, default_kind: str):
        """把一个备份文件转换成版本条目（统一时间、大小、MD5、与当前版差异）。"""
        try:
            st = file_path.stat()
        except OSError:
            return
        parsed = parse_bak_name(file_path.name)
        if parsed:
            kind, ts_str, _h = parsed
            try:
                t = datetime.strptime(ts_str, "%Y%m%d%H%M%S").timestamp()
                tstr = datetime.strptime(ts_str, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                t, tstr = st.st_mtime, fmt_time(st.st_mtime)
        else:
            kind, t, tstr = default_kind, st.st_mtime, fmt_time(st.st_mtime)
        md5 = md5_of(file_path)
        delta = ((st.st_size - cur_size) / cur_size * 100.0) if cur_size else 0.0
        entries.append({
            "file": file_path.name,
            "kind": kind,
            "label": KIND_LABEL.get(kind, ("历史版本", "#64748b", "#f1f5f9"))[0],
            "time": t,
            "time_str": tstr,
            "size": st.st_size,
            "md5": md5,
            "is_current": bool(cur_md5) and md5 == cur_md5,
            "delta_pct": round(delta, 1),
            "in_backup_dir": True,
        })

    bdir = draft_dir / BACKUP_DIR_NAME
    if bdir.is_dir():
        for f in sorted(bdir.glob("*.bak")):
            add(f, "save")          # .backup/ 内的备份，未知类型默认按「自动保存」处理
    prev = draft_dir / (DRAFT_CONTENT + ".bak")
    if prev.is_file():
        add(prev, "prev")            # 剪映保存的「上一版本」

    # 时间倒序：最新的版本排在最前面
    entries.sort(key=lambda e: (e["time"], e["file"]), reverse=True)
    return {
        "draft": str(draft_dir),
        "draft_name": draft_dir.name,
        "current_md5": cur_md5,
        "current_size": cur_size,
        "current_mtime": cur_mtime,
        "jianying_running": is_jianying_running(),
        "versions": entries,
    }


def safe_backup_file(draft_dir: Path, file_name: str) -> Path:
    """把用户输入的文件名安全地解析为 .backup/ 内的真实路径，防路径穿越。

    拒绝规则：空/相对路径标记、含分隔符、以「.」开头（隐藏文件与相对路径）。
    """
    if file_name in ("", ".", "..") or "/" in file_name or "\\" in file_name or file_name.startswith("."):
        raise ValueError("非法文件名")
    base = (draft_dir / BACKUP_DIR_NAME).resolve()
    target = (base / file_name).resolve()
    # 归一化后必须仍位于 .backup/ 目录之内，否则视为越界访问
    if not str(target).startswith(str(base) + os.sep) and target != base:
        raise ValueError("非法路径")
    if not target.is_file():
        raise ValueError("备份文件不存在: %s" % file_name)
    return target


def restore_version(draft_dir: Path, file_name: str) -> dict:
    """恢复指定历史版本为当前草稿（含自动备份与安全检查）。

    流程：
      1. 校验草稿目录合法；
      2. 加锁并确认剪映未运行（运行中会被剪映覆盖写入）；
      3. 把当前 draft_content.json 备份为「恢复快照」；
      4. 用所选历史版本覆盖 draft_content.json。
    """
    draft_dir = draft_dir.resolve()
    if not is_draft_dir(draft_dir):
        raise ValueError("不是有效的剪映草稿目录: %s" % draft_dir)

    with RESTORE_LOCK:
        if is_jianying_running():
            raise RuntimeError("剪映正在运行，请先关闭剪映再恢复版本（否则会被覆盖写入）")

        if file_name == DRAFT_CONTENT + ".bak":
            src = draft_dir / (DRAFT_CONTENT + ".bak")
            if not src.is_file():
                raise ValueError("备份文件不存在: %s" % file_name)
        else:
            src = safe_backup_file(draft_dir, file_name)

        content = draft_dir / DRAFT_CONTENT
        old_md5 = md5_of(content)

        # 自动备份当前草稿（可随时回退）
        bdir = draft_dir / BACKUP_DIR_NAME
        bdir.mkdir(exist_ok=True)
        snap_name = "%s_restore_%s.save.bak" % (datetime.now().strftime("%Y%m%d%H%M%S"), old_md5[:8])
        snap = bdir / snap_name
        shutil.copy2(content, snap)

        # 覆盖：src -> draft_content.json（copyfile 不保留元信息，符合覆盖语义）
        shutil.copyfile(src, content)

        return {
            "ok": True,
            "message": "已恢复版本 %s" % file_name,
            "backup_of_previous": snap.name,
            "previous_md5": old_md5,
            "new_md5": md5_of(content),
            "new_size": content.stat().st_size,
        }


def cleanup_backups(draft_dir: Path, keep: int = 20) -> dict:
    """清理过旧的「自动保存」备份，保留最近 keep 个。

    安全策略：
      - 剪映运行中拒绝操作；
      - 只清理剪映自动保存的 .save.bak，绝不触碰 close/load 备份与恢复快照；
      - 内容与当前草稿相同的备份一律保留；
      - keep < 1 拒绝。
    """
    draft_dir = draft_dir.resolve()
    if not is_draft_dir(draft_dir):
        raise ValueError("不是有效的剪映草稿目录: %s" % draft_dir)
    try:
        keep = int(keep)
    except (TypeError, ValueError):
        raise ValueError("保留数量必须是整数")
    if keep < 1:
        raise ValueError("保留数量至少为 1")

    with RESTORE_LOCK:
        if is_jianying_running():
            raise RuntimeError("剪映正在运行，请先关闭剪映再进行清理")
        bdir = draft_dir / BACKUP_DIR_NAME
        if not bdir.is_dir():
            return {"ok": True, "deleted": [], "freed": 0, "kept": 0, "total": 0}

        cur_md5 = md5_of(draft_dir / DRAFT_CONTENT)
        candidates = []
        for f in bdir.glob("*.save.bak"):
            if RE_RESTORE_BAK.match(f.name):
                continue  # 恢复快照永不清理
            try:
                if md5_of(f) == cur_md5:
                    continue  # 与当前草稿内容相同，保留
                t = datetime.strptime(f.name[:14], "%Y%m%d%H%M%S").timestamp()
            except (ValueError, OSError):
                try:
                    t = f.stat().st_mtime
                except OSError:
                    continue
            candidates.append((t, f))

        candidates.sort(key=lambda x: x[0], reverse=True)  # 新 → 旧
        to_delete = candidates[keep:]
        deleted, freed = [], 0
        for _t, f in to_delete:
            try:
                sz = f.stat().st_size
                f.unlink()
                deleted.append(f.name)
                freed += sz
            except OSError:
                continue
        return {
            "ok": True,
            "deleted": deleted,
            "freed": freed,
            "kept": min(keep, len(candidates)),
            "total": len(candidates),
        }


def delete_version(draft_dir: Path, file_name: str) -> dict:
    """删除某个历史版本备份文件（不可逆，需确认）。"""
    draft_dir = draft_dir.resolve()
    if not is_draft_dir(draft_dir):
        raise ValueError("不是有效的剪映草稿目录: %s" % draft_dir)

    with RESTORE_LOCK:
        if is_jianying_running():
            raise RuntimeError("剪映正在运行，请先关闭剪映再操作")
        if file_name == DRAFT_CONTENT:
            raise ValueError("不能删除当前草稿文件 draft_content.json")
        if file_name == DRAFT_CONTENT + ".bak":
            target = draft_dir / (DRAFT_CONTENT + ".bak")
        else:
            target = safe_backup_file(draft_dir, file_name)
        if not target.is_file():
            raise ValueError("文件不存在: %s" % file_name)
        freed = target.stat().st_size
        target.unlink()
        return {"ok": True, "message": "已删除 %s" % file_name, "deleted": file_name, "freed": freed}


# ---------------------------------------------------------------- 前端页面

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>剪映草稿历史版本管理器</title>
<style>
  :root {
    --bg: #f4f6f9; --card: #ffffff; --border: #e2e8f0; --text: #1e293b;
    --muted: #64748b; --primary: #2563eb; --primary-h: #1d4ed8;
    --danger: #dc2626; --ok: #059669; --warn-bg: #fffbeb; --warn-bd: #fcd34d; --warn-tx: #92400e;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font: 14px/1.6 "Microsoft YaHei", "PingFang SC", "Segoe UI", system-ui, sans-serif; min-height: 100vh; }
  .wrap { max-width: 1080px; margin: 0 auto; padding: 24px 20px 60px; }
  header.top { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 18px; }
  header.top h1 { font-size: 20px; font-weight: 700; }
  header.top h1 small { font-size: 12px; color: var(--muted); font-weight: 400; margin-left: 8px; }
  .status-pill { display: inline-flex; align-items: center; gap: 6px; padding: 4px 12px; border-radius: 999px; font-size: 12px; font-weight: 600; border: 1px solid transparent; }
  .status-ok { background: #ecfdf5; color: var(--ok); border-color: #a7f3d0; }
  .status-bad { background: #fef2f2; color: var(--danger); border-color: #fecaca; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; }

  .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px; margin-bottom: 16px; box-shadow: 0 1px 2px rgba(15,23,42,.04); }
  .card h2 { font-size: 15px; font-weight: 700; margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
  .card h2 .hint { font-size: 12px; color: var(--muted); font-weight: 400; }

  .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  input[type=text] { flex: 1; min-width: 260px; padding: 8px 12px; border: 1px solid var(--border); border-radius: 8px; font-size: 13px; background: #fff; color: var(--text); }
  input[type=text]:focus { outline: 2px solid rgba(37,99,235,.25); border-color: var(--primary); }
  button { cursor: pointer; border: none; border-radius: 8px; font-size: 13px; font-weight: 600; padding: 8px 16px; transition: .15s; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .btn-primary { background: var(--primary); color: #fff; }
  .btn-primary:hover:not(:disabled) { background: var(--primary-h); }
  .btn-ghost { background: #fff; color: var(--text); border: 1px solid var(--border); }
  .btn-ghost:hover:not(:disabled) { border-color: #cbd5e1; background: #f8fafc; }
  .btn-danger { background: #fff; color: var(--danger); border: 1px solid #fecaca; }
  .btn-danger:hover:not(:disabled) { background: #fef2f2; border-color: var(--danger); }
  .btn-sm { padding: 5px 12px; font-size: 12px; }

  .draft-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 10px; }
  .draft-item { border: 1.5px solid var(--border); border-radius: 10px; padding: 12px 14px; cursor: pointer; background: #fff; transition: .15s; }
  .draft-item:hover { border-color: #bfdbfe; }
  .draft-item.active { border-color: var(--primary); background: #eff6ff; }
  .draft-item .nm { font-weight: 700; font-size: 13px; margin-bottom: 4px; word-break: break-all; }
  .draft-item .meta { font-size: 12px; color: var(--muted); }

  .kv { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px 24px; }
  .kv .k { font-size: 12px; color: var(--muted); }
  .kv .v { font-weight: 600; font-size: 13px; word-break: break-all; }
  .kv .v.mono { font-family: Consolas, "Courier New", monospace; font-size: 12px; }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; font-size: 12px; color: var(--muted); font-weight: 600; padding: 8px 10px; border-bottom: 1.5px solid var(--border); white-space: nowrap; }
  td { padding: 10px; border-bottom: 1px solid #f1f5f9; vertical-align: middle; }
  tr:hover td { background: #f8fafc; }
  tr.is-current td { background: #ecfdf5; }
  tr.is-current:hover td { background: #dcfce7; }
  .tag { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; white-space: nowrap; }
  .fname { font-family: Consolas, "Courier New", monospace; font-size: 12px; color: #475569; word-break: break-all; max-width: 300px; }
  .delta-up { color: var(--ok); font-weight: 600; }
  .delta-down { color: var(--danger); font-weight: 600; }
  .delta-zero { color: var(--muted); }
  .empty { text-align: center; color: var(--muted); padding: 36px 0; }

  .banner { border-radius: 10px; padding: 10px 14px; font-size: 13px; margin-bottom: 14px; border: 1px solid; }
  .banner.warn { background: var(--warn-bg); border-color: var(--warn-bd); color: var(--warn-tx); }
  .banner.info { background: #eff6ff; border-color: #bfdbfe; color: #1e40af; }

  #toast { position: fixed; top: 20px; left: 50%; transform: translateX(-50%) translateY(-80px); background: #0f172a; color: #fff; padding: 12px 22px; border-radius: 10px; font-size: 13px; font-weight: 600; box-shadow: 0 8px 24px rgba(0,0,0,.25); opacity: 0; transition: .3s; z-index: 99; max-width: 80vw; }
  #toast.show { transform: translateX(-50%) translateY(0); opacity: 1; }
  #toast.ok { background: #065f46; }
  #toast.err { background: #991b1b; }

  .chips { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
  .chip { padding: 4px 14px; border-radius: 999px; border: 1px solid var(--border); background: #fff; font-size: 12.5px; font-weight: 600; color: var(--muted); cursor: pointer; transition: .15s; }
  .chip:hover { border-color: #cbd5e1; color: var(--text); }
  .chip.active { background: var(--primary); color: #fff; border-color: var(--primary); }
  .chip .n { opacity: .7; font-weight: 400; margin-left: 2px; }

  .discover-box { margin-top: 14px; }
  .root-chip { display: inline-flex; align-items: center; gap: 8px; border: 1px solid var(--border); background: #fff; border-radius: 10px; padding: 8px 14px; margin: 0 8px 8px 0; cursor: pointer; font-size: 12.5px; transition: .15s; max-width: 100%; }
  .root-chip:hover { border-color: var(--primary); background: #eff6ff; }
  .root-chip .p { font-family: Consolas, "Courier New", monospace; color: #334155; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 420px; }
  .root-chip .c { color: var(--muted); white-space: nowrap; }
  .root-chip .src { font-size: 11px; background: #f1f5f9; border-radius: 6px; padding: 1px 8px; color: #64748b; white-space: nowrap; }

  .spin { display: inline-block; width: 14px; height: 14px; border: 2px solid #cbd5e1; border-top-color: var(--primary); border-radius: 50%; animation: rot .7s linear infinite; vertical-align: -2px; }
  @keyframes rot { to { transform: rotate(360deg); } }

  /* ---------- 页脚版权 ---------- */
  .foot { margin-top: 28px; padding: 16px 10px 0; border-top: 1px solid var(--border); text-align: center; font-size: 12px; color: var(--muted); line-height: 1.9; }
  @media (max-width: 720px) { .fname { max-width: 140px; } th:nth-child(4), td:nth-child(4) { display: none; } }
</style>
</head>
<body>
<div class="wrap">

  <header class="top">
    <h1>剪映草稿历史版本管理器 <small>查询 .backup 历史版本 · 一键恢复</small></h1>
    <span id="jyStatus" class="status-pill status-ok"><span class="dot"></span><span id="jyStatusText">检测中…</span></span>
  </header>

  <div id="warnBox" class="banner warn" style="display:none">⚠️ 检测到剪映正在运行，恢复操作已被禁止。请先完全关闭剪映（包括托盘进程），再执行恢复。</div>

  <div class="card">
    <h2>① 选择草稿根目录 <span class="hint">即剪映的 “JianyingPro Drafts” 目录，可手动输入，也可自动发现</span></h2>
    <div class="row">
      <input type="text" id="rootInput" placeholder="例如：D:\AI短剧-素材库\JianyingPro Drafts">
      <button class="btn-primary" id="scanBtn" onclick="scanRoot()">扫描草稿</button>
      <button class="btn-ghost" id="discoverBtn" onclick="discoverRoots(false)">🔍 自动发现</button>
      <button class="btn-ghost" id="deepBtn" onclick="discoverRoots(true)">🔎 深度扫描</button>
    </div>
    <div class="discover-box" id="discoverBox"></div>
  </div>

  <div class="card" id="draftCard" style="display:none">
    <h2>② 选择草稿项目 <span class="hint" id="draftCount"></span></h2>
    <div class="draft-list" id="draftList"></div>
  </div>

  <div class="card" id="curCard" style="display:none">
    <h2>③ 当前草稿版本</h2>
    <div class="kv" id="curInfo"></div>
  </div>

  <div class="card" id="verCard" style="display:none">
    <h2>④ 历史版本列表 <span class="hint">按时间倒序 · 点击「恢复此版本」会把所选版本设为当前草稿（恢复前自动备份当前版本）</span>
      <button class="btn-ghost btn-sm" id="cleanupBtn" onclick="doCleanup()" style="margin-left:auto;flex:none">🧹 清理旧备份</button>
    </h2>
    <div class="chips" id="filterChips">
      <button class="chip active" data-k="all">全部</button>
      <button class="chip" data-k="save">自动保存</button>
      <button class="chip" data-k="close">关闭备份</button>
      <button class="chip" data-k="load">打开备份</button>
      <button class="chip" data-k="restore">恢复快照</button>
      <button class="chip" data-k="prev">上一版本</button>
      <button class="chip" data-k="current">当前版本</button>
    </div>
    <table>
      <thead>
        <tr>
          <th>#</th><th>时间</th><th>类型</th><th>文件</th><th>大小</th><th>与当前差异</th><th>MD5</th><th style="text-align:right">操作</th>
        </tr>
      </thead>
      <tbody id="verBody"></tbody>
    </table>
    <div class="empty" id="verEmpty" style="display:none">没有找到历史版本（.backup 目录为空）</div>
  </div>

</div>
<div id="toast"></div>

<footer class="foot">
  剪映草稿历史版本管理器 · 公众号：爱折腾的摄影大叔（lovephoto588）<br>
  Copyright (c) 2026 爱折腾的摄影大叔（lovephoto588） · 基于 MIT 协议开源
</footer>

<script>
/* ---------------------------------------------------------- 全局状态 */
var state = { root: "", drafts: [], draft: null, versions: [], jyRunning: false, filterKind: "all" };

/* ---------------------------------------------------------- 基础 UI 工具 */

// 顶部浮动提示条
function toast(msg, type) {
  var t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "show " + (type || "");
  clearTimeout(t._timer);
  t._timer = setTimeout(function(){ t.className = ""; }, 4200);
}
function fmtSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n/1024).toFixed(1) + " KB";
  return (n/1048576).toFixed(2) + " MB";
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  });
}

// 刷新剪映运行状态（页面加载时调用一次）
function setJyStatus(running) {
  state.jyRunning = running;
  var el = document.getElementById("jyStatus");
  var tx = document.getElementById("jyStatusText");
  if (running) {
    el.className = "status-pill status-bad";
    tx.textContent = "剪映运行中 · 已锁定恢复";
    document.getElementById("warnBox").style.display = "block";
  } else {
    el.className = "status-pill status-ok";
    tx.textContent = "剪映未运行 · 可安全恢复";
    document.getElementById("warnBox").style.display = "none";
  }
}

// 按输入的根目录扫描草稿列表
function scanRoot() {
  var root = document.getElementById("rootInput").value.trim();
  if (!root) { toast("请先填写草稿根目录", "err"); return; }
  var btn = document.getElementById("scanBtn");
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span> 扫描中';
  fetch("/api/drafts?root=" + encodeURIComponent(root))
    .then(function(r){ return r.json(); })
    .then(function(d){
      btn.disabled = false; btn.textContent = "扫描草稿";
      if (d.error) { toast(d.error, "err"); return; }
      state.root = d.root; state.drafts = d.drafts;
      document.getElementById("draftCard").style.display = "block";
      document.getElementById("draftCount").textContent = "共 " + d.drafts.length + " 个草稿";
      renderDrafts();
      if (d.drafts.length === 0) toast("该目录下没有发现草稿（含 draft_content.json 的子目录）", "err");
    })
    .catch(function(e){ btn.disabled = false; btn.textContent = "扫描草稿"; toast("扫描失败: " + e, "err"); });
}

// 渲染草稿卡片列表
function renderDrafts() {
  var box = document.getElementById("draftList");
  box.innerHTML = "";
  state.drafts.forEach(function(d, i){
    var div = document.createElement("div");
    div.className = "draft-item" + (state.draft === d.path ? " active" : "");
    div.innerHTML = '<div class="nm">' + esc(d.name) + '</div>' +
      '<div class="meta">当前 ' + fmtSize(d.current_size) + ' · 历史 ' + d.backup_count + ' 个</div>';
    div.onclick = function(){ selectDraft(d.path); };
    box.appendChild(div);
  });
}

// 选中草稿并加载其版本列表
function selectDraft(path) {
  state.draft = path;
  renderDrafts();
  loadVersions();
}

// 拉取当前草稿的详细信息 + 历史版本列表
function loadVersions() {
  if (!state.draft) return;
  document.getElementById("curCard").style.display = "none";
  document.getElementById("verCard").style.display = "none";
  fetch("/api/versions?path=" + encodeURIComponent(state.draft))
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (d.error) { toast(d.error, "err"); return; }
      state.versions = d.versions;
      setJyStatus(d.jianying_running);
      document.getElementById("curInfo").innerHTML =
        '<div><div class="k">草稿名称</div><div class="v">' + esc(d.draft_name) + '</div></div>' +
        '<div><div class="k">当前文件</div><div class="v mono">draft_content.json</div></div>' +
        '<div><div class="k">大小</div><div class="v">' + fmtSize(d.current_size) + '</div></div>' +
        '<div><div class="k">修改时间</div><div class="v">' + esc(new Date(d.current_mtime*1000).toLocaleString("zh-CN", {hour12:false})) + '</div></div>' +
        '<div><div class="k">MD5</div><div class="v mono">' + esc(d.current_md5) + '</div></div>' +
        '<div><div class="k">历史版本数</div><div class="v">' + d.versions.filter(function(v){return !v.is_current;}).length + ' 个</div>';
      document.getElementById("curCard").style.display = "block";
      renderVersions();
      document.getElementById("verCard").style.display = "block";
    })
    .catch(function(e){ toast("加载版本失败: " + e, "err"); });
}

// 渲染版本表格（按筛选条件过滤）
function renderVersions() {
  var body = document.getElementById("verBody");
  body.innerHTML = "";
  var list = state.versions.filter(function(v){
    return state.filterKind === "all" || v.kind === state.filterKind;
  });
  document.getElementById("verEmpty").style.display = list.length ? "none" : "block";
  document.getElementById("verEmpty").textContent = state.versions.length
    ? "当前筛选条件下没有版本" : "没有找到历史版本（.backup 目录为空）";
  list.forEach(function(v, i){
    var tr = document.createElement("tr");
    if (v.is_current) tr.className = "is-current";
    var delta;
    if (v.is_current) delta = '<span class="delta-zero">—</span>';
    else if (v.delta_pct > 0) delta = '<span class="delta-up">+' + v.delta_pct + '%</span>';
    else if (v.delta_pct < 0) delta = '<span class="delta-down">' + v.delta_pct + '%</span>';
    else delta = '<span class="delta-zero">0%</span>';
    var btn;
    if (v.is_current) {
      btn = '<span style="color:var(--muted);font-size:12px">正在使用</span>';
    } else {
      btn = '<button class="btn-primary btn-sm" style="margin-right:6px">恢复</button>' +
            '<button class="btn-danger btn-sm">删除</button>';
    }
    tr.innerHTML =
      '<td>' + (i+1) + '</td>' +
      '<td style="white-space:nowrap">' + esc(v.time_str) + '</td>' +
      '<td><span class="tag" style="color:' + tagColor(v.kind, 0) + ';background:' + tagColor(v.kind, 1) + '">' + esc(v.label) + '</span></td>' +
      '<td><div class="fname">' + esc(v.file) + '</div></td>' +
      '<td style="white-space:nowrap">' + fmtSize(v.size) + '</td>' +
      '<td>' + delta + '</td>' +
      '<td class="fname" style="max-width:110px">' + esc(v.md5.substring(0,10)) + '…</td>' +
      '<td style="text-align:right;white-space:nowrap">' + btn + '</td>';
    if (!v.is_current) {
      var btns = tr.querySelectorAll("button");
      // 闭包绑定，避免路径/文件名中的特殊字符被转义
      btns[0].onclick = function(){ doRestore(v.file); };
      btns[1].onclick = function(){ doDelete(v); };
    }
    body.appendChild(tr);
  });
}

// 取版本类型对应的前景色 / 背景色
function tagColor(kind, idx) {
  var map = {
    save:    ["#2563eb", "#eff6ff"],
    close:   ["#d97706", "#fffbeb"],
    load:    ["#64748b", "#f1f5f9"],
    restore: ["#7c3aed", "#f5f3ff"],
    prev:    ["#059669", "#ecfdf5"],
    current: ["#059669", "#ecfdf5"]
  };
  return (map[kind] || ["#64748b", "#f1f5f9"])[idx];
}

// 恢复指定历史版本（二次确认后调用 /api/restore）
function doRestore(file) {
  if (state.jyRunning) { toast("剪映正在运行，请先关闭剪映", "err"); return; }
  var v = null;
  state.versions.forEach(function(x){ if (x.file === file) v = x; });
  var msg = "确定要用以下历史版本覆盖当前草稿吗？\n\n" +
            "版本：" + (v ? v.label + " · " + v.time_str : file) + "\n" +
            "文件：" + file + "\n" +
            "大小：" + (v ? fmtSize(v.size) : "?") + "\n\n" +
            "恢复前会自动把当前草稿备份到 .backup/ 目录（可随时回退）。\n" +
            "恢复后需要重新打开剪映加载该草稿。";
  if (!confirm(msg)) return;
  fetch("/api/restore", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({path: state.draft, file: file})
  })
  .then(function(r){ return r.json(); })
  .then(function(d){
    if (!d.ok) { toast("恢复失败: " + d.error, "err"); return; }
    toast("✅ 已恢复 " + file + "（原版本已备份为 " + d.backup_of_previous + "）", "ok");
    loadVersions();
  })
  .catch(function(e){ toast("恢复失败: " + e, "err"); });
}

// 自动发现草稿根目录
function discoverRoots(deep) {
  var btn = document.getElementById(deep ? "deepBtn" : "discoverBtn");
  var old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> ' + (deep ? "深度扫描中" : "发现中");
  fetch("/api/discover?deep=" + (deep ? "1" : "0"))
    .then(function(r){ return r.json(); })
    .then(function(d){
      btn.disabled = false; btn.innerHTML = old;
      renderDiscover(d);
    })
    .catch(function(e){
      btn.disabled = false; btn.innerHTML = old;
      toast("自动发现失败: " + e, "err");
    });
}

function renderDiscover(d) {
  var box = document.getElementById("discoverBox");
  if (!d.roots || !d.roots.length) {
    box.innerHTML = '<div class="tip warn">' +
      (d.timed_out ? "深度扫描超时，结果可能不完整。" : "未在常见位置发现草稿根目录。") +
      ' 可尝试「🔎 深度扫描」（遍历各盘符，较慢），或手动输入目录路径。</div>';
    return;
  }
  box.innerHTML = '<div class="tip info">发现 <b>' + d.roots.length + '</b> 个草稿根目录' +
    (d.timed_out ? "（深度扫描超时，以下为部分结果）" : "") + '，点击即可扫描：</div>';
  // 注意：必须用闭包绑定，不能拼进 onclick 字符串——否则路径中的反斜杠会被 JS 转义吞掉
  d.roots.forEach(function(r){
    var chip = document.createElement("span");
    chip.className = "root-chip";
    chip.title = "点击扫描该目录";
    chip.innerHTML = '<span class="p">' + esc(r.path) + '</span>' +
      '<span class="c">' + r.count + ' 个草稿</span>' +
      '<span class="src">' + esc(r.source) + '</span>';
    chip.onclick = function(){ useRoot(r.path); };
    box.appendChild(chip);
  });
}

function useRoot(p) {
  // 点击发现结果后填入路径并立即扫描
  document.getElementById("rootInput").value = p;
  scanRoot();
}

// 类型筛选
document.querySelectorAll("#filterChips .chip").forEach(function(chip){
  chip.onclick = function(){
    document.querySelectorAll("#filterChips .chip").forEach(function(c){ c.classList.remove("active"); });
    chip.classList.add("active");
    state.filterKind = chip.getAttribute("data-k");
    renderVersions();
  };
});

// 删除历史版本（不可逆）
function doDelete(v) {
  if (state.jyRunning) { toast("剪映正在运行，请先关闭剪映", "err"); return; }
  var extra = "";
  if (v.kind === "restore") {
    extra = "\n\n⚠️ 这是「恢复快照」——删除后将无法回退到该次恢复前的状态。";
  } else if (v.kind === "prev") {
    extra = "\n\n⚠️ 这是剪映保存的「上一版本」，通常是内容最全的版本之一。";
  }
  var msg = "确定要永久删除这个历史版本吗？\n\n" +
            "类型：" + v.label + " · " + v.time_str + "\n" +
            "文件：" + v.file + "\n" +
            "大小：" + fmtSize(v.size) + extra +
            "\n\n此操作不可恢复（不会进回收站）。";
  if (!confirm(msg)) return;
  fetch("/api/delete", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({path: state.draft, file: v.file})
  })
  .then(function(r){ return r.json(); })
  .then(function(d){
    if (!d.ok) { toast("删除失败: " + d.error, "err"); return; }
    toast("🗑 已删除 " + v.file, "ok");
    loadVersions();
  })
  .catch(function(e){ toast("删除失败: " + e, "err"); });
}

// 清理旧备份
function doCleanup() {
  if (state.jyRunning) { toast("剪映正在运行，请先关闭剪映", "err"); return; }
  if (!state.draft) return;
  var saveCount = state.versions.filter(function(v){ return v.kind === "save"; }).length;
  var input = prompt(
    "保留最近多少个「自动保存」备份？（当前共 " + saveCount + " 个）\n\n" +
    "更早的自动保存将被永久删除；关闭/打开备份、恢复快照、当前版本不受影响。",
    "20"
  );
  if (input === null) return;
  var keep = parseInt(input, 10);
  if (!keep || keep < 1) { toast("请输入不小于 1 的数字", "err"); return; }
  if (!confirm("确认清理？\n\n将永久删除较早的「自动保存」备份（保留最近 " + keep + " 个），删除后无法恢复。")) return;
  fetch("/api/cleanup", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({path: state.draft, keep: keep})
  })
  .then(function(r){ return r.json(); })
  .then(function(d){
    if (!d.ok) { toast("清理失败: " + d.error, "err"); return; }
    var freed = d.freed >= 1048576 ? (d.freed/1048576).toFixed(2) + " MB" : (d.freed/1024).toFixed(1) + " KB";
    if (d.deleted.length) {
      toast("✅ 已清理 " + d.deleted.length + " 个旧备份，释放 " + freed, "ok");
    } else {
      toast("没有需要清理的备份（保留 " + d.kept + " 个，共 " + d.total + " 个可清理项）", "ok");
    }
    loadVersions();
  })
  .catch(function(e){ toast("清理失败: " + e, "err"); });
}

// 初始化：自动发现根目录 → 优先默认根 → 自动扫描
(function(){
  fetch("/api/state").then(function(r){return r.json();}).then(function(d){
    document.getElementById("rootInput").value = d.default_root;
    fetch("/api/discover?deep=0")
      .then(function(r){ return r.json(); })
      .then(function(dd){
        if (dd.roots && dd.roots.length) {
          var hit = dd.roots.some(function(r){ return r.path === d.default_root; });
          if (!hit) document.getElementById("rootInput").value = dd.roots[0].path;
        }
        scanRoot();
      })
      .catch(function(){ scanRoot(); });
  });
  setInterval(function(){
    fetch("/api/state").then(function(r){return r.json();}).then(function(d){
      setJyStatus(d.jianying_running);
    }).catch(function(){});
  }, 5000);
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- HTTP 服务


class Handler(BaseHTTPRequestHandler):
    """HTTP 接口处理器：只读接口用 GET，写操作（恢复/清理/删除）用 POST。"""

    server_version = "JianyingDraftHistory/1.0"

    def log_message(self, fmt, *args):  # 静默默认日志
        pass

    def _send_json(self, obj, status=200):
        """以 application/json 返回响应体。"""
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str):
        """返回内嵌的单文件前端页面。"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        """只读接口：页面、状态、草稿列表、自动发现、版本列表。"""
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send_html(INDEX_HTML)
            return

        if path == "/api/state":
            self._send_json({
                "default_root": str(find_default_root()),
                "jianying_running": is_jianying_running(),
            })
            return

        if path == "/api/drafts":
            root = Path(qs.get("root", [str(find_default_root())])[0]).expanduser()
            if not root.is_dir():
                self._send_json({"error": "目录不存在: %s" % root})
                return
            self._send_json({"root": str(root), "drafts": list_drafts(root)})
            return

        if path == "/api/discover":
            deep = qs.get("deep", ["0"])[0] in ("1", "true", "yes")
            try:
                timeout = float(qs.get("timeout", ["60"])[0])
            except ValueError:
                timeout = 60.0
            self._send_json(discover_roots(deep=deep, timeout=timeout))
            return

        if path == "/api/versions":
            raw = qs.get("path", [""])[0]
            if not raw:
                self._send_json({"error": "缺少 path 参数"})
                return
            d = Path(raw).expanduser()
            if not is_draft_dir(d):
                self._send_json({"error": "不是有效的剪映草稿目录: %s" % d})
                return
            self._send_json(list_versions(d))
            return

        self._send_json({"error": "Not Found"}, status=404)

    def do_POST(self):
        """写操作接口：/api/restore 恢复版本、/api/cleanup 清理备份、/api/delete 删除版本。"""
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path not in ("/api/restore", "/api/cleanup", "/api/delete"):
            self._send_json({"error": "Not Found"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            draft = Path(payload.get("path", "")).expanduser()
            if path == "/api/restore":
                fname = payload.get("file", "")
                result = restore_version(draft, fname)
            elif path == "/api/cleanup":
                result = cleanup_backups(draft, payload.get("keep", 20))
            else:
                result = delete_version(draft, payload.get("file", ""))
            self._send_json(result)
        except (ValueError, RuntimeError) as e:
            self._send_json({"ok": False, "error": str(e)}, status=400)
        except Exception as e:  # noqa: BLE001
            self._send_json({"ok": False, "error": "服务器错误: %s" % e}, status=500)


def main():
    """命令行入口：解析参数 → 启动本地服务 → 打印访问地址。"""
    ap = argparse.ArgumentParser(description="剪映草稿历史版本管理器")
    ap.add_argument("--port", type=int, default=8620, help="服务端口（默认 8620）")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    args = ap.parse_args()

    root = find_default_root()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d" % (args.host, args.port)
    print("=" * 56)
    print("  剪映草稿历史版本管理器已启动")
    print("  默认草稿根目录: %s" % root)
    print("  请在浏览器打开: %s" % url)
    print("  按 Ctrl+C 停止服务")
    print("=" * 56)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")


if __name__ == "__main__":
    main()
