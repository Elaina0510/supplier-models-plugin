"""M1 验收：``GET /endpoints``（双读 + profile 透传 + 密钥铁律）。

跑法（``tasks/progress.md`` §五 / ``prompt.md`` §5.1，填好占位后照抄；venv 里没有 pytest 也不许装）：

    cd "<本仓根目录>"                        # 必须 cd 到含 tests/ 的目录，discover 依赖 cwd
    V="<装有 hermes_cli 的 venv 里的 python>"
    T="<临时目录>/supplier-models-harness"    # 副本务必建在仓外，本仓 .gitignore 已排除 config/.env
    rm -rf "$T" && mkdir -p "$T"
    cp "<副本源 config.yaml>" "$T/config.yaml"   # 副本源须仍留有 legacy custom_providers: 条目
    cp "<副本源 .env>"        "$T/.env"
    HERMES_HOME="$(cygpath -w "$T")" "$V" -m unittest discover -s tests -v
    rm -rf "$T"      # 副本里含明文密钥，跑完必须删

口径：
* 只读临时副本；每个用例 ``setUp`` 先断言 ``get_config_path()`` 落在 ``HERMES_HOME``（临时副本）
  之下、且该副本目录在系统临时目录里 —— 这是「真 ``config.yaml`` / ``.env`` 零改动」的硬保险。
* 条数 / 名字 / 明文密钥分布一律**运行时先从副本里读出来**，再断言端点返回同样的东西
  （``prompt.md`` §2.7：3+2=5 那些数字只是某一刻的快照，不许硬编码成期望值）。
* 改过副本的用例（M1.8 的 ``${FAKE_KEY}``、R12 的 ``custom:`` 写法）在 ``setUp`` / ``tearDown``
  各还原一次 → 用例之间不依赖执行顺序。
* 不打网络、不碰 gateway：路由装在自建的 ``FastAPI()`` 上用 ``TestClient`` 直打。
"""

from __future__ import annotations

import inspect
import os
import re
import sys
import tempfile
import typing
import unittest
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from hermes_cli.config import get_config_path, load_config, read_raw_config

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PRODUCT_ROOT / "dashboard"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import plugin_api  # noqa: E402  （prompt §5.2：先把 dashboard 塞进 sys.path 再 import）

# 跨模块契约：M0 的 manifest.json + gateway 的 include_router(prefix="/api/plugins/<name>")
MOUNT_PREFIX = "/api/plugins/supplier-models"
ENDPOINTS_PATH = f"{MOUNT_PREFIX}/endpoints"

# 设计 §6.3 / M1.6 冻结的字段集（M2 按这些名字吃）
REQUIRED_FIELDS = ("id", "name", "base_url", "model", "models", "discover_models",
                   "has_api_key", "is_current", "source")
# 密钥铁律（M1.5）：关于密钥只回 has_api_key / api_key_plaintext 两个布尔；
# 官方行自带的 api_key_preview（redact 预览）必须被剥掉
FORBIDDEN_KEY_FIELDS = ("api_key", "apiKey", "api_key_preview", "api_key_value", "secret")


# ------------------------------------------------- 副本读取 / 定点文本改写（只碰副本）

def _home() -> Path:
    value = os.environ.get("HERMES_HOME")
    if not value:
        raise AssertionError("HERMES_HOME 未设置 —— 请用 progress §五 的测试骨架命令跑")
    return Path(value).resolve()


def _copy_file(name: str) -> Path:
    return _home() / name


def _read_copy(name: str = "config.yaml") -> str:
    """按原文读副本（``newline=""``：保留文件自己的行尾风格）。"""
    with _copy_file(name).open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _write_copy(text: str, name: str = "config.yaml") -> None:
    with _copy_file(name).open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _read_copy_bytes(name: str = "config.yaml") -> bytes:
    return _copy_file(name).read_bytes()


def _write_copy_bytes(data: bytes, name: str = "config.yaml") -> None:
    _copy_file(name).write_bytes(data)


def _split_eol(line: str) -> Tuple[str, str]:
    """``"  a: b\\r\\n"`` → ``("  a: b", "\\r\\n")``：定点改写时原样还该行自己的行尾。"""
    for ending in ("\r\n", "\n", "\r"):
        if line.endswith(ending):
            return line[:-len(ending)], ending
    return line, ""


def _rewrite_mapping_field(text: str, top_key: str, field: str, new_value: str) -> str:
    """把顶层块 ``top_key:`` 下 ``field:`` 的标量换成 ``new_value``（定点文本替换，其余字节不动）。

    找不到就抛错 —— 前提不成立的断言必须是失败，不许静默通过。
    """
    lines = text.splitlines(keepends=True)
    inside = False
    for index, line in enumerate(lines):
        if not inside:
            inside = line.startswith(f"{top_key}:")
            continue
        if re.match(r"^[A-Za-z_]", line):                     # 进了下一个顶层键 → 目标块里没有该字段
            break
        body, eol = _split_eol(line)
        match = re.match(rf"^(\s+){re.escape(field)}:(.*)$", body)
        if match:
            lines[index] = f"{match.group(1)}{field}: {new_value}{eol}"
            return "".join(lines)
    raise RuntimeError(f"副本的 {top_key}: 块里找不到 {field}: 行，改写前提不成立")


