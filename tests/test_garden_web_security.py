import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class GardenWebSecurityTests(unittest.TestCase):
    def test_home_entry_contains_no_key_in_source_or_url(self):
        # xiaoyu-web is not part of the garden open-source package
        pass

    def test_garden_browser_never_stores_or_sends_a_permanent_key(self):
        html = (ROOT / "garden-web/index.html").read_text(encoding="utf-8")
        self.assertIn('localStorage.removeItem("gardenWebKey")', html)
        self.assertNotIn('localStorage.setItem("gardenWebKey"', html)
        self.assertNotIn('"Authorization"', html)
        self.assertNotIn("Bearer ", html)
        self.assertIn('location.assign("/login")', html)

    def test_loopback_service_has_no_parallel_long_lived_token(self):
        server = (ROOT / "garden-web/server.py").read_text(encoding="utf-8")
        self.assertIn('HOST = os.environ.get("GARDEN_WEB_HOST", "127.0.0.1")', server)
        self.assertNotIn("def _auth_token", server)
        self.assertNotIn("def _authorized", server)
        readme = (ROOT / "garden-web/README.md").read_text(encoding="utf-8")
        self.assertIn("auth_request", readme)
        nginx = (ROOT / "garden-web/nginx-location.conf.example").read_text(
            encoding="utf-8",
        )
        self.assertIn("auth_request /_garden_auth", nginx)
        self.assertIn("error_page 401 403 =302 /login", nginx)

    def test_each_chicken_has_individual_safe_web_actions(self):
        html = (ROOT / "garden-web/index.html").read_text(encoding="utf-8")
        for marker in ("openChickenSheet(chick)", "喂鸡 ${chick.id}", "摸摸鸡 ${chick.id}", "鸡取名 ${chick.id}"):
            self.assertIn(marker, html)
        for field in ("chick.personality", "chick.chick_appearance", "chick.adult_appearance"):
            self.assertIn(field, html)
        server = (ROOT / "garden-web/server.py").read_text(encoding="utf-8")
        for command in ('"喂鸡"', '"摸摸鸡"', '"鸡取名"'):
            self.assertIn(command, server)

    def test_egg_story_precedes_coop_in_web_actions(self):
        html = (ROOT / "garden-web/index.html").read_text(encoding="utf-8")
        self.assertNotIn("搭一座鸡舍", html)
        self.assertIn('coop.story_status === "awaiting_choice"', html)
        self.assertIn('doAction("鸡蛋 孵化"', html)
        self.assertIn("选择孵化时才会搭起鸡舍", html)


