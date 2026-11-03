# -*- coding: utf-8 -*-
# ============================================================
#  剪映草稿历史版本管理器 · API 自动化测试
# ------------------------------------------------------------
#  公众号：爱折腾的摄影大叔（lovephoto588）
#  本文档（含本文件及配套文档）的归属与开发均属于
#  「爱折腾的摄影大叔（lovephoto588）」，版权所有。
# ============================================================
"""API 测试脚本：只读接口用真实草稿，恢复安全锁走 HTTP，完整恢复流程走模块级测试。

用法：先启动服务（python app.py），再运行 python test_api.py。
"""
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8620"          # 被测服务地址
REAL_DRAFT = r"D:\AI短剧-素材库\JianyingPro Drafts\1、聊斋志异·婴宁"  # 真实草稿（只读）
passed = failed = 0


def get(path):
    """发起 GET 请求并解析 JSON 响应。"""
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, payload):
    """发起 POST 请求，返回 (状态码, JSON 响应体)；HTTP 错误码同样解析为 JSON。"""
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, detail=""):
    """记录并打印一条断言结果。"""
    global passed, failed
    if cond:
        passed += 1
        print("  [PASS] %s" % name)
    else:
        failed += 1
        print("  [FAIL] %s %s" % (name, detail))


print("== 1. /api/state ==")
st = get("/api/state")
print("  ", st)
check("返回默认根目录", "default_root" in st)
check("返回剪映状态", "jianying_running" in st)

print("== 2. /api/drafts (真实) ==")
d = get("/api/drafts")
print("   root:", d["root"], "| drafts:", [x["name"] for x in d["drafts"]])
check("发现草稿项目", len(d["drafts"]) >= 1 and d["drafts"][0]["name"].startswith("1"))
check("草稿含 md5/大小", len(d["drafts"][0]["current_md5"]) == 32)
check("回收站草稿被标注", any("回收站" in x["name"] for x in d["drafts"]), str([x["name"] for x in d["drafts"]]))

print("== 3. /api/versions (真实草稿，只读) ==")
v = get("/api/versions?path=" + urllib.parse.quote(REAL_DRAFT))
print("   current: %d bytes, versions: %d" % (v["current_size"], len(v["versions"])))
check("版本列表非空", len(v["versions"]) >= 14)
check("当前版被标记", any(x["is_current"] for x in v["versions"]))
kinds = set(x["kind"] for x in v["versions"])
check("类型齐全", {"current", "save", "close", "load", "restore"} <= kinds, str(kinds))
check("倒序排列", [x["time"] for x in v["versions"]] == sorted([x["time"] for x in v["versions"]], reverse=True))

print("== 4. /api/restore 安全锁（剪映运行中必须拒绝） ==")
tmp = Path(tempfile.mkdtemp(prefix="jy_test_"))
draft = tmp / "test_draft"
(draft / ".backup").mkdir(parents=True)
(draft / "draft_content.json").write_bytes(b"CURRENT-CONTENT-1234567890")
shutil.copyfile(__file__, draft / ".backup" / "20261027021111_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.save.bak")
shutil.copyfile(__file__, draft / ".backup" / "20261027233450_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.close.bak")

code, r = post("/api/restore", {"path": str(draft), "file": "20261027233450_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.close.bak"})
print("   ", code, r)
if r.get("jianying") or "剪映正在运行" in str(r.get("error", "")):
    check("剪映运行时拒绝恢复（安全锁生效）", code == 400)
else:
    check("恢复成功", r.get("ok") is True)
    check("生成恢复快照", "_restore_" in r.get("backup_of_previous", ""))
    check("当前文件已替换", (draft / "draft_content.json").read_bytes() == Path(__file__).read_bytes())
    snap = draft / ".backup" / r["backup_of_previous"]
    check("快照内容为原当前版", snap.read_bytes() == b"CURRENT-CONTENT-1234567890")

print("== 4b. restore_version 完整流程（模块级，跳过进程检查） ==")
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["JY_TOOL_SKIP_PROCESS_CHECK"] = "1"   # 让模块级测试跳过剪映进程检测
import app as appmod  # noqa: E402

(draft / "draft_content.json").write_bytes(b"CURRENT-CONTENT-1234567890")
res = appmod.restore_version(draft, "20261027233450_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.close.bak")
print("   ", res)
check("恢复成功", res["ok"] is True)
check("生成恢复快照", "_restore_" in res["backup_of_previous"])
check("当前文件已替换", (draft / "draft_content.json").read_bytes() == Path(__file__).read_bytes())
snap = draft / ".backup" / res["backup_of_previous"]
check("快照内容为原当前版", snap.read_bytes() == b"CURRENT-CONTENT-1234567890")

print("== 4c. 安全防护 ==")
# 非法文件名必须被拒绝：路径穿越、不存在的文件、非草稿目录
for bad, why in [("../../app.py", "路径穿越"), ("nonexistent.bak", "不存在的文件")]:
    try:
        appmod.restore_version(draft, bad)
        check("拒绝" + why, False)
    except (ValueError, RuntimeError) as e:
        check("拒绝" + why, True)
try:
    appmod.restore_version(tmp, "x.save.bak")
    check("拒绝非草稿目录", False)
except (ValueError, RuntimeError):
    check("拒绝非草稿目录", True)