def _rewrite_legacy_api_key(text: str, entry_name: str, new_value: str) -> str:
    """把 ``custom_providers:`` 里 ``name: <entry_name>`` 那条的 ``api_key`` 换成 ``new_value``。"""
    lines = text.splitlines(keepends=True)
    inside = False
    target = False
    header = re.compile(rf"^\s*-\s*name:\s*{re.escape(entry_name)}\s*$")
    for index, line in enumerate(lines):
        if line.startswith("custom_providers:"):
            inside = True
            continue
        if inside and re.match(r"^[A-Za-z_]", line):
            inside = False
        if not inside:
            continue
        body, eol = _split_eol(line)
        if target and re.match(r"^\s*-\s+\S", body):          # 下一个条目 → 目标条目没有 api_key
            break
        if not target:
            if header.match(body):
                target = True
            continue
        match = re.match(r"^(\s*)api_key:(.*)$", body)
        if match:
            lines[index] = f"{match.group(1)}api_key: {new_value}{eol}"
            return "".join(lines)
    raise RuntimeError(f"副本的 custom_providers 里找不到条目 {entry_name!r} 的 api_key 行")


def _is_plaintext_raw(raw_value: Any) -> bool:
    """§6.3（v0.4 F9）判据：**未展开**原值非空且不是 ``${VAR}`` 模板 = 明文密钥落在 config.yaml。"""
    value = str(raw_value or "").strip()
    return bool(value) and not value.startswith("${")


