#!/usr/bin/env python3
"""小院子前端展示页的小型后端。

职责刻意收得很窄：
- GET  /            返回单文件前端（index.html）
- GET  /api/state   借助 garden.py 的官方只读快照组装页面数据
- POST /api/action  把白名单内的『home 院子 …』命令原样转给真正的后端 CLI

院子规则全部由 garden.py / home.py 维护，这里不重新实现任何生长逻辑。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MINI_YARD_HOME = Path(os.environ.get("MINI_YARD_HOME", Path(__file__).resolve().parent.parent))
if str(MINI_YARD_HOME) not in sys.path:
    sys.path.insert(0, str(MINI_YARD_HOME))

import garden  # noqa: E402
import weather  # noqa: E402

PORT = int(os.environ.get("GARDEN_WEB_PORT", "8137"))
# 安全规范（见 ../SECURITY_GUIDELINES.md）：默认只绑回环，外部访问走 SSH 隧道
HOST = os.environ.get("GARDEN_WEB_HOST", "127.0.0.1")
STATIC_DIR = Path(__file__).parent

ALLOWED_COMMANDS = {
    "查看", "逛逛", "记录", "手账", "篮子",
    "播种", "浇水", "除虫", "修剪", "松土", "施肥", "清理", "收获", "排水",
    "投喂", "摸摸", "陪玩", "取名", "做点吃的", "送给user", "送给朋友",
    "建鸡舍", "查看鸡舍", "鸡蛋",
    "喂鸡", "摸摸鸡", "鸡取名", "可孵化鸡蛋",
}
# 命令参数只允许中文/字母/数字/下划线/空格，防止透出 shell 元字符。
# - 0-9A-Za-z：昵称、动物/小鸡的 hex id（如 chick.id）
# - _：内部 crop_id/recipe_id 本身带下划线（chinese_cabbage、sweet_potato 等），
#   8/10 凌晨事故就是这里漏放行、正则直接拒收（8/10 凌晨事故复盘）
# - 一-鿿：中文名/中文昵称
# - 空格：多参数指令（如「播种 南瓜 1」「喂鸡 abcdef」）
# 之前字符类里多出的一个孤立 " p" 是历史遗留笔误，已清理；没有任何调用方
# 依赖裸字母 p 单独放行。
_ARG_SAFE = re.compile(r"^[0-9A-Za-z_一-鿿 ]+$")
MAX_CMD_LEN = 80


def _moisture_advice(moisture: float) -> str:
    if moisture < 25:
        return "需要浇水"
    if moisture < 50:
        return "可以浇水"
    if moisture < 70:
        return "暂时不用浇"
    if moisture < 85:
        return "不要再浇"
    return "明确不应浇"


_STAGE_LABEL = {"seed": "刚播下", "sprout": "发芽了", "growing": "正在长大", "ready": "成熟啦"}


def _plot_payload(plot: dict, now: datetime, real_env: bool) -> dict:
    payload = {
        "plot_id": plot.get("plot_id"),
        "label": garden.plot_label(str(plot.get("plot_id") or "")),
        "crop_id": plot.get("crop_id"),
        "crop_name": None,
        "status": plot.get("status"),
        "stage": plot.get("stage"),
        "stage_label": None,
        "growth_points": plot.get("growth_points"),
        "moisture": None,
        "moisture_label": None,
        "advice": None,
        "watered_today": False,
        "ready_in_seconds": None,
        "base_days": None,
        "growth_note": None,
        "condition": None,
        "quality": None,
        "fertilize_count": None,
        "fertilize_max": None,
    }
    if plot.get("status") == "empty" or not plot.get("crop_id"):
        return payload
    crop = garden.garden_crops.CROPS.get(plot["crop_id"])
    if crop is None:
        return payload
    payload["crop_name"] = crop["name"]
    payload["base_days"] = crop["growth_days"]
    # 第七版阶段E：品质标记只在 growing/ready 有意义，与 home.py 的
    # _garden_plot_line 同一条规则（withered 没有收成，不额外标注）。
    if plot.get("quality") == "poor" and plot.get("status") in ("growing", "ready"):
        payload["quality"] = "poor"
    today = now.date().isoformat()
    payload["watered_today"] = (
        int(plot.get("watering_by_date", {}).get(today, 0)) > 0
        or today in plot.get("water_bonus_dates", [])
    )
    if plot.get("status") == "ready":
        payload["stage_label"] = "已经成熟，随时可以收获"
    elif plot.get("status") == "withered":
        payload["stage_label"] = "已经枯死，需要清理"
    else:
        payload["stage_label"] = _STAGE_LABEL.get(str(plot.get("stage") or ""), "正在长大")
        timing = plot.get("timing")
        if isinstance(timing, dict):
            remaining = timing.get("current", {}).get("remaining_seconds")
            if isinstance(remaining, (int, float)):
                payload["ready_in_seconds"] = int(remaining)
        note = None
        try:
            note = garden.growth_environment_note(plot, now)
        except Exception:
            note = None
        payload["growth_note"] = note
        # 第九版：追肥次数只在 growing 有意义（跟品质标记同一条规则）；
        # 上限也一并传给前端，不在 index.html 里硬编码 5。
        payload["fertilize_count"] = int(plot.get("fertilize_count", 0))
        payload["fertilize_max"] = garden.FERTILIZE_MAX_PER_CYCLE
    soil = plot.get("soil")
    if real_env and isinstance(soil, dict):
        moisture = float(soil.get("moisture", 0))
        payload["moisture"] = round(moisture, 1)
        payload["moisture_label"] = garden.garden_weather.moisture_label(moisture)
        payload["advice"] = _moisture_advice(moisture)
    condition = plot.get("condition")
    if isinstance(condition, dict) and condition.get("status") in {"active", "resolved"}:
        payload["condition"] = {
            "type": condition.get("type"),
            "status": condition.get("status"),
            "name": garden.CONDITION_LABELS.get(str(condition.get("type")), "异常"),
            "action": garden.CONDITION_ACTIONS.get(str(condition.get("type"))),
            "yield_penalty": int(plot.get("yield_penalty", 0)),
        }
    return payload


def _animal_payload(entry: dict, now: datetime, status: str) -> dict:
    bond_points = int(entry.get("bond_points", 0))
    bond_level = int(entry.get("bond_level", 0))
    next_level_points = None
    for level, points, _name in garden.BOND_LEVELS:
        if level == bond_level + 1:
            next_level_points = points
            break
    return {
        "id": entry.get("id"),
        "kind": entry.get("kind"),
        "name": garden.display_name(entry, include_species=True),
        "nickname": entry.get("nickname"),
        "species": entry.get("species"),
        "category": entry.get("category"),
        "personality": entry.get("personality"),
        "trait": entry.get("trait"),
        "spot": entry.get("spot"),
        "status": status,
        "residency": entry.get("residency"),
        "bond_points": bond_points,
        "bond_level": bond_level,
        "bond_level_name": garden.bond_level_name(bond_level),
        "bond_next_points": next_level_points,
        "last_note": entry.get("last_note"),
        "last_action": entry.get("last_action"),
        "preferred_action": entry.get("preferred_action"),
        "care_actions": list(garden.actions_for(str(entry.get("kind") or "animal")))
        if entry.get("kind") in {"animal", "flower"} else [],
        "away_at": entry.get("away_at"),
    }


def build_state() -> dict:
    now = datetime.now(garden.TZ)
    try:
        garden.advance(now)
    except Exception:
        pass  # 结算失败不阻塞只读展示
    snapshot = garden.crop_snapshot(now=now, acknowledge_conditions=True)
    real_env = bool(snapshot.get("real_environment_enabled"))
    plots = [_plot_payload(plot, now, real_env) for plot in snapshot["plots"]]
    animals = [_animal_payload(e, now, "active") for e in garden.active_entries()]
    away = [_animal_payload(e, now, "away") for e in garden.away_entries()]
    try:
        journal = garden.journal_snapshot(now=now)
    except Exception:
        journal = None
    try:
        observation = weather.latest_weather_observation(now=now)
    except Exception:
        observation = None
    scene = None
    try:
        meta = garden.load_meta()
        cache = [c for c in meta.get("scene_cache", []) if isinstance(c, dict) and c.get("text")]
        if cache:
            latest = max(cache, key=lambda c: str(c.get("generated_at") or ""))
            scene = {"text": latest["text"], "generated_at": latest.get("generated_at")}
    except Exception:
        scene = None
    inventory = snapshot.get("inventory") or {}
    recipes = {
        recipe_id: {
            "name": recipe["name"],
            "ingredients": recipe["ingredients"],
            "prepared_ingredients": recipe.get("prepared_ingredients", {}),
            "egg_range": recipe.get("egg_range"),
            # 第七版阶段D：可做性判断只有 garden._recipe_ingredients_available
            # 这一份实现（produce + produce_poor 合计），前端做菜面板直接读
            # 这个字段，不在 JS 里再重算一遍，避免两边口径分裂。
            "makeable": garden._recipe_ingredients_available(inventory, recipe),
        }
        for recipe_id, recipe in garden.garden_crops.RECIPES.items()
    }
    return {
        "type": "garden_web_state",
        "server_now": snapshot["server_now"],
        "season": snapshot.get("season"),
        "plots": plots,
        "animals": animals,
        "away": away,
        "inventory": snapshot.get("inventory"),
        "journal": journal,
        "weather": observation,
        "scene": scene,
        "crop_catalog": snapshot.get("crop_catalog"),
        "recipes": recipes,
        "coop": garden.coop_snapshot(now=now),
        "real_environment_enabled": real_env,
        # 第七版阶段 C：光景页排水按钮要靠 yard_water 档位条件渲染，之前
        # 这份快照没有把院级环境事实传给前端。
        "environment": snapshot.get("environment"),
    }


def run_command(cmd: str) -> dict:
    # 8/10 凌晨事故复盘：白名单/参数正则拒收时以前完全不落日志，前端报出
    # 「不认识的字符」但后台看不到到底是哪条 cmd 被拒——排查绕了一大圈才
    # 靠前端代码走查找到根因。这里连同正常执行结果一起打到 stdout，跟既
    # 有 `[garden-web] %s %s`（log_message）同一个前缀风格，方便日后直接
    # grep `[garden-web] cmd=`。
    parts = cmd.split()
    if not parts or parts[0] not in ALLOWED_COMMANDS:
        print(f"[garden-web] cmd={cmd!r} ok=False reason=whitelist", flush=True)
        return {"ok": False, "text": "这个指令不在小院子的白名单里哦"}
    if len(cmd) > MAX_CMD_LEN or any(not _ARG_SAFE.match(p) for p in parts[1:]):
        print(f"[garden-web] cmd={cmd!r} ok=False reason=arg_safe", flush=True)
        return {"ok": False, "text": "指令里出现了不认识的字符，换个说法试试？"}
    home = shutil.which("home")
    argv = [home, "院子", *parts] if home else [sys.executable, str(MINI_YARD_HOME / "home.py"), "院子", *parts]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=240,
        )
    except subprocess.TimeoutExpired:
        print(f"[garden-web] cmd={cmd!r} ok=False reason=timeout", flush=True)
        return {"ok": False, "text": "指令超时啦，稍后再试试"}
    ok = proc.returncode == 0
    text = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    print(f"[garden-web] cmd={cmd!r} ok={ok}", flush=True)
    return {"ok": ok, "text": text or "（指令执行完啦）"}


class Handler(BaseHTTPRequestHandler):
    server_version = "GardenWeb/1.0"

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json({"ok": True})
            return
        if path == "/api/state":
            try:
                self._send_json(build_state())
            except Exception as exc:  # 页面侧需要可读的错误提示
                self._send_json({"error": f"读取小院子失败：{exc}"}, status=500)
            return
        if path in {"/", "/index.html"}:
            body = (STATIC_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/assets/"):
            # 只放行 assets 目录下的 png，且不允许目录逃逸
            name = path[len("/assets/"):]
            if ".." in name or not name.endswith(".png"):
                self._send_json({"error": "not found"}, status=404)
                return
            target = (STATIC_DIR / "assets" / name).resolve()
            if not str(target).startswith(str((STATIC_DIR / "assets").resolve())) or not target.is_file():
                self._send_json({"error": "not found"}, status=404)
                return
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        if path != "/api/action":
            self._send_json({"error": "not found"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send_json({"ok": False, "text": "请求格式不对"}, status=400)
            return
        cmd = str(payload.get("cmd") or "").strip()
        if not cmd:
            self._send_json({"ok": False, "text": "空指令"}, status=400)
            return
        self._send_json(run_command(cmd))

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[garden-web] %s %s\n" % (self.address_string(), fmt % args))


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"小院子展示页已就绪：http://{HOST}:{PORT}/（公网入口由 nginx 校验主站登录）")
    print(f"数据源：{garden.GARDEN_FILE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