print("== 4d. cleanup_backups 清理逻辑（模块级） ==")
# 构造：25 个不同时间的 save.bak + 1 个 close.bak + 1 个 restore 快照 + 1 个与当前版内容相同的 save.bak
cdir = tmp / "clean_draft"
(cdir / ".backup").mkdir(parents=True)
(cdir / "draft_content.json").write_bytes(b"NOW-CONTENT-000")
for i in range(25):
    (cdir / ".backup" / ("20261001%02d0000_%032d.save.bak" % (i + 1, i))).write_bytes(("OLD-%d" % i).encode())
(cdir / ".backup" / "20261027000000_cccccccccccccccccccccccccccccccc.close.bak").write_bytes(b"CLOSE-BAK")
(cdir / ".backup" / "20261028000000_restore_deadbeef.save.bak").write_bytes(b"RESTORE-SNAP")
(cdir / ".backup" / "20260930000000_99999999999999999999999999999999.save.bak").write_bytes(b"NOW-CONTENT-000")  # 与当前相同

res = appmod.cleanup_backups(cdir, 5)
print("   deleted:", len(res["deleted"]), "kept:", res["kept"], "total:", res["total"])
check("清理成功", res["ok"] is True)
check("删除 20 个旧 save", len(res["deleted"]) == 20, str(len(res["deleted"])))
check("保留最近 5 个 save", res["kept"] == 5)
check("close 备份保留", (cdir / ".backup" / "20261027000000_cccccccccccccccccccccccccccccccc.close.bak").is_file())
check("恢复快照保留", (cdir / ".backup" / "20261028000000_restore_deadbeef.save.bak").is_file())
check("与当前版相同的备份保留", (cdir / ".backup" / "20260930000000_99999999999999999999999999999999.save.bak").is_file())
try:
    appmod.cleanup_backups(cdir, 0)
    check("拒绝 keep<1", False)
except (ValueError, RuntimeError):
    check("拒绝 keep<1", True)

print("== 4e. 首页 HTML ==")
with urllib.request.urlopen(BASE + "/", timeout=10) as resp:
    body = resp.read().decode("utf-8")
check("首页可访问", "历史版本列表" in body and "恢复此版本" in body)

shutil.rmtree(tmp, ignore_errors=True)

print("== 5. /api/discover 自动发现 ==")
dq = get("/api/discover?deep=0")
print("   quick roots:", [(r["path"], r["count"]) for r in dq["roots"]])
check("quick 发现根目录", len(dq["roots"]) >= 1)
check("结果含计数与来源", all("count" in r and "source" in r for r in dq["roots"]))
check("发现工具同级根目录", any("JianyingPro Drafts" in r["path"] for r in dq["roots"]))
dd = get("/api/discover?deep=1&timeout=5")
print("   deep roots:", len(dd["roots"]), "timed_out:", dd["timed_out"])
check("deep 返回结构完整", "roots" in dd and "timed_out" in dd)
check("deep 覆盖 quick 结果", any(r["path"] in [x["path"] for x in dq["roots"]] for r in dd["roots"]))

print("== 6. 回归: 反斜杠路径与前端转义 ==")
p = urllib.parse.quote(r"D:\AI短剧-素材库\JianyingPro Drafts", safe="")
r = get("/api/drafts?root=" + p)
check("带反斜杠的 root 参数正常", "drafts" in r and len(r["drafts"]) >= 1, str(r)[:150])
with urllib.request.urlopen(BASE + "/", timeout=10) as resp:
    body = resp.read().decode("utf-8")
check("HTML 无 onclick 内联路径拼接", 'onclick="useRoot(' not in body and 'onclick="doRestore(' not in body)
check("HTML 使用闭包绑定", "chip.onclick = function(){ useRoot(r.path); };" in body)

print("== 7. /api/delete 删除历史版本 ==")
ddir = tmp / "del_draft"
(ddir / ".backup").mkdir(parents=True)
(ddir / "draft_content.json").write_bytes(b"KEEP-CURRENT")
(ddir / ".backup" / "20261027021111_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.save.bak").write_bytes(b"TO-DELETE")
(ddir / ".backup" / "20261027233450_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.close.bak").write_bytes(b"KEEP-CLOSE")

code, r = post("/api/delete", {"path": str(ddir), "file": "20261027021111_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.save.bak"})
print("   ", code, r)
if code == 400 and "剪映正在运行" in str(r.get("error", "")):
    check("剪映运行时拒绝删除（安全锁生效）", True)
else:
    check("删除成功", r.get("ok") is True)
    check("文件已删除", not (ddir / ".backup" / "20261027021111_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.save.bak").exists())
    check("其他备份保留", (ddir / ".backup" / "20261027233450_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.close.bak").is_file())
    check("当前草稿保留", (ddir / "draft_content.json").is_file())

for bad, why in [("draft_content.json", "删除当前草稿文件"), ("nope.bak", "不存在的文件"), ("../../app.py", "路径穿越")]:
    try:
        appmod.delete_version(ddir, bad)
        check("拒绝" + why, False)
    except (ValueError, RuntimeError):
        check("拒绝" + why, True)

print("\n结果: %d 通过, %d 失败" % (passed, failed))
sys.exit(1 if failed else 0)