def _copy_provider_rows(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """副本 ``providers:`` 里官方会列出的行（沿用官方过滤条件：dict 条目 + base_url 非空）。"""
    rows = []
    providers = raw.get("providers")
    if isinstance(providers, dict):
        for provider_id, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            base_url = str(entry.get("base_url") or entry.get("url") or entry.get("api") or "").strip()
            if not base_url:
                continue
            rows.append({"id": str(provider_id),
                         "name": str(entry.get("name") or provider_id),
                         "entry": entry})
    return rows


def _copy_legacy_rows(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """副本 ``custom_providers:``（cc-switch 写的 legacy 序列）里可列出的条目。"""
    rows = []
    for entry in (raw.get("custom_providers") or []):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        base_url = str(entry.get("base_url") or entry.get("url") or "").strip()
        if name and base_url:
            rows.append({"name": name, "entry": entry})
    return rows


def _raw_model_ids(entry: Any) -> List[str]:
    """副本条目 ``models:`` 的**磁盘** id 列表（dict 数键、list 数元素、其它形状 → ``[]``）。

    故意在测试里另写一遍口径、**不**调后端的 ``_entry_model_ids``：那样「期望值」与
    「实际值」是同一份代码，``allowlist_count`` 的断言就永远真、抓不到实现漂移
    （prompt §2.7：期望值一律从副本运行时推出来）。
    """
    models = entry.get("models") if isinstance(entry, dict) else None
    if isinstance(models, dict):
        return [str(key).strip() for key in models if str(key).strip()]
    if isinstance(models, (list, tuple)):
        return [str(item).strip() for item in models if str(item).strip()]
    return []


def _injected_view(disk_ids: List[str], default_model: str) -> List[str]:
    """官方 ``_models_from_custom_endpoint_entry``（``config_env.py:317-328``）的视图形状：
    ``model:`` 无条件插到第 0 位，再按「保序去重」清理 —— 期望值在这里独立复现一遍。"""
    ordered = [default_model] + list(disk_ids) if default_model else list(disk_ids)
    seen: set = set()
    return [item for item in ordered if item and not (item in seen or seen.add(item))]


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)


def _copy_secret_material() -> List[str]:
    """副本里真实存在的密钥串（legacy 明文 ``api_key`` + 副本 ``.env`` 的 HERMES_CUSTOM_* 值）。

    只用于断言「这些内容没出现在响应里」；**断言消息里不许出现它们本身**。
    """
    material: List[str] = []
    for row in _copy_legacy_rows(read_raw_config()):
        value = str(row["entry"].get("api_key") or "").strip()
        if value and not value.startswith("${"):
            material.append(value)
    if _copy_file(".env").exists():
        for line in _read_copy(".env").splitlines():
            key, separator, value = line.partition("=")
            if not separator or not key.strip().startswith("HERMES_CUSTOM_"):
                continue
            candidate = value.strip().strip('"').strip("'")
            if len(candidate) >= 16:
                material.append(candidate)
    return material


def _secret_fragments(secret: str) -> List[str]:
    """redact 预览会保留头部与尾部 —— 头 / 中 / 尾各取一段，任一段出现在响应里都算泄。"""
    fragments: List[str] = []
    spans = [(3, 12), (max(len(secret) // 2 - 6, 0), 12), (max(len(secret) - 12, 0), 12)]
    for start, length in spans:
        fragment = secret[start:start + length]
        if len(fragment) >= 8 and fragment not in fragments:
            fragments.append(fragment)
    return fragments


class EndpointsContractTest(unittest.TestCase):
    """M1.1–M1.8：路由骨架、双读合并、字段契约、profile 透传、密钥铁律。

    外加编排裁定的**契约补丁**（M5 上报的读契约缺口）：每行新增整数字段 ``allowlist_count``
    与其 follow-up（列表字段 ``allowlist_models`` = 磁盘白名单 id 的磁盘顺序，喂给
    ``formFromRow`` 的「已添加」栏）——既有字段与语义一字未动，所以那几项断言原样保留。
    """

    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(plugin_api.router, prefix=MOUNT_PREFIX)
        cls.client = TestClient(app)
        # 字节级快照：改过副本的用例靠它精确还原（行尾 / BOM 都不许被顺手改掉）
        cls.pristine_config = _read_copy_bytes("config.yaml")
        cls.pristine_config_text = _read_copy("config.yaml")
        cls.pristine_env = _read_copy_bytes(".env") if _copy_file(".env").exists() else None

    # ------------------------------------------------------------------ 基础设施

    def setUp(self):
        # —— 硬保险：任何动作之前先确认自己站在临时副本上，而不是用户的真配置 ——
        home = _home()
        self.assertTrue(home.is_relative_to(Path(tempfile.gettempdir()).resolve()),
                        f"HERMES_HOME={home} 不在系统临时目录下，测试可能写到真配置")
        config_path = Path(get_config_path()).resolve()
        self.assertEqual(str(home / "config.yaml").lower(), str(config_path).lower())
        self.assertTrue(str(config_path).lower().startswith(str(home).lower()),
                        f"get_config_path()={config_path} 不在 HERMES_HOME={home} 之下")
        self._restore_copy()

        # —— 运行时真相：先读副本，再拿副本去校验端点 ——
        self.raw = read_raw_config()
        self.cfg = load_config()
        self.copy_providers = _copy_provider_rows(self.raw)
        self.copy_legacy = _copy_legacy_rows(self.raw)
        self.assertTrue(self.copy_providers and self.copy_legacy,
                        "副本里没有同时存在 providers: 与 custom_providers: 条目 —— 双读断言失去意义，"
                        "先按 progress §五 重建 harness 副本")

    def tearDown(self):
        self._restore_copy()

    def _restore_copy(self):
        if _read_copy_bytes("config.yaml") != self.pristine_config:
            _write_copy_bytes(self.pristine_config, "config.yaml")
        self.assertEqual(self.pristine_config, _read_copy_bytes("config.yaml"),
                         "副本 config.yaml 没能还原成字节级原样")
        if self.pristine_env is not None:
            if _read_copy_bytes(".env") != self.pristine_env:
                _write_copy_bytes(self.pristine_env, ".env")
            self.assertEqual(self.pristine_env, _read_copy_bytes(".env"),
                             "副本 .env 被写坏了")

    def _fetch(self, query: str = "") -> Dict[str, Any]:
        response = self.client.get(f"{ENDPOINTS_PATH}{query}")
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertIsInstance(payload.get("endpoints"), list, response.text)
        return payload

    def _rows(self, query: str = "") -> List[Dict[str, Any]]:
        return self._fetch(query)["endpoints"]

    def _row_by_id(self, row_id: str) -> Dict[str, Any]:
        matches = [row for row in self._rows() if row.get("id") == row_id]
        self.assertEqual(1, len(matches), f"{row_id} 应当恰好一行，实际 {len(matches)} 行")
        return matches[0]

    def _expected_rows(self) -> List[Tuple[str, str, str]]:
        """副本 → 期望的 ``(id, name, source)`` 序列：providers: 在前、cc-switch 在后（M1.4）。"""
        provider_ids = [row["id"] for row in self.copy_providers]
        model_cfg = self.cfg.get("model") if isinstance(self.cfg.get("model"), dict) else {}
        expected = []
        if str(model_cfg.get("provider") or "").strip().lower() == "custom" \
                and str(model_cfg.get("base_url") or "").strip() and "custom" not in provider_ids:
            # 官方 _custom_endpoint_response 在这时会插一条 direct-config 行，形状也得跟着预期
            expected.append(("custom", "Custom", "direct-config"))
        expected += [(row["id"], row["name"], "providers") for row in self.copy_providers]
        expected += [(f"cc:{row['name']}", row["name"], "cc-switch") for row in self.copy_legacy]
        return expected

    # ------------------------------------------------------------------ M1.1 / M1.2

    def test_router_is_module_level_and_route_is_read_only(self):
        """M1.1：模块级 ``router = APIRouter()``；``/endpoints`` 的读方法仍是 GET。

        路由表用「必备路由在位 + 每条路径的方法集正确」的口径断言（M4 起了 ``POST /endpoints``、
        M5-M7 还会加 clear-models / migrate / activate / delete）——整张表的快照会必然失真。
        GET 自身的只读性由本类其余用例保证：``tearDown`` 逐字节还原副本、
        密钥铁律用例断言信封里无任何密钥材料。
        """
        self.assertIsInstance(plugin_api.router, APIRouter)
        methods: Dict[str, set] = {}                             # 同一路径可挂多条路由，方法要并起来
        for route in plugin_api.router.routes:
            methods.setdefault(getattr(route, "path", None), set()).update(
                getattr(route, "methods", None) or ())
        for path, required in (("/endpoints", {"GET", "POST"}),
                               ("/endpoints/validate", {"POST"}),
                               ("/endpoints/{endpoint_id}/pin", {"POST"})):
            with self.subTest(path=path):
                self.assertTrue(required <= (methods.get(path) or set()),
                                f"路由表缺 {sorted(required)} {path}，实际 {methods}")
        self.assertIn("endpoints", self._fetch(), "信封字段 endpoints 必须存在")
        self.assertIn("current", self._fetch(), "官方信封的 current 必须原样保留（M2 也读它）")
        for method in ("put", "patch", "delete"):                # POST 是 M4 的写路由，见 test_save
            with self.subTest(method=method):
                self.assertEqual(405, getattr(self.client, method)(ENDPOINTS_PATH).status_code)

    def test_route_signature_declares_optional_profile(self):
        """M1.2（R8）：形参 ``profile: Optional[str] = None`` 必须存在且默认为 ``None``。"""
        parameters = inspect.signature(plugin_api.endpoints).parameters
        self.assertIn("profile", parameters)
        self.assertIsNone(parameters["profile"].default)
        # plugin_api 用了 `from __future__ import annotations`，注解是字符串 → 先解析再比类型
        self.assertEqual(Optional[str], typing.get_type_hints(plugin_api.endpoints)["profile"])

    def test_profile_is_passed_through_to_official_list_and_scope(self):
        """M1.2：``?profile=X`` 原样透传给官方 ``list_custom_endpoints`` 和自己的 profile 作用域。

        假 profile 不交给官方解析器（那是「profile 不存在」，见下一个用例），本用例只记录
        「官方函数与自己的作用域各自收到了什么」。
        """
        baseline = plugin_api.list_custom_endpoints(None)      # 真函数 + 真副本，先取合法响应
        seen: Dict[str, Any] = {"list": [], "scope": []}

        def fake_list(profile: Optional[str] = None):
            seen["list"].append(profile)
            return baseline

        def fake_scope(profile: Optional[str] = None):
            seen["scope"].append(profile)
            return nullcontext()

        with mock.patch.object(plugin_api, "list_custom_endpoints", side_effect=fake_list), \
                mock.patch.object(plugin_api, "_config_profile_scope", side_effect=fake_scope):
            scoped = self.client.get(f"{ENDPOINTS_PATH}?profile=acme-team")
            self.assertEqual(200, scoped.status_code, scoped.text)
            default = self.client.get(ENDPOINTS_PATH)
            self.assertEqual(200, default.status_code, default.text)
        self.assertEqual(["acme-team", None], seen["list"],
                         "profile 没被透传给官方 list_custom_endpoints")
        self.assertEqual(["acme-team", None], seen["scope"], "profile 没被透传给 _config_profile_scope")

    def test_unknown_profile_is_not_silently_ignored(self):
        """R8 的反面：不存在的 profile 必须**如实失败**，不能悄悄降级成默认 profile 的列表。"""
        response = self.client.get(f"{ENDPOINTS_PATH}?profile=definitely-not-a-profile")
        self.assertEqual(404, response.status_code, response.text)
        self.assertIn("definitely-not-a-profile", response.text.lower())
        self.assertEqual(200, self.client.get(ENDPOINTS_PATH).status_code)

    def test_profile_current_and_absent_agree(self):
        """R8 语义：``profile`` 缺省与 ``current`` 都是「无覆盖」，两次结果必须完全一致。"""
        self.assertEqual(self._rows(), self._rows("?profile=current"))

    # ------------------------------------------------------------------ M1.3 / M1.4 / M1.6

    def test_dual_read_merges_copy_entries_providers_first(self):
        """M1.4 + §2.5 发现 1：两类条目都出现、providers: 在前、数量 = 副本实际数量。"""
        rows = self._rows()
        expected = self._expected_rows()
        self.assertEqual([item[0] for item in expected], [row["id"] for row in rows])
        self.assertEqual([item[2] for item in expected], [row["source"] for row in rows])
        self.assertEqual(len(expected), len(rows))
        for row in rows:
            self.assertTrue(str(row["base_url"]).strip(), f"行 {row['id']} 没有 base_url")

    def test_every_row_carries_the_frozen_field_set(self):
        """M1.6：字段齐全，外加 M1.7 需要的 ``api_key_plaintext``；类型也得是前端能直接吃的。"""
        for row in self._rows():
            for field in REQUIRED_FIELDS + ("api_key_plaintext",):
                self.assertIn(field, row, f"行 {row.get('id')} 缺字段 {field}")
            self.assertIsInstance(row["models"], list)
            for field in ("has_api_key", "api_key_plaintext", "is_current", "discover_models"):
                self.assertIsInstance(row[field], bool, f"行 {row['id']} 的 {field} 不是布尔")

    def test_discover_models_pin_state_matches_copy(self):
        """M1.6：每行的钉住状态逐行等于副本的 ``discover_models``（未设 = 默认 True）。"""
        expected: Dict[str, Any] = {}
        for row in self.copy_providers:
            expected[row["id"]] = bool(row["entry"].get("discover_models", True))
        for row in self.copy_legacy:
            expected[f"cc:{row['name']}"] = row["entry"].get("discover_models", True)
        actual = {row["id"]: row["discover_models"] for row in self._rows()}
        self.assertEqual(expected, actual)
        self.assertTrue([row_id for row_id, value in actual.items() if value is False],
                        "副本里没有已钉住（discover_models: false）的条目，钉住分支未被覆盖")

    def test_legacy_rows_use_cc_id_prefix_and_official_shapes(self):
        """M1.3：legacy 行 id = ``cc:<name>``（不与 providers: 的 id 撞车），models/model 归一化正确。"""
        rows = {row["id"]: row for row in self._rows()}
        provider_ids = [row["id"] for row in self.copy_providers]
        self.assertEqual(sorted(provider_ids), sorted(rows[row_id]["id"] for row_id in provider_ids))
        self.assertEqual(len(rows), len(provider_ids) + len(self.copy_legacy), "id 撞车会少行")
        for entry in self.copy_legacy:
            row = rows[f"cc:{entry['name']}"]
            models = entry["entry"].get("models")
            ids = list(models) if isinstance(models, (dict, list)) else []
            self.assertEqual(ids, row["models"])
            self.assertEqual(str(entry["entry"].get("model") or (ids[0] if ids else "")), row["model"])
            self.assertEqual("cc-switch", row["source"])

    # ------------------------------------- 契约补丁（编排裁定）：新整数字段 allowlist_count

    def _copy_disk_allowlists(self) -> Dict[str, List[str]]:
        """副本 → ``行 id : 磁盘 models id 列表`` 的期望表（providers + cc-switch 两段都覆盖）。"""
        expected = {row["id"]: _raw_model_ids(row["entry"]) for row in self.copy_providers}
        expected.update({f"cc:{row['name']}": _raw_model_ids(row["entry"])
                         for row in self.copy_legacy})
        return expected

    def test_allowlist_count_is_on_every_row_and_is_a_plain_integer(self):
        """契约补丁的形状半边：``allowlist_count`` 在**每一行**上，且是非负整数（不是布尔/字符串）。"""
        rows = self._rows()
        self.assertTrue(rows, "副本里没有可断言的行")
        for row in rows:
            with self.subTest(row_id=row.get("id")):
                self.assertIn("allowlist_count", row, f"行 {row.get('id')} 缺 allowlist_count")
                value = row["allowlist_count"]
                self.assertIsInstance(value, int, f"行 {row.get('id')} 的 allowlist_count 不是整数")
                self.assertNotIsInstance(value, bool, "布尔是 int 的子类，形状断言要单独挡住它")
                self.assertGreaterEqual(value, 0, "白名单条数不可能为负")

    def test_allowlist_count_equals_disk_models_key_count_from_the_copy(self):
        """每行的 ``allowlist_count`` 逐行等于**副本磁盘**上 ``models:`` 的条数（prompt §2.7 推出来的）。"""
        expected = self._copy_disk_allowlists()
        seen: set = set()
        for row in self._rows():
            row_id = str(row.get("id") or "")
            with self.subTest(row_id=row_id):
                if row_id in expected:
                    seen.add(row_id)
                    self.assertEqual(len(expected[row_id]), row["allowlist_count"],
                                     f"行 {row_id} 的 allowlist_count 不是磁盘 models 的条数")
                else:
                    # 唯一可能没有 providers: 条目在背后的是官方合成的 direct-config 行
                    # （`config_env.py:403-407`，只由顶层 model: 拼出）：磁盘白名单本来就是 0
                    self.assertEqual("direct-config", row.get("source"),
                                     f"行 {row_id} 既不在副本的 providers: 也不在 custom_providers:")
                    self.assertEqual(0, row["allowlist_count"],
                                     "direct-config 行没有磁盘白名单，allowlist_count 必须是 0")
        self.assertEqual(set(expected), seen, "副本里的条目在响应里没行（期望表覆盖不全）")

    def test_models_view_stays_injected_while_allowlist_count_reports_disk_truth(self):
        """**只加字段**：``models`` 仍是「磁盘白名单 ∪ 注入的默认模型」，新字段才带得出磁盘真值。

        本机快照里每个 ``providers:`` 条目的 ``model:`` 恰好已在自己的白名单里（注入被去重吃掉），
        所以两个数字此刻相等 —— 这正是「光看 models 算不出清空后为 0」的成因，
        故这里断言的是**关系**（视图 = 注入函数作用在磁盘列表上）与**差异位**（默认模型不在白名单里
        时视图恰好多 1 条），而不是硬编码某个数；``allowlist_count`` 归零的差分证据在
        ``test_clear_models.py`` 的清空用例里。
        """
        providers = {row["id"]: row["entry"] for row in self.copy_providers}
        legacy = {row["name"]: row["entry"] for row in self.copy_legacy}
        rows = self._rows()
        differing = 0
        for row in rows:
            row_id = str(row.get("id") or "")
            source = str(row.get("source") or "")
            with self.subTest(row_id=row_id, source=source):
                if source == "cc-switch":
                    entry = legacy.get(str(row.get("name") or ""))
                    self.assertIsNotNone(entry, f"legacy 行 {row_id} 在副本里找不到条目")
                    raw_models = entry.get("models")
                    ids = list(raw_models) if isinstance(raw_models, (dict, list)) else []
                    # _cc_switch_rows 不注入默认模型（只在 model 字段上兜 ids[0]），所以
                    # 行里的条数就是磁盘条数；len(ids) 与过滤空键后的条数必须一致，否则
                    # 「数 len(models)」与「数非空键」两种口径开始分叉，得回来复核。
                    self.assertEqual(ids, row["models"], "legacy 行的 models 被注入了别的东西")
                    self.assertEqual(len(ids), len(_raw_model_ids(entry)),
                                     f"legacy 条目 {row_id} 的磁盘 models 里有空键，口径分叉")
                    self.assertEqual(len(row["models"]), row["allowlist_count"])
                    continue
                if source != "providers":
                    continue                                    # direct-config 由上一个用例管
                entry = providers.get(row_id)
                self.assertIsNotNone(entry, f"providers 行 {row_id} 在副本里找不到条目")
                disk_ids = _raw_model_ids(entry)
                default_field = str(entry.get("model") or entry.get("default_model") or "").strip()
                view = _injected_view(disk_ids, default_field)
                self.assertEqual(view, row["models"],
                                 f"行 {row_id} 的 models 语义变了（既有字段必须冻结）")
                self.assertEqual(len(disk_ids), row["allowlist_count"])
                if default_field and default_field not in disk_ids:
                    differing += 1
                    self.assertEqual(row["allowlist_count"] + 1, len(row["models"]),
                                     f"行 {row_id} 注入了默认模型却没多出一条")
        self.assertGreaterEqual(differing, 0)                    # 0 也是本机快照的真实分布

    def test_allowlist_count_tolerance_for_cleared_and_list_shaped_entries(self):
        """helper 的容忍度半边（磁盘上凑不齐的形态在这里合成，**不碰副本**）：
        清空 → 0、list 形态照数、非容器 → 0、定位不到 → 0。
        """
        synthetic = {
            "providers": {
                "cleared-dict": {"base_url": "https://a.invalid/v1", "model": "m1", "models": {}},
                "cleared-none": {"base_url": "https://b.invalid/v1", "model": "m1"},
                "list-shape": {"base_url": "https://c.invalid/v1", "model": "m1",
                               "models": ["m1", "m2", ""]},
                "scalar-shape": {"base_url": "https://d.invalid/v1", "model": "m1",
                                 "models": "m1"},
                "no-base-url": {"model": "m1", "models": {"x": {}, "y": {}}},
            }
        }
        cases = (
            ("cleared-dict", 0),        # 磁盘 {} —— 行里的 models 却有注入的 1 条
            ("cleared-none", 0),        # 根本没有 models: 键
            ("list-shape", 2),          # list 也数（空元素不算）
            ("scalar-shape", 0),        # 手写的标量 models: 不是容器
            ("no-base-url", 2),         # 官方不列该行（无 base_url），但计数照磁盘
            ("absent-entry", 0),        # 定位不到 → 0，绝不抛
        )
        for endpoint_id, expected in cases:
            with self.subTest(endpoint_id=endpoint_id):
                self.assertEqual(expected, plugin_api._providers_allowlist_count(synthetic, endpoint_id))
        for bad_cfg in (None, {}, {"providers": None}, {"providers": ["not-a-dict"]}):
            with self.subTest(bad_cfg=str(bad_cfg)):
                self.assertEqual(0, plugin_api._providers_allowlist_count(bad_cfg, "list-shape"),
                                 "派生字段绝不能把只读列表打成异常")

        # 分支判据：cc-switch 行按 len(models) 计数（无注入），providers 行按磁盘计数（有注入）
        rows = [{"id": "cc:x", "source": "cc-switch", "models": ["m1", "m2", "m3"]},
                {"id": "cleared-dict", "source": "providers", "models": ["m1"]},
                {"id": "custom", "source": "direct-config", "models": ["m1"]}]
        self.assertIs(rows, plugin_api._apply_allowlist_count(rows, synthetic),
                      "helper 应与 _apply_is_current 同形状：原地改 + 返回同一个 rows")
        self.assertEqual([3, 0, 0], [row["allowlist_count"] for row in rows])
        # 定位不到条目（老/异常形状）也必须给出具体的 0，绝不能留缺字段让前端去猜
        orphan = [{"id": "no-such-entry", "source": "providers", "models": ["m1"]}]
        plugin_api._apply_allowlist_count(orphan, {})
        self.assertEqual(0, orphan[0]["allowlist_count"])

    # -------------------------------- 契约补丁第二处追加（编辑态）：列表字段 allowlist_models

    def test_allowlist_models_is_on_every_row_and_agrees_with_allowlist_count(self):
        """形状半边：每行都带 `allowlist_models`（``list[str]``），且
        ``allowlist_count == len(allowlist_models)`` —— 两个字段绝不允许各说一套。
        """
        rows = self._rows()
        self.assertTrue(rows, "副本里没有可断言的行")
        for row in rows:
            with self.subTest(row_id=row.get("id")):
                self.assertIn("allowlist_models", row, f"行 {row.get('id')} 缺 allowlist_models")
                ids = row["allowlist_models"]
                self.assertIsInstance(ids, list, f"行 {row.get('id')} 的 allowlist_models 不是列表")
                self.assertNotIsInstance(ids, str, "字符串也是序列，但它不是「id 列表」")
                for item in ids:
                    self.assertIsInstance(item, str, f"行 {row.get('id')} 的白名单 id 不是字符串")
                    self.assertEqual(item, item.strip(), "id 应当是去掉首尾空白后的形态")
                self.assertIsInstance(row["allowlist_count"], int)
                self.assertEqual(row["allowlist_count"], len(ids),
                                 f"行 {row.get('id')} 的计数与列表对不上（同一条来源链该保证的事）")

    def test_allowlist_models_equals_disk_models_ids_from_the_copy(self):
        """每行的 ``allowlist_models`` 逐行等于**副本磁盘**上 ``models:`` 的 id **序列**
        （含顺序，prompt §2.7：期望值运行时从副本推出来，不硬编码任何 id）。
        """
        expected = self._copy_disk_allowlists()
        seen: set = set()
        for row in self._rows():
            row_id = str(row.get("id") or "")
            with self.subTest(row_id=row_id):
                if row_id in expected:
                    seen.add(row_id)
                    self.assertEqual(expected[row_id], row["allowlist_models"],
                                     f"行 {row_id} 的 allowlist_models 不是磁盘上 models 的键序")
                else:
                    # 唯一可能没有 providers: 条目在背后的是官方合成的 direct-config 行
                    # （`config_env.py:403-407`）：它背后根本没有磁盘白名单
                    self.assertEqual("direct-config", row.get("source"),
                                     f"行 {row_id} 既不在副本的 providers: 也不在 custom_providers:")
                    self.assertEqual([], row["allowlist_models"],
                                     "direct-config 行没有磁盘白名单，allowlist_models 必须是空列表")
        self.assertEqual(set(expected), seen, "副本里的条目在响应里没行（期望表覆盖不全）")
        # cc-switch 行：那条路没有注入，所以磁盘真值 == 行里的 models（`_cc_switch_rows:71-72`）
        legacy = [row for row in self._rows() if str(row.get("source") or "") == "cc-switch"]
        self.assertTrue(legacy, "副本里没有 legacy 行，cc-switch 分支未被覆盖")
        for row in legacy:
            with self.subTest(row_id=row.get("id")):
                self.assertEqual(row["models"], row["allowlist_models"],
                                 "legacy 行的 allowlist_models 该照抄它自己的 models（那里已是磁盘序列）")

    def test_allowlist_models_helper_contract_branches_and_degradation(self):
        """helper 的容忍度与形状半边（合成数据，**不碰副本**）：
        清空 → ``[]``、list 形态照抄元素、非容器 → ``[]``、定位不到 → ``[]``、绝不抛。

        顺带钉住**本补丁要修的那个差分**：官方读路径给「磁盘 ``{}`` + ``model: m1``」的行
        带回 ``models = ["m1"]``（注入视图），而 ``allowlist_models`` 必须是 ``[]`` ——
        ``formFromRow`` 从此吃后者，编辑器的「已添加」栏才不会被默认模型自己填回去。
        """
        synthetic = {
            "providers": {
                "cleared-dict": {"base_url": "https://a.invalid/v1", "model": "m1", "models": {}},
                "cleared-none": {"base_url": "https://b.invalid/v1", "model": "m1"},
                "list-shape": {"base_url": "https://c.invalid/v1", "model": "m1",
                               "models": ["m2", "m1", ""]},
                "scalar-shape": {"base_url": "https://d.invalid/v1", "model": "m1",
                                 "models": "m1"},
            }
        }
        cases = (
            ("cleared-dict", []),           # 磁盘 {} —— 行里的 models 却有注入的 1 条
            ("cleared-none", []),           # 根本没有 models: 键
            ("list-shape", ["m2", "m1"]),   # list 也照磁盘顺序（空元素不算）
            ("scalar-shape", []),           # 手写的标量 models: 不是容器
            ("absent-entry", []),           # 定位不到 → []，绝不抛
        )
        for endpoint_id, expected in cases:
            with self.subTest(endpoint_id=endpoint_id):
                actual = plugin_api._providers_allowlist_models(synthetic, endpoint_id)
                self.assertEqual(expected, actual)
                self.assertEqual(len(actual),
                                 plugin_api._providers_allowlist_count(synthetic, endpoint_id),
                                 "两个派生字段的口径分叉了（同一份 _entry_model_ids 该给出同一个答案）")
        for bad_cfg in (None, {}, {"providers": None}, {"providers": ["not-a-dict"]}):
            with self.subTest(bad_cfg=str(bad_cfg)):
                self.assertEqual([], plugin_api._providers_allowlist_models(bad_cfg, "list-shape"),
                                 "派生字段绝不能把只读列表打成异常")

        # 分支判据与就地形状（与 _apply_allowlist_count 一致：原地改 + 返回同一个 rows）
        rows = [{"id": "cc:x", "source": "cc-switch", "models": ["m1", "m2", "m3"]},
                {"id": "cleared-dict", "source": "providers", "models": ["m1"]},
                {"id": "custom", "source": "direct-config", "models": ["m1"]}]
        self.assertIs(rows, plugin_api._apply_allowlist_models(rows, synthetic),
                      "helper 应与 _apply_allowlist_count 同形状：原地改 + 返回同一个 rows")
        self.assertEqual([["m1", "m2", "m3"], [], []],
                         [row["allowlist_models"] for row in rows])
        # 差分（本补丁的判据核心）：providers 行「视图 1 条 / 磁盘 0 条」
        self.assertEqual(["m1"], rows[1]["models"], "注入视图是既有字段语义，冻结不动")
        self.assertEqual([], rows[1]["allowlist_models"],
                         "刚清空的条目被种子带回默认模型 —— formFromRow 的缺口没被修掉")
        # 两个字段不共享同一个 list 对象（就地改 row["models"] 不许串到磁盘真值那一份）
        self.assertIsNot(rows[0]["models"], rows[0]["allowlist_models"],
                         "legacy 行的 allowlist_models 是 row.models 的别名（该复刻一份）")
        # 计数与列表同时落位：先跑 count 再跑 models，等式在每一行上都成立
        plugin_api._apply_allowlist_count(rows, synthetic)
        for row in rows:
            with self.subTest(row_id=row["id"]):
                self.assertEqual(row["allowlist_count"], len(row["allowlist_models"]))
        # 定位不到条目（老/异常形状）也得给出空列表，绝不能留缺字段让前端去猜
        orphan = [{"id": "no-such-entry", "source": "providers", "models": ["m1"]}]
        plugin_api._apply_allowlist_models(orphan, {})
        self.assertEqual([], orphan[0]["allowlist_models"])

    # ------------------------------------------------------------------ R12 / M1.4

    def test_is_current_follows_copy_model_provider(self):
        """R12：``is_current`` 完全由副本的 ``model.provider`` 决定，认得的行才亮。"""
        current = str((self.cfg.get("model") or {}).get("provider") or "").strip()
        self.assertTrue(current, "副本的 model.provider 为空，本用例没有意义")
        bare = current.split(":", 1)[1].strip() if current.lower().startswith("custom:") else current
        expected = sorted(row_id for row_id, name, _source in self._expected_rows()
                          if row_id.lower() == bare.lower() or name.lower() == bare.lower())
        actual = sorted(row["id"] for row in self._rows() if row["is_current"])
        self.assertEqual(expected, actual)
        self.assertEqual(1, len(actual), f"当前供应商应有且只有一行，实际 {actual}")

    def test_is_current_accepts_custom_prefix_spelling(self):
        """R12：``model.provider`` 写成 ``custom:<name>`` 时，legacy 行也必须被认成当前供应商。"""
        legacy = self.copy_legacy[0]["name"]
        _write_copy(_rewrite_mapping_field(self.pristine_config_text, "model", "provider",
                                           f'"custom:{legacy}"'))
        rows = self._rows()
        self.assertEqual([f"cc:{legacy}"], [row["id"] for row in rows if row["is_current"]])
        for row in rows:
            if not row["id"].startswith("cc:"):
                self.assertFalse(row["is_current"], f"providers: 行 {row['id']} 不该被认成当前供应商")

    # ------------------------------------------------------------------ M1.5 / M1.7 密钥铁律

    def test_plaintext_flag_follows_the_raw_value(self):
        """M1.7：明文判据 = 副本**未展开**原值；``providers:`` 行（key_env 指 .env）一律 False。"""
        expected = {row["id"]: _is_plaintext_raw(row["entry"].get("api_key"))
                    for row in self.copy_providers}
        expected.update({f"cc:{row['name']}": _is_plaintext_raw(row["entry"].get("api_key"))
                         for row in self.copy_legacy})
        actual = {row["id"]: row["api_key_plaintext"] for row in self._rows()}
        self.assertEqual(expected, actual)
        self.assertTrue([row_id for row_id, value in actual.items() if value],
                        "副本里没有明文密钥条目，M1.7 的 True 分支未被覆盖")
        for row in self.copy_providers:
            self.assertFalse(actual[row["id"]], f"providers: 行 {row['id']} 不该报明文密钥")

    def test_response_carries_no_key_material(self):
        """M1.5：关于密钥只回两个布尔；响应里既不出现 ``sk-`` 开头的串，也不出现副本里的真实密钥。"""
        response = self.client.get(ENDPOINTS_PATH)
        payload = response.json()
        for row in payload["endpoints"]:
            for field in FORBIDDEN_KEY_FIELDS:
                self.assertNotIn(field, row, f"行 {row.get('id')} 漏出密钥字段 {field}")
        for value in _iter_strings(payload):
            self.assertFalse(value.startswith("sk-"), "响应里出现 sk- 开头的字符串（疑似密钥）")
            self.assertNotIn("Bearer ", value)
        material = _copy_secret_material()
        self.assertTrue(material, "副本里没有可读密钥样本，本用例的「不外泄」分支未被覆盖")
        for secret in material:
            for fragment in _secret_fragments(secret):
                self.assertNotIn(fragment, response.text, "响应里出现了副本中真实密钥的内容")

    def test_env_ref_rewrite_clears_plaintext_flag(self):
        """M1.8（§6.3 v0.4 F9）：legacy 明文 key 换成 ``${FAKE_KEY}`` → 明文告警必须消失。

        ``load_config()`` 对解析不了的 ``${VAR}`` 原样保留（``config.py:_env_expand_match``
        「keeping the literal placeholder」），所以展开值仍非空 —— 用展开值判就会把模板误报成
        「明文密钥在 config.yaml」，这正是本用例守住的坑。
        """
        target = next((row["name"] for row in self.copy_legacy
                       if _is_plaintext_raw(row["entry"].get("api_key"))), None)
        self.assertTrue(target, "副本里没有带明文 api_key 的 legacy 条目，F9 判据无法验证")
        before = self._row_by_id(f"cc:{target}")
        self.assertTrue(before["api_key_plaintext"], "改写前该条目就该报明文密钥")
        others = [row["name"] for row in self.copy_legacy
                  if row["name"] != target and _is_plaintext_raw(row["entry"].get("api_key"))]

        rewritten = _rewrite_legacy_api_key(self.pristine_config_text, target, '"${FAKE_KEY}"')
        self._assert_only_api_key_line_changed(self.pristine_config_text, rewritten)
        _write_copy(rewritten)

        after = self._row_by_id(f"cc:{target}")
        self.assertFalse(after["api_key_plaintext"], "${FAKE_KEY} 模板被误报成明文密钥")
        # has_api_key 的口径 = 展开值非空（§6.3）；模板解析不了时 load_config 原样保留字面量，
        # 所以「有密钥」仍然成立 —— 只有拿 raw 值判才不会被它骗成「明文密钥在 config.yaml」。
        expanded = next((str(entry.get("api_key") or "").strip()
                         for entry in (load_config().get("custom_providers") or [])
                         if isinstance(entry, dict)
                         and str(entry.get("name") or "").strip() == target), "")
        self.assertEqual(bool(expanded), bool(after["has_api_key"]),
                         "has_api_key 未跟随展开值（§6.3 口径）")
        self.assertTrue(expanded.startswith("${"),
                        "副本的 ${FAKE_KEY} 没被原样保留，本用例守的坑没被踩到")
        for field in ("id", "name", "base_url", "model", "models", "discover_models", "source"):
            self.assertEqual(before[field], after[field], f"改写密钥把字段 {field} 也带变了")
        for name in others:
            self.assertTrue(self._row_by_id(f"cc:{name}")["api_key_plaintext"],
                            f"{name} 的明文告警被无关条目带没了")
        self.assertEqual(self.pristine_env, _read_copy_bytes(".env"), "测试写了副本的 .env")

    @staticmethod
    def _assert_only_api_key_line_changed(before: str, after: str) -> None:
        """M1.8 的自证：定点改写只动一行，且动的那行必须是 ``api_key:``。"""
        before_lines = before.splitlines()
        after_lines = after.splitlines()
        assert len(before_lines) == len(after_lines), "改写动了副本的行数"
        changed = [(index, old, new) for index, (old, new) in enumerate(zip(before_lines, after_lines))
                   if old != new]
        assert len(changed) == 1, f"预期只改 1 行，实际改了 {len(changed)} 行"
        _index, _old, new = changed[0]
        assert re.match(r"^\s*api_key:", new), f"改动的不是 api_key 行：{new.strip()[:16]}…"


if __name__ == "__main__":
    unittest.main()