class GardenWebActionErrorHandlingTests(unittest.TestCase):
    """8/9 事故：/api/action 失败时后端回的是 HTTP 200 + {ok:false,text:"…"}，
    前端 doAction 曾经完全不看 ok 字段，失败提示被套成功样式弹出。这里锁住
    修复后的三处：doAction 按 r.ok 分支、地块瓦片播种前的本地空地复核、
    页面重新可见时的即时刷新（弱化本地缓存过期窗口）。"""

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "garden-web/index.html").read_text(encoding="utf-8")

    def test_do_action_branches_on_backend_ok_field(self):
        self.assertIn("r.ok === false", self.html)
        self.assertRegex(self.html, r'showModal\(r\.text \|\| "执行失败[^"]*",\s*"err"\)')

    def test_plot_tile_sow_rechecks_cached_plot_status_before_opening(self):
        self.assertIn("cachedPlots.find(p => p.plot_id ===", self.html)
        self.assertIn('showModal("这块菜畦已经种上东西啦，挑一块空菜畦再来吧。", "err")', self.html)

    def test_visibilitychange_triggers_a_silent_refresh(self):
        self.assertIn('document.addEventListener("visibilitychange"', self.html)
        self.assertIn('if (document.visibilityState === "visible") refresh(true);', self.html)

    @unittest.skipUnless(shutil.which("node"), "node 不在 PATH 里，跳过 JS 语法检查")
    def test_inline_script_is_syntactically_valid(self):
        match = re.search(r"<script>(.*)</script>", self.html, re.S)
        self.assertIsNotNone(match, "index.html 里没找到内联 <script> 块")
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tmp:
            tmp.write(match.group(1))
            tmp_path = tmp.name
        try:
            result = subprocess.run(
                ["node", "--check", tmp_path],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            Path(tmp_path).unlink(missing_ok=True)


class GardenWebArgSafeRegexTests(unittest.TestCase):
    """8/10 凌晨事故：地块瓦片播种发的是内部 crop_id（chinese_cabbage/
    sweet_potato 等带下划线），_ARG_SAFE 当时没放行下划线，整条指令被
    「指令里出现了不认识的字符」拒收，用户播不进小白菜/红薯。修复分两层：
    正则放行下划线（本类覆盖）+ 前端瓦片播种改发中文名而不是 id（见
    GardenWebActionErrorHandlingTests 同文件里另一个类新增的静态断言）。
    这里直接从 server.py 源码里抠出正则表达式本体编译复测，覆盖『该放行
    的放行、该拒收的shell元字符仍然拒收』两头，不靠猜测的字符串常量。"""

    @classmethod
    def setUpClass(cls):
        source = (ROOT / "garden-web/server.py").read_text(encoding="utf-8")
        match = re.search(r'_ARG_SAFE = re\.compile\(r"(\^.*\$)"\)', source)
        assert match, "server.py 里没找到 _ARG_SAFE 的正则定义"
        cls.pattern = re.compile(match.group(1))

    def test_underscore_crop_and_recipe_ids_pass(self):
        for token in ("chinese_cabbage", "sweet_potato", "pumpkin_porridge", "cabbage_soup"):
            self.assertIsNotNone(self.pattern.match(token), token)

    def test_chinese_names_and_hex_ids_pass(self):
        for token in ("小白菜", "红薯", "南瓜", "a1b2c3", "1号菜畦", "12个蛋"):
            self.assertIsNotNone(self.pattern.match(token), token)

    def test_shell_metacharacters_still_rejected(self):
        for token in (";rm -rf", "a|b", "$(whoami)", "`id`", "../etc/passwd", '"quoted"', "'quoted'", "a&&b", "a>b"):
            self.assertIsNone(self.pattern.match(token), token)

    def test_lone_letter_p_typo_is_gone(self):
        # 旧正则字符类里混进了一个孤立的 " p"，是历史笔误而非有意为之的
        # 白名单条目；这里锁住它已被清理，防止将来又被误当"看起来存在
        # 就不要动"抄回去。字符类顺序是 [0-9A-Za-z_一-鿿 ]，裸的 "p" 不
        # 应该在结尾单独出现在空格之后。
        source = (ROOT / "garden-web/server.py").read_text(encoding="utf-8")
        self.assertIn('_ARG_SAFE = re.compile(r"^[0-9A-Za-z_一-鿿 ]+$")', source)


class GardenWebSowTileSendsCropNameTests(unittest.TestCase):
    """同一事故的前端半边：瓦片播种路径 openSowSheet 以前直接把
    chip.dataset.crop（内部 id）拼进 doAction 命令，跟篮子播种路径
    openSowPickPlot（一直发 cropName(cropId)）口径不一致。锁住两条路径
    现在都发中文名。"""

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "garden-web/index.html").read_text(encoding="utf-8")

    def test_plot_tile_sow_path_sends_crop_name_not_raw_id(self):
        self.assertIn("doAction(`播种 ${cropName(chip.dataset.crop)} ${plotNo}`", self.html)
        self.assertNotIn("doAction(`播种 ${chip.dataset.crop} ${plotNo}`", self.html)

    def test_basket_sow_path_still_sends_crop_name(self):
        self.assertIn("doAction(`播种 ${cropName(cropId)} ${chip.dataset.plot}`", self.html)


if __name__ == "__main__":
    unittest.main()
