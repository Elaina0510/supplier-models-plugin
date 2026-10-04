"""M3 验收：``POST /endpoints/validate``（探测 + 编辑态密钥回退，决策 14 / R13 / T25 后端半边）。

跑法（``tasks/progress.md`` §五 / ``prompt.md`` §5.1，逐字照抄；venv 里没有 pytest 也不许装）：

    cd "H:/application/hermesnew/home/plugins/supplier-models"
    V="H:/application/hermesnew/hermes-agent/venv/Scripts/python.exe"
    T="$LOCALAPPDATA/Temp/providerchange-harness"
    rm -rf "$T" && mkdir -p "$T"
    cp H:/application/hermesnew/home/config.yaml "$T/config.yaml"
    cp H:/application/hermesnew/home/.env         "$T/.env"
    HERMES_HOME="$(cygpath -w "$T")" "$V" -m unittest discover -s tests -v
    rm -rf "$T"      # 副本里含明文密钥，跑完必须删

口径：
* **零真网络**：官方探测的 httpx 客户端由 ``_endpoint_probe_client``（``config_env.py:647``）
  造，本文件把它整体换成 ``httpx.MockTransport`` —— 请求对象是真的（URL / 头都由官方代码
  装配），只是永远出不去。断言「捕获到 N 条请求」本身就是「没有真网络」的证据。
* **零真密钥**：所有回退用的密钥都是本文件写进**副本**的 ``sk-FAKE-TEST-*`` 假值，
  用例结束按字节还原副本；真实副本里的密钥只被读来判「有没有 key_env / 是否明文」，
  其内容既不进断言消息也不进期望值。
* 只读/只写临时副本：``setUp`` 先断言 ``get_config_path()`` 落在 ``HERMES_HOME``（副本）
  之下、且该目录在系统临时目录里 —— 这是「真 ``config.yaml`` / ``.env`` 零改动」的硬保险。
  M3 本身**不写任何配置**（F10），所以另有一条用例逐字节证明写了请求之后副本没变。
* 条目名 / id / 密钥分布一律**运行时先从副本里读出来**，不硬编码（``prompt.md`` §2.7）。
"""

from __future__ import annotations

import httpx
import inspect
import logging
import os
import re
import sys
import tempfile
import typing
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from hermes_cli.config import get_config_path, invalidate_env_cache, read_raw_config
from hermes_cli.web_models import CustomEndpointUpdate
from hermes_cli.web_routers import config_env

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PRODUCT_ROOT / "dashboard"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import plugin_api  # noqa: E402  （prompt §5.2：先把 dashboard 塞进 sys.path 再 import）

MOUNT_PREFIX = "/api/plugins/supplier-models"
VALIDATE_PATH = f"{MOUNT_PREFIX}/endpoints/validate"

# 假密钥前缀：本文件里出现的「密钥」全是这个形状，真密钥一个不进断言
FAKE_KEY_PREFIX = "sk-FAKE-TEST-"
# 探测用的假端点：``.invalid`` 永不被 DNS（传输层已被 MockTransport 换掉）
PROBE_BASE_URL = "http://probe.invalid/v1"
PROBE_MODELS_URL = f"{PROBE_BASE_URL}/models"
MISSING_ENTRY_ID = "zz-m3-no-such-endpoint"

# validate 的响应契约（M3 定义，M4/M7 只读）：官方四字段 + 派生 error_kind
RESPONSE_FIELDS = ("ok", "reachable", "message", "models", "error_kind")


# ------------------------------------------------------------- 副本读取 / 定点改写

def _home() -> Path:
    value = os.environ.get("HERMES_HOME")
    if not value:
        raise AssertionError("HERMES_HOME 未设置 —— 请用 progress §五 的测试骨架命令跑")
    return Path(value).resolve()


def _read_copy_bytes(name: str = "config.yaml") -> bytes:
    return (_home() / name).read_bytes()


def _write_copy_bytes(data: bytes, name: str = "config.yaml") -> None:
    (_home() / name).write_bytes(data)


def _split_eol(line: str) -> Tuple[str, str]:
    for ending in ("\r\n", "\n", "\r"):
        if line.endswith(ending):
            return line[:-len(ending)], ending
    return line, ""


def _rewrite_legacy_api_key(text: str, entry_name: str, new_value: str) -> str:
    """把副本 ``custom_providers:`` 里 ``name: <entry_name>`` 那条的 ``api_key`` 换成假值。"""
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
        if target and re.match(r"^\s*-\s+\S", body):
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


def _set_env_value(text: str, name: str, value: str) -> str:
    """把副本 ``.env`` 里的 ``NAME=`` 换成 ``value``（没有这一行就追加）。"""
    lines = text.splitlines(keepends=True)
    pattern = re.compile(rf"^\s*{re.escape(name)}=")
    for index, line in enumerate(lines):
        body, eol = _split_eol(line)
        if pattern.match(body):
            lines[index] = f"{name}={value}{eol}"
            return "".join(lines)
    joiner = "" if (not lines or lines[-1].endswith(("\n", "\r"))) else "\n"
    return text + f"{joiner}{name}={value}\n"


def _raw_providers(raw: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    providers = raw.get("providers")
    return {str(key): entry for key, entry in providers.items() if isinstance(entry, dict)} \
        if isinstance(providers, dict) else {}


def _raw_legacy(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [entry for entry in (raw.get("custom_providers") or []) if isinstance(entry, dict)]


def _is_plaintext(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and not text.startswith("${")


def _fake_key(tag: str) -> str:
    """足够长（redact 预览的头/中/尾三段取值都盖得住）的假密钥。"""
    return f"{FAKE_KEY_PREFIX}{tag}-" + "0123456789abcdef" * 2


def _secret_fragments(secret: str) -> List[str]:
    """假密钥的头 / 中 / 尾三段：任一段出现在响应或日志里都算泄。"""
    fragments: List[str] = []
    spans = [(3, 12), (max(len(secret) // 2 - 6, 0), 12), (max(len(secret) - 12, 0), 12)]
    for start, length in spans:
        fragment = secret[start:start + length]
        if len(fragment) >= 8 and fragment not in fragments:
            fragments.append(fragment)
    return fragments


def _payload(endpoint_id: str = "", base_url: str = PROBE_BASE_URL,
             name: str = "M3 探测用例", model: str = "fake/model-a",
             api_key: Optional[str] = None) -> Dict[str, Any]:
    """官方 ``CustomEndpointUpdate`` 的最小必填形状（``web_models.py:36``）。

    ``api_key=None`` → **整个字段不发**（前端铁律 ``form.apiKey.trim() || undefined`` 在
    JSON 层的等价写法）；发 ``""`` 是另一条语义，另有用例覆盖。
    """
    body: Dict[str, Any] = {"base_url": base_url, "id": endpoint_id, "model": model, "name": name}
    if api_key is not None:
        body["api_key"] = api_key
    return body


class ValidateRouteTest(unittest.TestCase):
    """M3.1 / M3.2 / M3.9 / M3.10 / M3.11：路由形状、密钥回退、不外泄、失败分类。"""

    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(plugin_api.router, prefix=MOUNT_PREFIX)
        cls.client = TestClient(app)
        cls.pristine_config = _read_copy_bytes("config.yaml")
        cls.pristine_config_text = cls.pristine_config.decode("utf-8")
        env_path = _home() / ".env"
        cls.env_exists = env_path.exists()
        cls.pristine_env = env_path.read_bytes() if cls.env_exists else None

    # ------------------------------------------------------------------ 基础设施

    def setUp(self):
        home = _home()
        # —— 硬保险（prompt §5.2）：本文件会写副本，写之前必须先确认站在副本上 ——
        self.assertTrue(home.is_relative_to(Path(tempfile.gettempdir()).resolve()),
                        f"HERMES_HOME={home} 不在系统临时目录下，测试可能写到真配置")
        config_path = Path(get_config_path()).resolve()
        self.assertTrue(str(config_path).lower().startswith(str(home).lower()),
                        f"get_config_path()={config_path} 不在 HERMES_HOME={home} 之下")
        self.assertEqual(str(home / "config.yaml").lower(), str(config_path).lower())
        self.assertTrue(self.env_exists, "副本里没有 .env —— §5.1 的 harness 命令要求一并复制 .env")
        self._restore_copy()

        raw = read_raw_config()
        self.raw = raw
        self.providers = _raw_providers(raw)
        self.legacy = _raw_legacy(raw)
        self.assertTrue(self.providers and self.legacy,
                        "副本里没有同时存在 providers: 与 custom_providers: 条目 —— "
                        "回退断言失去意义，先按 progress §五 重建 harness 副本")
        self._scoped_calls: List[str] = []

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
                invalidate_env_cache()
            self.assertEqual(self.pristine_env, _read_copy_bytes(".env"), "副本 .env 被写坏了")

    # ------------------------------------------------------- 假探测（真请求、零网络）

    @contextmanager
    def _fake_probe(self, models: Any = ("fake/model-a", "fake/model-b"),
                    status: int = 200, error: Optional[Exception] = None):
        """把官方探测客户端换成 MockTransport：请求真装配、响应真解析、网络零发生。"""
        seen: Dict[str, Any] = {"requests": [], "timeouts": [], "urls": []}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["requests"].append(request)
            seen["urls"].append(str(request.url))
            if error is not None:
                raise error
            if isinstance(models, list) and models and isinstance(models[0], str):
                body: Any = {"data": [{"id": mid} for mid in models]}
            else:
                body = {"data": list(models or [])}
            return httpx.Response(status, json=body)

        def factory(url: str, timeout: float):
            seen["timeouts"].append(timeout)
            return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     timeout=httpx.Timeout(timeout))

        with mock.patch.object(config_env, "_endpoint_probe_client", new=factory):
            yield seen

    @contextmanager
    def _captured_logs(self):
        """挂一个 root handler 收所有日志文本（T25：日志里不许出现密钥材料）。"""
        records: List[str] = []

        class Collector(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                try:
                    records.append(f"{record.levelname}:{self.format(record)}")
                except Exception:                       # 格式化本身失败也不许影响用例
                    records.append(str(record.msg))

        handler = Collector()
        handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            yield records
        finally:
            root.setLevel(previous)
            root.removeHandler(handler)

    # ------------------------------------------------------------------ 副本取材

    def _providers_entry_with_key_env(self) -> Tuple[str, Dict[str, Any]]:
        """副本里 ``providers:`` 中带 ``key_env`` 且磁盘上不是明文 api_key 的条目（M3.10 取材）。"""
        matches = [(pid, entry) for pid, entry in self.providers.items()
                   if str(entry.get("key_env") or "").strip() and not _is_plaintext(entry.get("api_key"))]
        self.assertTrue(matches, "副本里没有带 key_env 的 providers: 条目，回退用例无从验证")
        return matches[0]

    def _legacy_entry_with_plaintext(self) -> Tuple[str, Dict[str, Any]]:
        """副本里 ``custom_providers:`` 中带明文 api_key 的条目（M3.2 legacy 分支取材）。"""
        matches = [(str(entry.get("name") or "").strip(), entry) for entry in self.legacy
                   if _is_plaintext(entry.get("api_key"))]
        self.assertTrue(matches, "副本里没有带明文 api_key 的 legacy 条目，legacy 回退无从验证")
        return matches[0]

    def _seed_env_key(self, env_name: str, fake: str) -> None:
        """把假密钥写进**副本**的 ``.env``，并确认 ``_scoped_key_env`` 真能读到它。"""
        env_text = _read_copy_bytes(".env").decode("utf-8") if self.env_exists else ""
        _write_copy_bytes(_set_env_value(env_text, env_name, fake).encode("utf-8"), ".env")
        invalidate_env_cache()
        self.assertEqual(fake, plugin_api._scoped_key_env(env_name),
                         "假密钥没能经 _scoped_key_env 读回 —— 本用例走的是官方读取链，"
                         "读不回来就说明前提（非 multiplex / .env 生效）不成立，不许当通过")

    def _seed_legacy_key(self, entry_name: str, fake: str) -> None:
        text = _rewrite_legacy_api_key(self.pristine_config_text, entry_name, fake)
        _write_copy_bytes(text.encode("utf-8"), "config.yaml")
        self.assertTrue(_is_plaintext(next(
            (e.get("api_key") for e in _raw_legacy(read_raw_config())
             if str(e.get("name") or "").strip() == entry_name), None)),
            "假明文密钥没写进副本，legacy 回退前提不成立")

    # --------------------------------------------------------------- M3.1 路由形状

    def test_route_is_post_and_takes_no_profile(self):
        """M3.1：``POST /endpoints/validate`` 存在；官方不吃 profile，本路由形参也只有 ``body``。"""
        methods: Dict[str, set] = {}                # 同一路径可挂多条路由（M4 在 /endpoints 上加了 POST）
        for route in plugin_api.router.routes:
            methods.setdefault(getattr(route, "path", None), set()).update(
                getattr(route, "methods", None) or ())
        self.assertEqual({"POST"}, methods.get("/endpoints/validate"),
                         f"validate 路由没挂上或方法不对：{methods}")
        # 「整张表只有 M1+M3 两条」是快照式断言，M4（save / pin）与 M5-M7 必然把它撞失效 ——
        # 只断言本模块依赖的路径在位，多余路由交给各自模块的契约用例
        for path in ("/endpoints", "/endpoints/validate"):
            with self.subTest(path=path):
                self.assertIn(path, methods, f"M1/M3 的必备路径消失了，整表：{methods}")
        parameters = inspect.signature(plugin_api.validate_endpoint).parameters
        self.assertEqual(["body"], list(parameters), "validate 不该声明 profile 形参（§6.3 F5 唯一例外）")
        hints = typing.get_type_hints(plugin_api.validate_endpoint)
        self.assertIs(CustomEndpointUpdate, hints["body"],
                      "请求体必须是官方 CustomEndpointUpdate，字段名不许自造")
        with self._fake_probe():
            self.assertEqual(405, self.client.get(VALIDATE_PATH).status_code)

    def test_shared_helper_signature_is_frozen(self):
        """progress §四 / prompt §5.6：``_with_stored_key`` 归 M3，名字与签名对 M4+ 冻结。"""
        self.assertTrue(callable(getattr(plugin_api, "_with_stored_key", None)),
                        "M3 必须引入共享 helper _with_stored_key")
        self.assertEqual(["body"], list(inspect.signature(plugin_api._with_stored_key).parameters))
        for other in ("_cc_switch_rows", "_raw_key_is_plaintext", "CC_ID_PREFIX", "KEY_MATERIAL_KEYS"):
            self.assertTrue(hasattr(plugin_api, other), f"M1 的共享符号 {other} 不该被 M3 挪走")

    def test_missing_required_fields_rejected_by_official_model(self):
        """官方请求模型是 ``name``/``base_url``/``model`` 必填 —— 前端 payload 必须带上（防 422）。"""
        with self._fake_probe() as seen:
            response = self.client.post(VALIDATE_PATH, json={"base_url": PROBE_BASE_URL})
        self.assertEqual(422, response.status_code, response.text)
        self.assertEqual([], seen["requests"], "422 之前就该被拦，不该发出探测")

    # --------------------------------------------------------- M3.4 探测本身（零网络）

    def test_probe_uses_official_models_url_and_8s_timeout(self):
        """§4 性能行：URL 是 ``base_url + /models``，超时沿用官方 8.0s（插件不加更短的）。"""
        with self._fake_probe(models=["fake/model-a", "fake/model-b", "fake/model-a"]) as seen:
            payload = self.client.post(VALIDATE_PATH, json=_payload("")).json()
        self.assertEqual(1, len(seen["requests"]))
        self.assertEqual(PROBE_MODELS_URL, seen["urls"][0])
        self.assertEqual([8.0], seen["timeouts"], "探测超时被改短了会盖掉官方 8s 口径")
        self.assertEqual({"ok": True, "reachable": True, "message": "",
                          "models": ["fake/model-a", "fake/model-b"], "error_kind": "ok"}, payload,
                         f"探测成功时的响应形状不对：{payload}")

    def test_user_supplied_key_is_used_and_never_echoed(self):
        """前端填了 Key 时：用前端填的（不被已存值覆盖），且不回显。"""
        fake = _fake_key("typed")
        with self._fake_probe() as seen:
            response = self.client.post(VALIDATE_PATH, json=_payload("amd", api_key=fake))
        request = seen["requests"][0]
        self.assertEqual(f"Bearer {fake}", request.headers.get("authorization"))
        self.assertNotIn(FAKE_KEY_PREFIX, response.text, "响应里出现了密钥内容")
        for field in RESPONSE_FIELDS:
            self.assertIn(field, response.json())

    def test_empty_api_key_string_still_falls_back_for_existing_entry(self):
        """M3.2 的「``body.api_key`` 为空」含空串：条目存在 → 照样回退（前端铁律是不发 ``""``）。"""
        endpoint_id, entry = self._providers_entry_with_key_env()
        fake = _fake_key("empty-string")
        self._seed_env_key(str(entry["key_env"]).strip(), fake)
        with self._fake_probe() as seen:
            self.client.post(VALIDATE_PATH, json=_payload(endpoint_id, api_key=""))
        self.assertEqual(f"Bearer {fake}", seen["requests"][0].headers.get("authorization"))

    # ------------------------------------------------------------- M3.10 / T25 回退

    def test_fallback_for_providers_entry_sends_authorization(self):
        """M3.10：``providers:`` 条目有 key_env + 请求不带 api_key → **发出的请求头带鉴权**。"""
        endpoint_id, entry = self._providers_entry_with_key_env()
        env_name = str(entry["key_env"]).strip()
        fake = _fake_key("providers")
        self._seed_env_key(env_name, fake)

        with self._fake_probe() as seen, self._captured_logs() as logs:
            response = self.client.post(VALIDATE_PATH, json=_payload(endpoint_id))

        request = seen["requests"][0]
        self.assertEqual(f"Bearer {fake}", request.headers.get("authorization"),
                         "密钥回退没生效：官方拿不到 key 就不挂 Authorization（config_env.py:629-631）")
        # 不回显 / 不回传前端 / 不进日志
        self.assertNotIn("api_key", response.json(), "响应里不该出现 api_key 字段")
        self.assertNotIn(FAKE_KEY_PREFIX, response.text, "响应里出现了密钥内容")
        joined = "\n".join(logs)
        self.assertNotIn(FAKE_KEY_PREFIX, joined, "日志里出现了密钥内容")
        for fragment in _secret_fragments(fake):
            self.assertNotIn(fragment, response.text, "响应里出现了密钥片段")
            self.assertNotIn(fragment, joined, "日志里出现了密钥片段")

    def test_fallback_for_legacy_entry_sends_authorization(self):
        """M3.2 的 legacy 分支：``cc:<name>`` → 取 ``custom_providers:`` 里那条的明文 api_key。"""
        entry_name, entry = self._legacy_entry_with_plaintext()
        fake = _fake_key("legacy")
        self._seed_legacy_key(entry_name, fake)

        with self._fake_probe() as seen:
            response = self.client.post(VALIDATE_PATH,
                                        json=_payload(f"{plugin_api.CC_ID_PREFIX}{entry_name}"))

        self.assertEqual(f"Bearer {fake}", seen["requests"][0].headers.get("authorization"),
                         "legacy 条目的明文密钥回退没生效")
        self.assertNotIn(FAKE_KEY_PREFIX, response.text)

    def test_fallback_does_not_touch_the_original_body(self):
        """回退写进的是**副本**（``model_copy``）：原 body 仍是 ``api_key=None``，无从回显。"""
        endpoint_id, entry = self._providers_entry_with_key_env()
        fake = _fake_key("copy")
        self._seed_env_key(str(entry["key_env"]).strip(), fake)
        body = CustomEndpointUpdate(id=endpoint_id, name="M3", base_url=PROBE_BASE_URL, model="fake/a")
        probed = plugin_api._with_stored_key(body)
        self.assertIsNone(body.api_key, "原 body 被改了：响应里任何一处都能把它回显出去")
        self.assertIsNot(body, probed, "回退必须写在副本上")
        self.assertEqual(fake, probed.api_key)
        for field in ("id", "name", "base_url", "model"):
            self.assertEqual(getattr(body, field), getattr(probed, field),
                             f"副本把非密钥字段 {field} 也改了")

    # ----------------------------------------------------- M3.11 / C5 不回退的两条路

    def test_missing_entry_sends_unauthenticated_probe(self):
        """M3.11：条目不存在（新建供应商）→ **不做**回退，裸发探测，不崩。"""
        self.assertNotIn(MISSING_ENTRY_ID, self.providers)
        self._scoped_calls = []

        def spy(name: str) -> str:
            self._scoped_calls.append(str(name))
            return ""

        with self._fake_probe() as seen, \
                mock.patch.object(plugin_api, "_scoped_key_env", side_effect=spy):
            response = self.client.post(VALIDATE_PATH, json=_payload(MISSING_ENTRY_ID))

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, len(seen["requests"]))
        self.assertIsNone(seen["requests"][0].headers.get("authorization"),
                          "条目不存在时不该带鉴权头（决策 14 派生口径）")
        self.assertEqual([], self._scoped_calls, "条目不存在时根本不该去读任何 key_env")
        self.assertEqual("ok", response.json()["error_kind"])

    def test_empty_id_sends_unauthenticated_probe(self):
        """新建路径（``id`` 为空串）同样不回退 —— 与上一条互补，守住「只在条目已存在时回退」。"""
        with self._fake_probe() as seen:
            response = self.client.post(VALIDATE_PATH, json=_payload(""))
        self.assertEqual(200, response.status_code, response.text)
        self.assertIsNone(seen["requests"][0].headers.get("authorization"))

    def test_fail_closed_scoped_key_env_probes_without_auth(self):
        """C5：``_scoped_key_env`` fail-closed 返回空串 → 按「无鉴权探测」走，**不抛异常**。"""
        endpoint_id, entry = self._providers_entry_with_key_env()
        with self._fake_probe() as seen, \
                mock.patch.object(plugin_api, "_scoped_key_env", return_value="") as scoped:
            response = self.client.post(VALIDATE_PATH, json=_payload(endpoint_id))
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(scoped.called, "本用例要覆盖的就是「回退去读了 key_env」这一步")
        self.assertIsNone(seen["requests"][0].headers.get("authorization"),
                          "拿不到 key 就该裸探测，不许编一个鉴权头")
        self.assertNotIn(FAKE_KEY_PREFIX, response.text)

    def test_broken_entry_lookup_degrades_to_plain_probe(self):
        """定位密钥时的任何异常都不许把探测打成 500（只读探测比报错有用）。"""
        endpoint_id, _entry = self._providers_entry_with_key_env()
        with self._fake_probe() as seen, \
                mock.patch.object(plugin_api, "load_config", side_effect=RuntimeError("boom")):
            response = self.client.post(VALIDATE_PATH, json=_payload(endpoint_id))
        self.assertEqual(200, response.status_code, response.text)
        self.assertIsNone(seen["requests"][0].headers.get("authorization"))
        self.assertEqual("ok", response.json()["error_kind"])

    # --------------------------------------------------------- M3.9 后端侧失败分类

    def test_probe_failure_kinds(self):
        """M3.9 的判据侧：不可达 / 401 / 403 / 其它非 2xx / 0 条 / 没填端点，各归各类。"""
        cases: List[Tuple[str, Dict[str, Any], str]] = [
            ("unreachable", {"error": httpx.ConnectError("no route")}, "unreachable"),
            ("rejected-401", {"status": 401}, "auth"),
            ("forbidden-403", {"status": 403}, "auth"),
            ("server-error", {"status": 500}, "http"),
            ("no-models", {"models": []}, "empty"),
            ("blank-url", {"base_url": ""}, "no_endpoint"),
        ]
        for label, kwargs, expected in cases:
            with self.subTest(case=label):
                if "base_url" in kwargs:
                    payload = self.client.post(
                        VALIDATE_PATH, json=_payload("", base_url=kwargs["base_url"])).json()
                else:
                    options: Dict[str, Any] = {k: v for k, v in kwargs.items() if k != "base_url"}
                    with self._fake_probe(**options):
                        payload = self.client.post(VALIDATE_PATH, json=_payload("")).json()
                self.assertEqual(expected, payload["error_kind"], payload)
                self.assertEqual([], payload["models"], payload)
                # 0 条不是「失败」：官方照样 ok=True（UC-06 允许手动添加），其余四类才是 ok=False
                self.assertEqual(expected == "empty", bool(payload["ok"]), payload)
                self.assertEqual(set(RESPONSE_FIELDS), set(payload), f"响应字段漂移：{payload}")

    def test_blank_endpoint_never_leaves_the_backend(self):
        """``base_url`` 为空时官方自己就短路返回，连一个请求都不会发（UC-01 的异常分支）。"""
        with self._fake_probe() as seen:
            payload = self.client.post(VALIDATE_PATH, json=_payload("", base_url="  ")).json()
        self.assertEqual([], seen["requests"])
        self.assertEqual("no_endpoint", payload["error_kind"])
        self.assertFalse(payload["ok"])

    # ------------------------------------------------------------- F10 零写入 + 只读性

    def test_validate_writes_nothing_to_the_copy(self):
        """F10 / M3 边界：探测链路一个字都不写 —— 连着打四种失败，副本字节不变。"""
        before_config = _read_copy_bytes("config.yaml")
        before_env = _read_copy_bytes(".env")
        with self._fake_probe():
            for body in (_payload(""), _payload(MISSING_ENTRY_ID),
                         _payload(MISSING_ENTRY_ID, api_key=_fake_key("write-test"))):
                self.assertEqual(200, self.client.post(VALIDATE_PATH, json=body).status_code)
        self.assertEqual(before_config, _read_copy_bytes("config.yaml"), "validate 写了 config.yaml")
        self.assertEqual(before_env, _read_copy_bytes(".env"), "validate 写了 .env")
        self.assertNotIn(FAKE_KEY_PREFIX, _read_copy_bytes("config.yaml").decode("utf-8", "replace"))
        self.assertNotIn(FAKE_KEY_PREFIX, _read_copy_bytes(".env").decode("utf-8", "replace"))

    def test_real_copy_secrets_never_appear_in_validate_responses(self):
        """T25 后端半边的加强版：拿副本里**真**存在的密钥去验「不外泄」。

        真密钥内容只在本用例内被读出来比对，绝不写进断言消息、也不出现在期望值里。
        """
        material: List[str] = []
        for entry in self.legacy:
            value = str(entry.get("api_key") or "").strip()
            if _is_plaintext(value) and len(value) >= 16:
                material.append(value)
        env_path = _home() / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                key, separator, value = line.partition("=")
                if separator and key.strip().startswith("HERMES_CUSTOM_"):
                    candidate = value.strip().strip('"').strip("'")
                    if len(candidate) >= 16:
                        material.append(candidate)
        self.assertTrue(material, "副本里没有可读密钥样本，本用例的「不外泄」分支未被覆盖")
        endpoint_id, _entry = self._providers_entry_with_key_env()
        legacy_name, _legacy = self._legacy_entry_with_plaintext()
        with self._fake_probe(), self._captured_logs() as logs:
            texts = [self.client.post(VALIDATE_PATH, json=_payload(endpoint_id)).text,
                     self.client.post(
                         VALIDATE_PATH,
                         json=_payload(f"{plugin_api.CC_ID_PREFIX}{legacy_name}")).text]
            joined_logs = "\n".join(logs)
        for secret in material:
            for fragment in _secret_fragments(secret):
                for text in texts:
                    self.assertNotIn(fragment, text, "响应里出现了副本中真实密钥的内容")
                self.assertNotIn(fragment, joined_logs, "日志里出现了副本中真实密钥的内容")

    def test_official_validate_is_called_with_the_same_single_body_argument(self):
        """M3.1：本路由**只**把（回退处理过的）body 交给官方，不塞 profile、不加旁路。"""
        async def fake_official(body):                            # 官方就是 async def（:622）
            return {"ok": True, "reachable": True, "message": "", "models": ["fake/model-a"]}

        with mock.patch.object(plugin_api, "validate_custom_endpoint", side_effect=fake_official) as official, \
                mock.patch.object(plugin_api, "_with_stored_key", side_effect=lambda body: body) as fallback:
            with self._fake_probe():
                response = self.client.post(VALIDATE_PATH, json=_payload("amd"))
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, fallback.call_count, "回退必须走在官方 validate 之前")
        self.assertEqual(1, official.call_count)
        self.assertEqual({}, official.call_args.kwargs,
                         f"官方 validate 不吃 profile，不该有额外关键字参数：{official.call_args}")
        self.assertEqual(1, len(official.call_args.args))
        self.assertIsInstance(official.call_args.args[0], CustomEndpointUpdate)

    def test_router_is_still_module_level_api_router(self):
        """M1 的骨架没被 M3 动到：``router`` 仍是模块级 APIRouter，两条路由并存。

        只断言「M1/M3 依赖的路径在位」：M4 起会继续往同一张表加写路由（M5-M7 亦然），
        整表快照不属于本模块的契约。
        """
        self.assertIsInstance(plugin_api.router, APIRouter)
        paths = {getattr(route, "path", None) for route in plugin_api.router.routes}
        required = {"/endpoints", "/endpoints/validate"}
        self.assertTrue(required <= paths,
                        f"M1/M3 的必备路径缺失：{sorted(required - paths)}，整表 {sorted(paths)}")


# ============================================== 前端静态判据（缺陷修复 round 1 · M3/M4/M7）

DESKTOP_DIR = PRODUCT_ROOT / "desktop"

# ``const|let|var x = new Map(`` / ``new Set(`` —— M3 的 ``groups`` 正是这一类。
# Map/Set **不支持** bracket 索引：``groups[name]`` 恒为 ``undefined``，紧跟的 ``.sort()``
# 就是 ``TypeError: Cannot read properties of undefined (reading 'sort')`` —— 真机上整页被
# 错误边界接管（desktop.log 19:43 / 19:48）。
_COLLECTION_DECL_RE = re.compile(r"(?:const|let|var)\s+(\w+)\s*=\s*new\s+(?:Map|Set)\s*\(")
# 约定式集合名（``addedSet`` 这种**接参数进来的** Map/Set，声明处抓不到，按名字抓）。
_COLLECTION_NAME_RE = re.compile(r"^\w+(?:Map|Set)$")


def _strip_js_comments(text: str) -> str:
    """去掉 ``//`` 行注释与 ``/* */`` 块注释（判据按**代码**判，注释里的字样不算数）。

    与 ``test_activate_delete.py`` / ``test_migrate.py`` 的同名 helper 逐字同口径
    （三个模块各自持有一份，不跨测试文件 import —— 与本目录既有约定一致）。
    """
    out: List[str] = []
    index = 0
    length = len(text)
    while index < length:
        pair = text[index:index + 2]
        if pair == "//":
            end = text.find("\n", index)
            index = length if end == -1 else end
            continue
        if pair == "/*":
            end = text.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        out.append(text[index])
        index += 1
    return "".join(out)


class PluginJsM3LintTest(unittest.TestCase):
    """``desktop/plugin.js`` 的**静态** lint（M3 的纯函数区 + round 1 两颗缺陷的护栏）。

    ⚠️ 这不是渲染证据（prompt §5.4）：磁盘插件没有渲染基建，能钉的就是「代码形状」。
    缺陷 #2 那条尤其如此 —— 它原本是一个**运行时才炸**的 TypeError，本类把它钉成
    跑测试就红的静态判据（真机表现：探测成功且候选跨多个厂商时整页进错误边界）。
    """

    @classmethod
    def setUpClass(cls):
        cls.source = (DESKTOP_DIR / "plugin.js").read_text(encoding="utf-8")
        cls.code = _strip_js_comments(cls.source)

    @staticmethod
    def _fn_body(code: str, signature: str, terminator: str = "\n}") -> str:
        """从 ``signature`` 起切到下一个 ``terminator``（够圈住一个函数体）。"""
        begin = code.index(signature)
        return code[begin:code.index(terminator, begin)]

    # ------------------------------------------------ 缺陷 #2：Map 的 bracket 索引（全站）

    def test_no_bracket_indexing_of_map_or_set_anywhere(self):
        """任何 Map/Set（声明式 ``x = new Map()`` 或约定名 ``*Map`` / ``*Set``）都**不许**被
        ``x[...]`` 读。判据覆盖**整份文件**（M3–M7 全扫一遍），不只 ``deriveCandidateGroups``——
        同一类崩溃只会以另一种变量名再长出来。"""
        names = set(_COLLECTION_DECL_RE.findall(self.code))
        names |= {name for name in re.findall(r"\b([A-Za-z_$]\w*)\s*\[", self.code)
                  if _COLLECTION_NAME_RE.match(name)}
        self.assertTrue(names, "一条 Map/Set 都没抓到？判据落空了")
        offenders = [name for name in sorted(names)
                     if re.search(rf"\b{re.escape(name)}\s*\[", self.code)]
        self.assertEqual([], offenders,
                         f"Map/Set 被 bracket 索引了（恒 undefined → 整页错误边界）：{offenders}")
        self.assertNotIn("groups[", self.code, "M3 的 `groups` 又回到 bracket 索引")

    def test_derive_candidate_groups_reads_buckets_via_get(self):
        """修复后的**正向**形状：``deriveCandidateGroups`` 全文件只有一处，分组一律经 ``.get()``。"""
        self.assertEqual(1, self.source.count("function deriveCandidateGroups("),
                         "deriveCandidateGroups 被复制出第二份（分组口径必须唯一）")
        body = self._fn_body(self.code, "function deriveCandidateGroups(")
        self.assertIn("groups.get(vendor) || []", body, "分组累加不再走 Map.get")
        self.assertIn("groups.get(name).sort((a, b) => a.id.localeCompare(b.id))", body,
                      "分组项不再走 Map.get（缺陷 #2 的原始崩溃点）")
        self.assertIn("OTHER_GROUP_LABEL", body, "「Other 恒排最后」的判据不在本函数里了")


if __name__ == "__main__":
    unittest.main()
