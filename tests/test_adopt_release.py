"""M12 验收：官方密钥供应商「克隆转正」（接管 ``adopt`` / 退回 ``release``）+ 只读候选发现。

设计依据：``detailed-designv0.0.3.md`` §4（§4.3.1/4.3.2/4.3.3 三条路由、§4.3.4 拒绝面表、
§4.5 状态机、§4.7 测试表 a–k）；需求 ``proposalv0.0.3.md`` §5.2.2 / §5.4。

⚠️ 跑法（``tasksv0.0.3/progress.md`` §八 主 Agent 钉住的 harness 变体，填好占位后照抄；
**venv 里没有 pytest，也不许装** —— stdlib ``unittest`` only）：

    cd "<本仓根目录>"                        # 必须 cd 到含 tests/ 的目录，discover 依赖 cwd
    V="<装有 hermes_cli 的 venv 里的 python>"
    T="<临时目录>/supplier-models-harness"    # 副本务必建在仓外，本仓 .gitignore 已排除 config/.env
    rm -rf "$T" && mkdir -p "$T"
    cp "<基线备份 config.yaml>" "$T/config.yaml"
    cp "<基线备份 .env>"        "$T/.env"
    HERMES_HOME="$(cygpath -w "$T")" "$V" -m unittest discover -s tests -v
    rm -rf "$T"      # 副本里含明文密钥，跑完必须删

（副本源是**仍留有 legacy ``custom_providers:`` 条目的基线备份对**、不是当前真配置：
progress §八 的 R-1 裁决；真 ``config.yaml`` / ``.env`` 全程只读。手上没有这种备份时得
手工构造同样形状的副本 —— 喂一份 ``custom_providers: []`` 的副本会成批 ``setUp`` 前置
断言失败，那是输入不匹配、不是代码缺陷。）

四条纪律（照 ``test_migrate.py`` 的头注口径，本文件同样写盘）：

* **只写副本**：每个写动作第一件事就是 ``self._guard_before_write()`` —— ``config.yaml``
  与 ``.env`` 两条路径都必须落在系统临时目录下的副本里；``setUp`` / ``tearDown``
  各按字节还原 ``setUpClass`` 的快照，所以用例顺序无关、破坏性写在用例内闭环。
* **密钥值零出现**：断言用的密钥值一律是本文件造的形状（``sk-FAKE-M12-*``）；副本里
  真密钥只被**按值比对**用于「有没有被动过」，且比对一律走 ``_assert_absent`` 这类
  **固定 msg** 的写法（``unittest`` 的默认失败消息会把参数原文吐进输出），本文件不出现
  任何真实 key 的字面量，也不把值打印出来。
* **零网络 / 零真 env**：目录侧三处取数（``provider_catalog`` / ``load_env`` /
  ``_PROVIDER_MODELS``）与 ``PROVIDER_REGISTRY`` 一律经基类的 ``_patch`` 换成**本文件的
  合成供应商**（端点用 ``.invalid`` TLD：结构上不可能有真服务，同时也是零网络的证明）。
* **计数不硬编码**（progress §五）：条目数 / 白名单条数 / 「还剩几条」全部**先从副本读**
  再断言运算关系；``amd`` / ``token_rhythm`` / ``zhipu-glm`` 这些名字只当**取材对象**，
  不作为期望值写死。

判据口径：值级比对为主（官方 ``save_config`` 首次写入会追加注释块，逐行 diff 必假红）。
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli.config import (
    get_config_path,
    get_env_path,
    invalidate_env_cache,
    load_config,
    save_config,
)
from hermes_cli.provider_catalog import ProviderDescriptor

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PRODUCT_ROOT / "dashboard"
DESKTOP_DIR = PRODUCT_ROOT / "desktop"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import plugin_api  # noqa: E402

MOUNT_PREFIX = "/api/plugins/supplier-models"
LIST_PATH = f"{MOUNT_PREFIX}/endpoints"                     # M1 只读路由（行契约冻结）
CANDIDATES_PATH = f"{MOUNT_PREFIX}{plugin_api.BUILTIN_CANDIDATES_PATH}"
# 密钥铁律（与 M1 的 KEY_MATERIAL_KEYS 同口径）：响应里不许出现任何密钥材料字段
FORBIDDEN_RESPONSE_KEYS = ("api_key", "api_key_preview")
# 设计 §2.2 的统一禁词表（M10 已按它重写全站；本模块的新串从第一天起就得过这一关）
BANNED_COPY_WORDS = ("providers:", "models:", "custom_providers:", "discover_models",
                     "model:", "key_env", "标准形态", "决议 ", "UC-", "§")
# 只读提示的**逐字**冻结串（拍板 #11 / 设计 §4.4-4）
CLONE_KEY_READONLY_HINT = "改密钥请回官方设置 · API 密钥页（官方换钥匙，这里自动跟上）"

# ── 合成供应商（不碰真目录、不碰真 env）────────────────────────────────────────
SYNTH = "zzm12"
SYNTH_SLUG = f"{SYNTH}-source"                 # 走 PROVIDER_REGISTRY 主路
SYNTH_LABEL = "ZZ M12 Source"
SYNTH_ENV_VAR = "ZZ_M12_FAKE_OFFICIAL_VAR"
SYNTH_FAKE_SECRET = "sk-FAKE-M12-not-a-real-key"
SYNTH_BASE_URL = f"https://{SYNTH_SLUG}.invalid/v1"
SYNTH_MODELS = [f"{SYNTH}/alpha", f"{SYNTH}/beta", f"{SYNTH}/gamma"]
FALLBACK_SLUG = "openrouter"                   # 走 A4 兜底表（不在注册表、无静态目录）
FALLBACK_ENV_VAR = "OPENROUTER_API_KEY"
NOKEY_SLUG = f"{SYNTH}-nokey"
KEYLESS_SLUG = f"{SYNTH}-keyless"
ACCOUNTS_SLUG = f"{SYNTH}-accounts"
CLONE_OF_SYNTH = f"{plugin_api.CLONE_ID_PREFIX}{SYNTH_SLUG}"


def _descriptor(slug: str, *, env_vars: Tuple[str, ...] = (SYNTH_ENV_VAR,), label: str = SYNTH_LABEL,
                tab: str = "keys", keyless: bool = False,
                base_url_env_var: str = "") -> ProviderDescriptor:
    """造一条内置目录描述符（字段名实测自 ``provider_catalog.ProviderDescriptor``）。"""
    return ProviderDescriptor(
        slug=slug, label=label, description=f"{slug} used by tests/test_adopt_release.py",
        auth_type="api_key", tab=tab, api_key_env_vars=env_vars,
        base_url_env_var=base_url_env_var, signup_url="", order=999, keyless=keyless)


def _catalog() -> List[ProviderDescriptor]:
    """本文件的「目录」：一条已配密钥、一条未配、一条匿名、一条在「账号」页。"""
    return [
        _descriptor(SYNTH_SLUG),
        _descriptor(NOKEY_SLUG, env_vars=(f"{SYNTH}_NOKEY_VAR",), label="ZZ M12 NoKey"),
        _descriptor(KEYLESS_SLUG, env_vars=(f"{SYNTH}_KEYLESS_VAR",), label="ZZ M12 Keyless",
                    keyless=True),
        _descriptor(ACCOUNTS_SLUG, env_vars=(f"{SYNTH}_ACCOUNTS_VAR",), label="ZZ M12 Accounts",
                    tab="accounts"),
    ]


# --------------------------------------------------------------- 副本读写原语

def _home() -> Path:
    value = os.environ.get("HERMES_HOME")
    if not value:
        raise AssertionError("HERMES_HOME 未设置 —— 请用 progress §八 钉住的 harness 命令跑")
    return Path(value).resolve()


def _read_copy_bytes(name: str) -> bytes:
    return (_home() / name).read_bytes()


def _write_copy_bytes(data: bytes, name: str) -> None:
    (_home() / name).write_bytes(data)


def _load_copy_yaml() -> Dict[str, Any]:
    return yaml.safe_load(_read_copy_bytes("config.yaml").decode("utf-8")) or {}


def _store_copy_yaml(data: Dict[str, Any]) -> None:
    _write_copy_bytes(yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode("utf-8"),
                      "config.yaml")
    invalidate_env_cache()


def _providers_of(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    providers = (data if data is not None else _load_copy_yaml()).get("providers")
    return providers if isinstance(providers, dict) else {}


def _env_pairs() -> Dict[str, str]:
    """副本 ``.env`` 的 (变量名 → 值)。**只用于值级比对的被减数**，永不打印值。"""
    pairs: Dict[str, str] = {}
    for line in _read_copy_bytes(".env").decode("utf-8", errors="replace").splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not match:
            continue
        pairs[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return pairs


def _sha(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _assert_absent(test: unittest.TestCase, needle: str, haystack: str, message: str) -> None:
    """**固定 msg** 的「不含」判据：默认失败消息会把 needle（可能是密钥值）原文吐出来。"""
    test.assertFalse(needle in haystack, message)


def _assert_no_key_material(test: unittest.TestCase, payload: Any, origin: str) -> None:
    """密钥铁律的值级判据（M1 手法）：响应 JSON 序列化后**不含任何** env 值 / 密钥材料字段。

    msg 只出**变量名与长度**，绝不出值。
    """
    blob = json.dumps(payload, ensure_ascii=False, default=str)
    for key in FORBIDDEN_RESPONSE_KEYS:
        _assert_absent(test, f'"{key}"', blob, f"{origin} 的响应里出现了密钥材料字段 {key}")
    for name, value in _env_pairs().items():
        if len(value) < 8:
            continue
        _assert_absent(test, value, blob,
                       f"{origin} 的响应带出了 {name} 的值（长度 {len(value)}，值不打印）")
    _assert_absent(test, SYNTH_FAKE_SECRET, blob,
                   f"{origin} 的响应带出了本文件的假密钥值（{SYNTH_ENV_VAR}）")


def _literal_text(node: ast.AST) -> str:
    """取一个 AST 表达式里**写死的措辞**（f-string 的插值槽丢掉，只留字面段）。

    禁词判据要的是「用户会读到什么」，而插值槽塞进来的是 id / 名称，判它没有意义；
    ``ast.literal_eval`` 又在 f-string 上直接报错，所以自己走这三型。
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else ""
    if isinstance(node, ast.JoinedStr):
        return "".join(_literal_text(part) for part in node.values)
    if isinstance(node, ast.BinOp):
        return _literal_text(node.left) + _literal_text(node.right)
    return ""


class M12CopyTest(unittest.TestCase):
    """写路径用例的共同地基：副本快照 + 还原 + 写前守卫 + 目录侧打桩。"""

    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(plugin_api.router, prefix=MOUNT_PREFIX)
        cls.client = TestClient(app)
        cls.pristine_config = _read_copy_bytes("config.yaml")
        cls.pristine_env = _read_copy_bytes(".env")

    def setUp(self) -> None:
        self._guard_before_write()
        self.assertTrue((_home() / ".env").exists(),
                        "副本里没有 .env —— §八 钉住的 harness 命令要求一并复制 .env")
        self.assertEqual(str((_home() / ".env").resolve()).lower(),
                         str(Path(get_env_path()).resolve()).lower(),
                         "get_env_path() 不在副本上，.env 的写测试会碰真配置")
        self._restore_copy()
        # 目录侧一律打桩：不碰真 env、不发网络（设计 §4.2 的三处取数 + 注册表）
        self._patch("provider_catalog", return_value=[
            *_catalog(), _descriptor(FALLBACK_SLUG, env_vars=(FALLBACK_ENV_VAR,),
                                     label="OpenRouter")])
        self._patch("load_env",
                    return_value={SYNTH_ENV_VAR: SYNTH_FAKE_SECRET,
                                  FALLBACK_ENV_VAR: SYNTH_FAKE_SECRET})
        self._patch("_PROVIDER_MODELS", {SYNTH_SLUG: list(SYNTH_MODELS)})
        self._patch("PROVIDER_REGISTRY", {SYNTH_SLUG: mock.Mock(inference_base_url=SYNTH_BASE_URL)})
        self._patch("get_default_model_for_provider",
                    side_effect=lambda provider: f"{provider}-fallback-model")

    def _patch(self, attribute: str, *args: Any, **kwargs: Any) -> Any:
        """把 ``plugin_api`` 自己那份绑定打上桩（stdlib unittest 没有 ``self.patch``）。

        打在模块全局上 = 本文件的桩与 M6.7 的 ``save_config`` 计数器同一口径：被测代码
        查的就是这个名字，绕开真目录、真 env 与真网络。``addCleanup`` 负责还原。
        """
        patcher = mock.patch.object(plugin_api, attribute, *args, **kwargs)
        replacement = patcher.start()
        self.addCleanup(patcher.stop)
        return replacement

    def tearDown(self) -> None:
        self._restore_copy()

    # ---------------------------------------------------------------- 基础设施

    def _guard_before_write(self) -> None:
        """每个写动作前的红线断言（照 M4/M5/M6 的同一道守卫，不许省）。"""
        home = _home()
        config_path = Path(get_config_path()).resolve()
        self.assertTrue(home.is_relative_to(Path(tempfile.gettempdir()).resolve()),
                        f"写入前守卫失败：HERMES_HOME={home} 不在系统临时目录下")
        self.assertTrue(str(config_path).lower().startswith(str(home).lower()),
                        f"写入前守卫失败：get_config_path()={config_path} 不在副本 {home} 之下")
        self.assertEqual(str(home / "config.yaml").lower(), str(config_path).lower())

    def _restore_copy(self) -> None:
        if _read_copy_bytes("config.yaml") != self.pristine_config:
            _write_copy_bytes(self.pristine_config, "config.yaml")
        self.assertEqual(self.pristine_config, _read_copy_bytes("config.yaml"),
                         "副本 config.yaml 没能还原成字节级原样")
        if _read_copy_bytes(".env") != self.pristine_env:
            _write_copy_bytes(self.pristine_env, ".env")
        invalidate_env_cache()
        self.assertEqual(self.pristine_env, _read_copy_bytes(".env"), "副本 .env 被写坏了")

    # ------------------------------------------------------------------ 取数

    def _rows(self) -> List[Dict[str, Any]]:
        response = self.client.get(LIST_PATH)
        self.assertEqual(200, response.status_code, response.text)
        return response.json().get("endpoints") or []

    def _row_ids(self) -> List[str]:
        return [str(row.get("id") or "") for row in self._rows()]

    def _candidates(self) -> Dict[str, Any]:
        response = self.client.get(CANDIDATES_PATH)
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def _candidate_rows(self) -> List[Dict[str, Any]]:
        return self._candidates().get("candidates") or []

    def _candidate_for(self, slug: str) -> Dict[str, Any]:
        rows = [row for row in self._candidate_rows() if str(row.get("id") or "") == slug]
        self.assertEqual(1, len(rows), f"候选列表里 {slug} 应当恰好一行，实到 {len(rows)} 行")
        return rows[0]

    def _adopt(self, slug: str):
        self._guard_before_write()
        return self.client.post(f"{LIST_PATH}/{slug}/adopt")

    def _release(self, endpoint_id: str, payload: Optional[Dict[str, Any]] = None):
        self._guard_before_write()
        return self.client.post(f"{LIST_PATH}/{endpoint_id}/release", json=payload or {})

    def _entry(self, endpoint_id: str) -> Dict[str, Any]:
        entry = _providers_of().get(endpoint_id)
        self.assertIsInstance(entry, dict, f"副本 providers 里没有可用的 {endpoint_id} 条目")
        return entry

    def _seed_provider(self, endpoint_id: str, entry: Dict[str, Any]) -> None:
        data = _load_copy_yaml()
        providers = data.get("providers") if isinstance(data.get("providers"), dict) else {}
        providers[endpoint_id] = entry
        data["providers"] = providers
        _store_copy_yaml(data)

    def _adopt_clone(self, slug: str = SYNTH_SLUG) -> Dict[str, Any]:
        """前置：真跑一次接管，返回接管响应（用例自己的断言不受它影响）。"""
        response = self._adopt(slug)
        self.assertEqual(200, response.status_code, response.text)
        return response.json()


# ═══════════════════════════ 用例 a：发现过滤（M12.14 / 设计 §4.3.1）═══════════════

class BuiltinCandidatesDiscoveryTest(M12CopyTest):
    """只读候选那条 GET：五段过滤 + 行字段 + **纯读零写入**。"""

    def test_only_providers_with_a_configured_official_key_are_listed(self):
        """a-1/a-2：配了才出现、没配不出现（proposal §5.4-1「未配密钥的目录项不出现」）。"""
        ids = [str(row.get("id") or "") for row in self._candidate_rows()]
        self.assertIn(SYNTH_SLUG, ids, "已配密钥的目录项没被列出来")
        self.assertNotIn(NOKEY_SLUG, ids, "未配密钥的目录项出现在候选里（该收紧的范围）")
        # 把同一个变量从 env 里拿掉 → 连这一条也不出现（判据是 env 存在性，不是猜）
        self._patch("load_env", return_value={})
        ids_after = [str(row.get("id") or "") for row in self._candidate_rows()]
        self.assertNotIn(SYNTH_SLUG, ids_after, "去掉密钥后候选仍然列出了它")

    def test_keyless_and_accounts_tab_entries_are_out_of_scope(self):
        """a-4：匿名端点（keyless）与「账号」页（OAuth 登录态）一概不进这张表。"""
        ids = [str(row.get("id") or "") for row in self._candidate_rows()]
        self.assertNotIn(KEYLESS_SLUG, ids, "keyless 条目被列成可接管候选")
        self.assertNotIn(ACCOUNTS_SLUG, ids, "accounts 页的条目被列成可接管候选")

    def test_adopted_or_name_occupied_entries_are_not_listed_again(self):
        """a-3：已接管（克隆行在主列表里）与名字被**可用**条目占用 → 都不重复出现。"""
        self.assertIn(SYNTH_SLUG, [row["id"] for row in self._candidate_rows()])
        self._seed_provider(CLONE_OF_SYNTH, {"base_url": SYNTH_BASE_URL, "model": SYNTH_MODELS[0],
                                             "name": "已接管的克隆"})
        self.assertNotIn(SYNTH_SLUG, [row["id"] for row in self._candidate_rows()],
                         "已有克隆还列候选（会与主列表的克隆行双份）")

        data = _load_copy_yaml()
        del data["providers"][CLONE_OF_SYNTH]
        data["providers"][SYNTH_SLUG] = {"base_url": "https://user-own.invalid/v1",
                                         "model": "user/model", "name": "用户自建同名"}
        _store_copy_yaml(data)
        self.assertNotIn(SYNTH_SLUG, [row["id"] for row in self._candidate_rows()],
                         "名字被用户自建条目占用还列候选（接管会撞车）")

    def test_half_adopted_state_is_still_listed_with_its_status(self):
        """设计 §4.5 第四态：无克隆 + 官方入口已停用 → **仍然列出**并带可修复状态。"""
        self._seed_provider(SYNTH_SLUG, {"enabled": False})
        rows = self._candidate_rows()
        row = next((item for item in rows if str(item.get("id") or "") == SYNTH_SLUG), None)
        self.assertIsNotNone(row, "半途态被当成「已占用」而整条消失了（该列出才可修复）")
        self.assertEqual(plugin_api.BUILTIN_STATUS_HALF_ADOPTED, row.get("status"),
                         "半途态没标出自己的状态")
        self.assertIs(True, row.get("adoptable"), "半途态不该把接管钮关掉")

    def test_candidate_row_shape_and_snapshot_sources_expose_only_names(self):
        """§4.3.1 行字段 + §4.2 端点快照两条来源（注册表主路 / A4 兜底表）。"""
        row = self._candidate_for(SYNTH_SLUG)
        self.assertEqual(SYNTH_LABEL, row.get("name"))
        self.assertEqual(plugin_api.BUILTIN_ROW_SOURCE, row.get("source"))
        self.assertEqual(SYNTH_BASE_URL, row.get("base_url"), "端点快照没取注册表那一条")
        self.assertEqual(list(SYNTH_MODELS), row.get("models"))
        self.assertIs(True, row.get("has_api_key"))
        self.assertEqual([SYNTH_ENV_VAR], row.get("api_key_env_names"), "只该出**变量名**")
        self.assertIs(True, row.get("adoptable"))
        self.assertEqual(plugin_api.BUILTIN_STATUS_UNMANAGED, row.get("status"))
        # 不在注册表、也没有静态目录的那一类（openrouter）：端点落兜底表、目录为空
        fallback = self._candidate_for(FALLBACK_SLUG)
        self.assertEqual(plugin_api.BUILTIN_BASE_URL_FALLBACK[FALLBACK_SLUG],
                         fallback.get("base_url"), "兜底表没补上注册表给不出的端点")
        self.assertEqual([], fallback.get("models"), "静态目录缺失时不该凭空造模型")
        self.assertEqual([FALLBACK_ENV_VAR], fallback.get("api_key_env_names"))
        # 信封两个键；current 复用 M1 语义
        envelope = self._candidates()
        self.assertEqual({"candidates", "current"}, set(envelope))
        self.assertIsInstance(envelope.get("current"), dict)

    def test_discovery_is_read_only(self):
        """纯读：一条候选查询之后副本逐字节不变。"""
        config_before = _read_copy_bytes("config.yaml")
        env_before = _read_copy_bytes(".env")
        with mock.patch.object(plugin_api, "save_config",
                               wraps=plugin_api.save_config) as counter:
            self.assertTrue(self._candidate_rows())
            self.assertEqual(0, counter.call_count, "发现路由写了盘")
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"), "候选查询写了 config.yaml")
        self.assertEqual(env_before, _read_copy_bytes(".env"), "候选查询写了 .env")

    def test_discovery_response_carries_no_key_values(self):
        """k 的发现半边：值级比对（M1 手法）。"""
        _assert_no_key_material(self, self._candidates(), "GET /endpoints/builtin-candidates")


# ════════════════════════════ 用例 b / c / d / e / f：接管（M12.15–M12.19）══════════

class AdoptRouteTest(M12CopyTest):
    """``POST /endpoints/{slug}/adopt``：落盘形状 + .env 零写入 + 单次写 + 幂等 + 拒绝面。"""

    def test_adopt_lands_clone_referencing_official_var_and_disables_builtin(self):
        """b：克隆的磁盘形状六条 + 停用开关同现 + 最小开关不冒幽灵行。"""
        self._guard_before_write()
        keys_before = set(_providers_of())
        payload = self._adopt_clone()

        providers = _providers_of()
        self.assertIn(CLONE_OF_SYNTH, providers, "克隆条目没落盘")
        self.assertEqual(keys_before | {CLONE_OF_SYNTH, SYNTH_SLUG}, set(providers),
                         "接管顺手动了别的条目（新增的应当只有克隆与停用开关）")
        clone = providers[CLONE_OF_SYNTH]
        self.assertEqual(SYNTH_ENV_VAR, clone.get("key_env"),
                         "key_env 没指向 .env 里既有的官方变量名（拍板 #11 的地基）")
        self.assertNotIn("api_key", clone, "克隆条目上不许带 api_key 字段")
        self.assertEqual(SYNTH_SLUG, clone.get(plugin_api.CLONE_ORIGIN_FIELD),
                         "来源标记不对（前端只读判据与退回的内置名都吃它）")
        self.assertIs(False, clone.get("discover_models"), "接管必须落进白名单模式（铁律 1）")
        self.assertEqual(SYNTH_BASE_URL, clone.get("base_url"), "端点快照没落盘")
        self.assertEqual(SYNTH_MODELS[0], clone.get("model"), "默认模型该取静态目录首项")
        self.assertEqual(sorted(SYNTH_MODELS), sorted((clone.get("models") or {}).keys()))
        self.assertEqual({"enabled": False}, providers.get(SYNTH_SLUG),
                         "停用内置应当是最小开关（带端点的条目会在官方列表里冒幽灵行）")
        # 回执
        self.assertIs(True, payload.get("ok"))
        self.assertEqual(CLONE_OF_SYNTH, payload.get("id"))
        self.assertEqual(plugin_api.ADOPT_CREATED, payload.get("adopted"))
        self.assertIs(True, payload.get("disabled_builtin"))
        self.assertEqual(SYNTH_ENV_VAR, payload.get("key_env"))
        self.assertEqual(SYNTH_BASE_URL, payload.get("snapshot_base_url"))
        # 官方出行判据：最小开关不出行 → 列表里只有克隆那一行
        ids = self._row_ids()
        self.assertIn(CLONE_OF_SYNTH, ids)
        self.assertNotIn(SYNTH_SLUG, ids, "只带停用开关的条目冒成了列表行（幽灵行）")

    def test_adopt_writes_zero_bytes_to_env_and_reports_env_written_false(self):
        """c：**副本 .env 逐字节不变** + 回执 ``env_written`` 恒 False。"""
        self._guard_before_write()
        env_before = _read_copy_bytes(".env")
        # 打在**官方**那份绑定上：本插件区块根本不 import `save_env_value`（这本身就是证据）
        with mock.patch("hermes_cli.web_routers.config_env.save_env_value",
                        side_effect=AssertionError("接管不该写 .env")) as env_write:
            payload = self._adopt_clone()
        self.assertEqual(0, env_write.call_count, "接管调用了一次 .env 写入")
        self.assertIs(False, payload.get("env_written"))
        self.assertEqual(env_before, _read_copy_bytes(".env"), ".env 被写动了（拍板 #11 破了）")
        # 官方那把变量名也没被「清空密钥」那条支碰过（值级：键还在、字节不变）
        self.assertEqual(env_before, _read_copy_bytes(".env"))

    def test_adopt_calls_save_config_exactly_once(self):
        """d（接管半边）：克隆 + 停用合并成**单次**原子写（M6.7 同手法，打在 plugin_api 自己那份绑定上）。"""
        self._guard_before_write()
        with mock.patch.object(plugin_api, "save_config",
                               wraps=plugin_api.save_config) as counter:
            response = self._adopt(SYNTH_SLUG)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, counter.call_count,
                         "接管不是单次原子写（>1 次会先把刚写进去的条目盖掉）")
        self.assertIn(CLONE_OF_SYNTH, _providers_of(), "单次写之后克隆条目却没落盘")

    def test_adopt_is_idempotent_and_leaves_the_key_set_untouched(self):
        """e：已有克隆再接管 → ``adopted:"already"``，且 providers 键集合零变化。"""
        self._adopt_clone()
        keys_before = set(_providers_of())
        entry_before = json.dumps(_providers_of()[CLONE_OF_SYNTH], sort_keys=True)
        payload = self._adopt_clone()

        self.assertEqual(plugin_api.ADOPT_ALREADY, payload.get("adopted"))
        self.assertEqual(keys_before, set(_providers_of()), "重试接管新增了条目（不幂等）")
        self.assertEqual(entry_before, json.dumps(_providers_of()[CLONE_OF_SYNTH], sort_keys=True),
                         "重试接管改写了既有克隆的字段")

    def test_adopt_repairs_the_half_adopted_state(self):
        """§4.5 第四态的修复路：半途态下再点接管 → 补齐克隆 + 停用照旧在位。"""
        self._guard_before_write()
        self._seed_provider(SYNTH_SLUG, {"enabled": False})
        payload = self._adopt_clone()
        self.assertEqual(plugin_api.ADOPT_CREATED, payload.get("adopted"))
        providers = _providers_of()
        self.assertIn(CLONE_OF_SYNTH, providers)
        self.assertIs(False, providers[SYNTH_SLUG].get("enabled"))

    def test_adopt_refusals_write_zero_bytes(self):
        """f：名字占用 409 / 未配密钥 404 / 陌生 slug 404 —— 三条拒绝面全部零写入。"""
        self._guard_before_write()
        self._seed_provider(SYNTH_SLUG, {"base_url": "https://user-own.invalid/v1",
                                         "model": "user/model", "name": "用户自建同名"})
        config_before = _read_copy_bytes("config.yaml")
        env_before = _read_copy_bytes(".env")
        response = self._adopt(SYNTH_SLUG)
        self.assertEqual(409, response.status_code, response.text)
        self.assertIn("列表未改动", response.json().get("detail") or "",
                      "拒绝没给「列表未改动」承诺")

        unconfigured = self._adopt(NOKEY_SLUG)
        self.assertEqual(404, unconfigured.status_code, unconfigured.text)
        unknown = self._adopt(f"{SYNTH}-not-in-catalog")
        self.assertEqual(404, unknown.status_code, unknown.text)
        for refusal in (response, unconfigured, unknown):
            self.assertIn("列表未改动", refusal.json().get("detail") or "")

        self.assertNotIn(CLONE_OF_SYNTH, _providers_of(), "被拒的接管还是把克隆写出来了")
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"), "拒绝面写了 config.yaml")
        self.assertEqual(env_before, _read_copy_bytes(".env"), "拒绝面写了 .env")

    def test_adopt_refuses_when_the_snapshot_or_default_model_is_unavailable(self):
        """§4.3.4 第三行：端点快照或默认模型取不到 → 400 + 零写入。"""
        self._guard_before_write()
        self._patch("PROVIDER_REGISTRY", {})          # 注册表没有 → 无快照
        config_before = _read_copy_bytes("config.yaml")
        response = self._adopt(SYNTH_SLUG)
        self.assertEqual(400, response.status_code, response.text)
        self.assertIn("列表未改动", response.json().get("detail") or "")
        self.assertNotIn(CLONE_OF_SYNTH, _providers_of())
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"), "400 还是写了盘")

    def test_adopt_response_carries_no_key_values(self):
        """k 的接管半边：值级比对。"""
        _assert_no_key_material(self, self._adopt_clone(), "POST /endpoints/{slug}/adopt")


# ═══════════════════════════════ 用例 g / h / i + d：退回（M12.20–M12.22）═══════════

class ReleaseRouteTest(M12CopyTest):
    """``POST /endpoints/{id}/release``：三拍顺序 + precheck 只读 + 非克隆拒绝 + 单次写。"""

    def _set_top_model(self, mapping: Dict[str, Any]) -> None:
        data = _load_copy_yaml()
        data["model"] = mapping
        _store_copy_yaml(data)

    def test_precheck_is_read_only_and_agrees_with_the_real_run(self):
        """precheck：零写入 + ``{blocks, plan}`` 形状，且判据与真跑同一份计算。"""
        self._adopt_clone()
        config_before = _read_copy_bytes("config.yaml")
        env_before = _read_copy_bytes(".env")
        with mock.patch.object(plugin_api, "save_config",
                               side_effect=AssertionError("precheck 不该写盘")) as counter:
            response = self._release(CLONE_OF_SYNTH, {"precheck_only": True})
        self.assertEqual(0, counter.call_count)
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertIs(True, payload.get("precheck_only"))
        self.assertEqual(CLONE_OF_SYNTH, payload.get("id"))
        self.assertIn("blocks", payload, "precheck 少了 blocks（弹窗按它组句）")
        self.assertEqual(SYNTH_SLUG, (payload.get("plan") or {}).get("origin"))
        self.assertEqual([], payload.get("blocks"), "顶层没指克隆却报了提示")
        self.assertNotIn("endpoints", payload, "precheck 不该顺手跑一遍列表（白读盘）")
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"), "precheck 写了 config")
        self.assertEqual(env_before, _read_copy_bytes(".env"), "precheck 写了 .env")
        self.assertIn(CLONE_OF_SYNTH, _providers_of(), "precheck 把克隆条目删掉了（它是只读分支）")

    def test_release_repoints_top_level_model_then_removes_clone_then_unloads(self):
        """g：顶层指着克隆时，同一次写入内 provider 切回内置、克隆消失、停用撤销（空条目整摘）。"""
        self._guard_before_write()
        self._adopt_clone()
        self._set_top_model({"provider": CLONE_OF_SYNTH, "default": f"{SYNTH}/off-catalog",
                             "base_url": "https://mirror.invalid/v1",
                             "api_key": "zz-not-a-real-key", "key_env": "ZZ_MIRROR_VAR"})
        config_before = _read_copy_bytes("config.yaml")
        env_before = _read_copy_bytes(".env")
        # 前置检查必须先预告「会切回官方」，弹窗才有那句实况话
        pre = self._release(CLONE_OF_SYNTH, {"precheck_only": True}).json()
        self.assertIs(True, (pre.get("plan") or {}).get("moved_top_model"))
        self.assertIn(plugin_api.RELEASE_NOTE_TOP_MODEL, pre.get("blocks") or [])
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"), "precheck 写了盘")

        with mock.patch.object(plugin_api, "save_config",
                               wraps=plugin_api.save_config) as counter:
            response = self._release(CLONE_OF_SYNTH)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, counter.call_count, "退回不是单次原子写")
        released = response.json().get("released") or {}
        self.assertIs(True, released.get("moved_top_model"))
        self.assertEqual(SYNTH_SLUG, released.get("to_builtin"))
        self.assertIs(True, released.get("clone_removed"))
        self.assertIs(True, released.get("builtin_reenabled"))

        data = _load_copy_yaml()
        providers = _providers_of(data)
        self.assertNotIn(CLONE_OF_SYNTH, providers, "克隆没被删（第 2 拍没落地）")
        self.assertNotIn(SYNTH_SLUG, providers,
                         "只带停用开关的条目该整条摘掉（留空 dict = 幽灵条目）")
        top = data.get("model") or {}
        self.assertEqual(SYNTH_SLUG, top.get("provider"), "顶层没指回内置")
        self.assertEqual(f"{SYNTH_SLUG}-fallback-model", top.get("default"),
                         "现值不在内置可服务集合里 → 该保守换成默认模型")
        for mirror in ("base_url", "api_key", "key_env"):
            self.assertNotIn(mirror, top, f"顶层镜像 {mirror} 没摘（内置自己从 env 解析）")
        self.assertEqual(env_before, _read_copy_bytes(".env"), "退回写动了 .env")

    def test_release_keeps_current_model_when_builtin_serves_it(self):
        """第 1 拍的「拿不准才换」：现值仍在内置目录里 → 逐字保留，不无谓改写。"""
        self._adopt_clone()
        self._set_top_model({"provider": CLONE_OF_SYNTH, "default": SYNTH_MODELS[-1]})
        response = self._release(CLONE_OF_SYNTH)
        self.assertEqual(200, response.status_code, response.text)
        top = _load_copy_yaml().get("model") or {}
        self.assertEqual(SYNTH_MODELS[-1], top.get("default"), "可服务的模型被换掉了")
        self.assertEqual(SYNTH_SLUG, top.get("provider"))

    def test_release_leaves_main_model_mirrors_untouched_when_not_pointing_at_clone(self):
        """h：顶层没指克隆 → ``MAIN_MODEL_MIRROR_FIELDS`` 四个键的值零改动。"""
        self._guard_before_write()
        self._adopt_clone()
        mirror = {"provider": f"{SYNTH}-other", "default": "someone/else",
                  "base_url": "https://other.invalid/v1", "key_env": "ZZ_OTHER_VAR",
                  "api_key": "zz-fake-mirror-value-not-a-real-key"}
        self._set_top_model(dict(mirror))
        response = self._release(CLONE_OF_SYNTH)
        self.assertEqual(200, response.status_code, response.text)
        self.assertIs(False, (response.json().get("released") or {}).get("moved_top_model"))
        top = _load_copy_yaml().get("model") or {}
        for field in plugin_api.MAIN_MODEL_MIRROR_FIELDS:
            self.assertEqual(mirror[field], top.get(field),
                             f"顶层镜像 {field} 被动了（它本来不指着这条克隆）")
        self.assertNotIn(CLONE_OF_SYNTH, _providers_of())

    def test_release_restores_an_existing_builtin_entry_without_clobbering_it(self):
        """第 3 拍的另一支：内置名上有真字段 → 只撤停用、其余原样留（不是整条摘掉）。"""
        self._guard_before_write()
        self._adopt_clone()
        data = _load_copy_yaml()
        data["providers"][SYNTH_SLUG] = {"enabled": False, "context_length": 4096}
        _store_copy_yaml(data)
        response = self._release(CLONE_OF_SYNTH)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual({"context_length": 4096}, _providers_of().get(SYNTH_SLUG),
                         "撤停用时顺手丢了条目上别的字段")

    def test_release_of_a_non_clone_is_refused_and_keeps_the_key_set(self):
        """i：非克隆条目 → 400 且 providers 键集合不变（含「列表未改动」承诺）。"""
        self._guard_before_write()
        victim = next((pid for pid, entry in sorted(_providers_of().items())
                       if isinstance(entry, dict) and (entry.get("base_url") or entry.get("url"))), "")
        self.assertTrue(victim, "副本里没有可用于本用例的普通条目（先按 §八 重建 harness 副本）")
        keys_before = set(_providers_of())
        config_before = _read_copy_bytes("config.yaml")
        env_before = _read_copy_bytes(".env")

        response = self._release(victim)
        self.assertEqual(400, response.status_code, response.text)
        detail = response.json().get("detail") or ""
        self.assertIn("列表未改动", detail)
        self.assertEqual(keys_before, set(_providers_of()), "被拒的退回还是删了东西")
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"), "400 写了 config")
        self.assertEqual(env_before, _read_copy_bytes(".env"), "400 写了 .env")

    def test_release_of_a_missing_entry_is_404_and_writes_nothing(self):
        """§4.3.4 末行：条目不存在 → 404（复用 M5 的定位 helper，不静默新建）。"""
        config_before = _read_copy_bytes("config.yaml")
        response = self._release(f"{SYNTH}-never-existed")
        self.assertEqual(404, response.status_code, response.text)
        self.assertNotIn(f"{plugin_api.CLONE_ID_PREFIX}{SYNTH}-never-existed", _providers_of())
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"), "404 写了盘")

    def test_release_does_not_go_through_the_official_delete(self):
        """D3：退回不走官方 delete（它会按克隆 id 去删一把从不存在于 .env 的变量）。"""
        self._adopt_clone()
        with mock.patch.object(plugin_api, "delete_custom_endpoint",
                               side_effect=AssertionError("退回不该走官方 delete")) as official:
            response = self._release(CLONE_OF_SYNTH)
        self.assertEqual(0, official.call_count)
        self.assertEqual(200, response.status_code, response.text)

    def test_release_response_carries_no_key_values(self):
        """k 的退回半边：值级比对（precheck 与真跑两份响应都判）。"""
        self._adopt_clone()
        _assert_no_key_material(self,
                                self._release(CLONE_OF_SYNTH, {"precheck_only": True}).json(),
                                "POST /endpoints/{id}/release (precheck)")
        _assert_no_key_material(self, self._release(CLONE_OF_SYNTH).json(),
                                "POST /endpoints/{id}/release")


# ═══════════════════════════════ 用例 j：克隆的常规保存（M12.23）════════════════════

class CloneRegularSaveTest(M12CopyTest):
    """接管之后的常规保存：两条通道都必须保住 ``key_env`` 与 ``managed_from``，且不写 .env。"""

    def _save_payload(self, models: List[str], model: str) -> Dict[str, Any]:
        """前端 ``buildSavePayload`` 对克隆表单的等价写法：**不带 api_key**（M12.10）。"""
        entry = _providers_of()[CLONE_OF_SYNTH]
        payload = {
            "base_url": str(entry.get("base_url") or ""),
            "discover_models": False,
            "id": CLONE_OF_SYNTH,
            "make_default": False,
            "model": model,
            "models": models,
            "name": str(entry.get("name") or CLONE_OF_SYNTH),
        }
        self.assertNotIn("api_key", payload, "克隆表单的 payload 不许提交 key（拍板 #11）")
        return payload

    def _post_save(self, payload: Dict[str, Any]):
        self._guard_before_write()
        return self.client.post(LIST_PATH, json=payload)

    def test_official_channel_save_keeps_key_env_and_the_origin_marker(self):
        """j-1 官方支（无删除）：merge 语义不碰 ``key_env``。"""
        self._adopt_clone()
        env_before = _read_copy_bytes(".env")
        payload = self._save_payload(list(SYNTH_MODELS), SYNTH_MODELS[0])
        response = self._post_save(payload)
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual(plugin_api.SAVE_CHANNEL_OFFICIAL, body.get("write_channel"),
                         "等值保存不该切到直写支")
        self.assertEqual(0, body.get("removed_models"))
        clone = _providers_of()[CLONE_OF_SYNTH]
        self.assertEqual(SYNTH_ENV_VAR, clone.get("key_env"), "官方支把 key_env 弄丢了")
        self.assertEqual(SYNTH_SLUG, clone.get(plugin_api.CLONE_ORIGIN_FIELD),
                         "官方支把来源标记弄丢了（克隆卡会变「可改密钥」）")
        self.assertEqual(env_before, _read_copy_bytes(".env"), "官方支写动了 .env")

    def test_direct_channel_save_keeps_key_env_and_the_origin_marker(self):
        """j-2 直写支（有删除）：只 pop 模型差集，``key_env`` 与来源标记都得在。"""
        self._adopt_clone()
        env_before = _read_copy_bytes(".env")
        kept = SYNTH_MODELS[:len(SYNTH_MODELS) - 1]
        payload = self._save_payload(list(kept), kept[0])
        response = self._post_save(payload)
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual(plugin_api.SAVE_CHANNEL_DIRECT, body.get("write_channel"),
                         "有删除的保存没走直写支（M9 的通道判据）")
        self.assertEqual(len(SYNTH_MODELS) - len(kept), body.get("removed_models"))
        clone = _providers_of()[CLONE_OF_SYNTH]
        self.assertEqual(SYNTH_ENV_VAR, clone.get("key_env"), "直写支把 key_env 弄丢了")
        self.assertEqual(SYNTH_SLUG, clone.get(plugin_api.CLONE_ORIGIN_FIELD))
        self.assertEqual(sorted(kept), sorted((clone.get("models") or {}).keys()))
        self.assertEqual(env_before, _read_copy_bytes(".env"), "直写支写动了 .env")

    def test_regular_save_of_a_clone_never_writes_a_second_key(self):
        """j 的密钥半边：两条通道全程 ``save_env_value`` 零调用（.env 逐字节不变）。"""
        self._adopt_clone()
        env_before = _read_copy_bytes(".env")
        with mock.patch("hermes_cli.web_routers.config_env.save_env_value",
                        side_effect=AssertionError("常规保存不该写 .env")) as env_write:
            self.assertEqual(200, self._post_save(
                self._save_payload(list(SYNTH_MODELS), SYNTH_MODELS[0])).status_code)
            kept = SYNTH_MODELS[:max(1, len(SYNTH_MODELS) - 1)]
            self.assertEqual(200, self._post_save(self._save_payload(list(kept), kept[0])).status_code)
        self.assertEqual(0, env_write.call_count)
        self.assertEqual(env_before, _read_copy_bytes(".env"))


# ═══════════ 用例 l：来源标记透传到行上（编排裁定 2026-09-26，第三处契约补丁）═══════════

class CloneOriginRowContractTest(M12CopyTest):
    """接管后**真实的 ``GET /endpoints`` 行**必须带 ``managed_from``；不该带的行必须不带。

    缺口本体（本次返工的靶子）：官方 ``_endpoint_row``（``web_routers/config_env.py:366-376``）
    是**白名单**，``_custom_endpoint_response`` 只把整条条目当 ``key_entry`` 用，条目上的
    ``managed_from`` 到不了行上 ⇒ 前端三处判据（已接管徽章 / 退回钮 / 密钥框置灰）在真列表上
    恒假。既有 34 条全走「读磁盘真相」，没有一条经过 ``endpoints()`` 的行形状，所以全体漏网。

    本类的判据一律走真实的 ``endpoints()``（含官方白名单那一段），**不**用读副本 config 的
    helper 代替 —— 替身 helper 恰好绕开缺口，钉不住这次的问题。
    """

    def _list_rows(self) -> Dict[str, Any]:
        """真实只读列表（``profile=None``，与路由同一个函数体）→ ``行 id : 行``。"""
        payload = plugin_api.endpoints(profile=None)
        self.assertIsInstance(payload, dict, "endpoints() 没返回信封 dict")
        rows: Dict[str, Any] = {}
        for row in (payload.get("endpoints") or []):
            self.assertIsInstance(row, dict)
            rows[str(row.get("id") or "")] = row
        self.assertTrue(rows, "副本里一行都没有，本判据没跑到补丁分支")
        return rows

    def _seed_legacy(self, name: str, extra: Optional[Dict[str, Any]] = None) -> None:
        """往 ``custom_providers:`` 种一条 legacy 条目，让「cc 行不碰」那一支**必然被跑到**。"""
        data = _load_copy_yaml()
        legacy = data.get("custom_providers") if isinstance(data.get("custom_providers"), list) else []
        entry: Dict[str, Any] = {"name": name, "base_url": f"https://{name}-cc.invalid/v1",
                                 "model": f"{SYNTH}/cc-{name}", "models": [f"{SYNTH}/cc-{name}"]}
        entry.update(extra or {})
        legacy.append(entry)
        data["custom_providers"] = legacy
        _store_copy_yaml(data)

    def test_adopted_clone_row_carries_the_origin_marker_on_the_real_list(self):
        """(a) 主判据：接管后克隆行 ``id == managed-<slug>`` 且 ``managed_from == <slug>``。"""
        self._guard_before_write()
        self.assertEqual([], [row for row in self._list_rows().values()
                              if plugin_api.CLONE_ORIGIN_FIELD in row],
                         "接管前就有行带来源标记（前置不成立，后面的断言会假绿）")

        self.assertEqual(200, self._adopt(SYNTH_SLUG).status_code)
        rows = self._list_rows()
        clone = rows.get(CLONE_OF_SYNTH)
        self.assertIsNotNone(clone, "接管后克隆行没出现在真实只读列表里")
        self.assertEqual(f"{plugin_api.CLONE_ID_PREFIX}{SYNTH_SLUG}", clone.get("id"))
        self.assertEqual(SYNTH_SLUG, clone.get(plugin_api.CLONE_ORIGIN_FIELD),
                         "克隆行没带来源标记 = 本次缺口（徽章 / 退回钮 / 密钥置灰三处都吃它）")
        self.assertEqual(plugin_api.PROVIDERS_ROW_SOURCE, clone.get("source"))
        self.assertEqual([CLONE_OF_SYNTH], [str(row.get("id") or "") for row in rows.values()
                          if plugin_api.CLONE_ORIGIN_FIELD in row],
                         "带来源标记的行不止克隆那一行")
        # 前端真正消费的是**过网关那份** JSON（剥密钥在 `_normalize_provider_row`、白名单在官方），
        # 直调函数对上不算数，故同一判据在 HTTP 路由上再钉一次。
        http_rows = {str(row.get("id") or ""): row for row in self._rows()}
        self.assertEqual(SYNTH_SLUG,
                         (http_rows.get(CLONE_OF_SYNTH) or {}).get(plugin_api.CLONE_ORIGIN_FIELD),
                         "GET /endpoints 的行上没带来源标记（徽章 / 退回钮 / 置灰三处恒假）")

    def test_plain_providers_and_legacy_rows_never_get_the_origin_key(self):
        """(a) 反向半边：普通 ``providers:`` 行与 cc-switch legacy 行**不含**该键。"""
        self._guard_before_write()
        # 磁盘上连 legacy 条目也写一个同名字段：证明「不碰」是判据使然，不是那段盘恰好没有
        self._seed_legacy(f"{SYNTH}-cc", {plugin_api.CLONE_ORIGIN_FIELD: SYNTH_SLUG})
        self._adopt_clone()
        rows = self._list_rows()

        plain = [row for row in rows.values()
                 if str(row.get("source") or "") == plugin_api.PROVIDERS_ROW_SOURCE
                 and str(row.get("id") or "") != CLONE_OF_SYNTH]
        self.assertTrue(plain, "副本里没有普通 providers 行，反向半边没跑到")
        for row in plain:
            self.assertNotIn(plugin_api.CLONE_ORIGIN_FIELD, row,
                             f"普通行 {row.get('id')} 被写上了来源标记（会假显「已接管」）")
        cc_rows = [row for row in rows.values()
                   if str(row.get("source") or "") == plugin_api.CC_SOURCE]
        self.assertTrue(cc_rows, "副本里没有 legacy 行，cc 分支没跑到")
        for row in cc_rows:
            self.assertNotIn(plugin_api.CLONE_ORIGIN_FIELD, row,
                             f"legacy 行 {row.get('id')} 被写上了来源标记")

    def test_blank_or_non_string_origin_marker_writes_no_key(self):
        """(b) 边界：``"   "`` 与非字符串（``7``）→ **不写这个键**；带值的 strip 后才写。"""
        self._guard_before_write()
        blank, numeric, spaced = f"{SYNTH}-blank", f"{SYNTH}-numeric", f"{SYNTH}-spaced"
        for row_id, marker in ((blank, "   "), (numeric, 7), (spaced, f"  {SYNTH_SLUG}  ")):
            self._seed_provider(row_id, {"base_url": f"https://{row_id}.invalid/v1",
                                         "model": f"{SYNTH}/m-{row_id}",
                                         plugin_api.CLONE_ORIGIN_FIELD: marker})
        rows = self._list_rows()

        for row_id in (blank, numeric):
            row = rows.get(row_id)
            self.assertIsNotNone(row, f"种子条目 {row_id} 没出行（本判据没跑到补丁分支）")
            self.assertNotIn(plugin_api.CLONE_ORIGIN_FIELD, row,
                             f"{row_id} 的来源标记形态（空白 / 非字符串）不该写出键")
        # 对照组：同一份盘上手写的**带值**标记必须透传（证明上面两条「不写键」是判据、不是没跑到）
        self.assertIsNotNone(rows.get(spaced), "对照组条目没出行")
        self.assertEqual(SYNTH_SLUG, (rows.get(spaced) or {}).get(plugin_api.CLONE_ORIGIN_FIELD),
                         "手写的来源标记没 strip 后透传")

    def test_row_contract_patch_leaks_no_key_values(self):
        """(d) 密钥铁律：打了第三处补丁的**完整列表响应**在值级上仍带不出任何密钥。"""
        self._guard_before_write()
        self._adopt_clone()
        payload = plugin_api.endpoints(profile=None)
        clone = {str(r.get("id")): r for r in (payload.get("endpoints") or [])}.get(CLONE_OF_SYNTH)
        self.assertIn(plugin_api.CLONE_ORIGIN_FIELD, clone or {},
                      "补丁没作用到行上 → 下面那条「没泄密」是句空话")
        _assert_no_key_material(self, payload, "GET /endpoints（含第三处契约补丁）")


# ═══════════════════════════════════ 用例 k：静态 lint（M12.24）═════════════════════

class M12StaticLintTest(unittest.TestCase):
    """区块标记 + 新串禁词表 + 前端三处硬判据。⚠️ 静态判据、**不是渲染证据**（prompt §5.4）。"""

    @classmethod
    def setUpClass(cls):
        cls.backend = Path(plugin_api.__file__).read_text(encoding="utf-8")
        cls.frontend = (DESKTOP_DIR / "plugin.js").read_text(encoding="utf-8")

    @staticmethod
    def _strip_js_comments(text: str) -> str:
        without_blocks = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        return re.sub(r"(?m)^[ \t]*//.*$", "", without_blocks)

    def test_m12_zone_markers_exist_and_are_appended_last(self):
        """「区块 M12」标记在两文件里都在，且都坐在他人区块**之后**（只追加纪律）。"""
        self.assertIn("区块 M12", self.backend, "plugin_api.py 里没有「区块 M12」标记")
        self.assertIn("区块 M12", self.frontend, "plugin.js 里没有「区块 M12」标记")
        self.assertGreater(self.backend.index("区块 M12"), self.backend.index("区块 M7"),
                           "M12 区块跑到了 M7 之前（不许重排他人区块）")
        self.assertGreater(self.frontend.index("区块 M12："), self.frontend.index("区块 M7："))
        self.assertGreater(self.frontend.index("区块 M12："),
                           self.frontend.index("export default {"),
                           "前端区块必须追加在文件尾（入口注册块之后）")

    def test_frozen_constant_names_are_present(self):
        """命名对测试冻结：后端四个新常量 + 三条路由函数名 + 前端只读提示常量。"""
        for name in ("BUILTIN_CANDIDATES_PATH", "BUILTIN_ROW_SOURCE", "CLONE_ID_PREFIX",
                     "CLONE_ORIGIN_FIELD", "EndpointReleaseRequest"):
            self.assertIn(name, self.backend, f"后端少了冻结常量 {name}")
        for route in ("builtin_candidates", "adopt_builtin_candidate", "release_clone"):
            self.assertIn(f"def {route}(", self.backend, f"后端路由函数名不是设计的 {route}")
        self.assertIn("CLONE_KEY_READONLY_HINT", self.frontend, "前端少了密钥只读提示常量")

    def test_new_backend_details_have_no_jargon_and_promise_untouched_list(self):
        """§4.3.4 的拒绝面：每条 detail 全过 §2.2 禁词表，含「列表未改动」承诺与动作对象。

        取数用 **AST**（与 M10 的判据同手法）：只看落在「区块 M12」里的 ``HTTPException``
        调用，``detail=`` 的字符串拼接由 ``ast.literal_eval`` 求值，注释里的字面量不算。
        """
        zone_start = self.backend.index("区块 M12（追加）")
        zone_line = self.backend[:zone_start].count("\n") + 1
        tree = ast.parse(self.backend)
        details: List[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "HTTPException" or node.lineno < zone_line:
                continue
            for keyword in node.keywords:
                if keyword.arg != "detail":
                    continue
                value = _literal_text(keyword.value)
                self.assertTrue(value, f"detail 取不到字面措辞（第 {node.lineno} 行）")
                details.append(value)
        self.assertGreaterEqual(len(details), 5,
                                f"区块 M12 的拒绝面没抓到预期数量（实到 {len(details)} 条）")
        for detail in details:
            for banned in BANNED_COPY_WORDS:
                self.assertNotIn(banned, detail, f"拒绝文案里有黑话「{banned}」：{detail[:40]}…")
            self.assertIn("列表未改动", detail, f"拒绝文案少了「列表未改动」承诺：{detail[:40]}…")
            self.assertTrue("接管" in detail or "退回" in detail,
                            f"拒绝文案没说是哪个动作被拒（动作对象）：{detail[:40]}…")

    def test_new_frontend_copy_passes_the_banned_word_table(self):
        """前端新区块的字符串常量值全过 §2.2；只读提示逐字冻结。"""
        zone = self.frontend[self.frontend.index("区块 M12："):]
        values = re.findall(r"^\s*(?:const|let)\s+[A-Z][A-Z0-9_]*\s*=\s*'([^']*)'",
                            zone, flags=re.M)
        self.assertGreaterEqual(len(values), 20, f"新区块的常量没抓到预期数量（{len(values)} 条）")
        for value in values:
            for banned in BANNED_COPY_WORDS:
                self.assertNotIn(banned, value, f"前端新文案里有黑话「{banned}」：{value[:40]}…")
        self.assertIn(CLONE_KEY_READONLY_HINT, values, "只读提示不再逐字等于冻结串")

    def test_frontend_gate_and_payload_rules_are_wired(self):
        """M12.10 的三条硬判据（静态半边）：置灰 + 强制不提交 key + 退回入口的闸门。

        ⚠️ 不是渲染证据：真机渲染属 M12.25+ 的人工项。
        """
        self.assertIn("keyReadonly: Boolean(textOf(row && row[CLONE_ORIGIN_FIELD]))",
                      self.frontend, "formFromRow 不再按来源标记判定密钥只读")
        form_body = self.frontend[self.frontend.index("function FormBody"):
                                  self.frontend.index("const FIELD_CONTEXT_HINT",
                                                     self.frontend.index("function FormBody"))]
        self.assertIn("disabled: form.keyReadonly === true", form_body, "克隆表单的密钥框没置灰")
        self.assertIn("hint: form.keyReadonly === true ? CLONE_KEY_READONLY_HINT : apiKeyHint",
                      form_body, "克隆表单没给只读提示")
        payload = self.frontend[self.frontend.index("function buildSavePayload"):
                               self.frontend.index("\n}\n", self.frontend.index("function buildSavePayload"))]
        self.assertIn("api_key: form.keyReadonly === true ? undefined", payload,
                      "buildSavePayload 没对克隆表单强制不提交 key")
        card = self.frontend[self.frontend.index("function ProviderCardActions"):
                            self.frontend.index("function ProviderCard({")]
        self.assertIn("isAdoptedCloneRow(row)", card, "退回入口没有只在克隆行渲染的闸门")
        self.assertEqual(2, card.count("isProvidersSource(row)"),
                         "M5/M7 的 providers 闸门份数被改动了（M12 不许新增第三个调用点）")
        self.assertIn("BuiltinCandidateCard", self.frontend)
        self.assertIn("useBuiltinCandidatesQuery", self.frontend)

    def test_backend_zone_never_touches_env_writing_helpers(self):
        """.env 零写入的设计证明点：新区块不**调用**任何 .env 写/删函数，
        ``env_written`` 也只可能是字面 ``False``（docstring 里提官方那三支是说明，不算调用）。"""
        zone = self.backend[self.backend.index("区块 M12（追加）"):]
        code = re.sub(r'"""[\s\S]*?"""', "", re.sub(r"(?m)^#.*$", "", zone))
        for helper in ("save_env_value", "remove_env_value", "delete_custom_endpoint"):
            self.assertIsNone(re.search(rf"\b{helper}\s*\(", code),
                              f"新区块调用了 {helper}()（.env 或官方删除都不该出现在这里）")
        self.assertIn('"env_written": False', code, "接管回执少了字面 False 的自证")
        written = re.findall(r'"env_written":\s*([^,\n}]+)', code)
        self.assertEqual(["False"], written,
                         f"env_written 必须是**唯一一处字面 False**，实到 {written}")
        # 改动记录：2026-09-26 R4（M12.4/M12.5 复核）——两处落盘现在坐在
        # `_config_profile_scope(profile)` **里面**（M5/M6 同口径：官方 save_config 的路径由
        # 作用域解析，写在作用域外会让带 profile 的请求落到默认配置文件上）。本判据的本意
        # 不变 = 「接管与退回各只写一次、写点不增多」，故把缩进敏感的 needle 放宽一档，
        # 另加一条钉「两条都在 profile 作用域内」。
        sites = re.findall(r"^\s*save_config\(cfg\)", code, flags=re.M)
        self.assertEqual(2, len(sites),
                         f"接管与退回应当各只有一次 save_config（实到 {len(sites)} 个直写点）")
        scoped = re.findall(r"with _config_profile_scope\(profile\):\n\s*save_config\(cfg\)",
                            code)
        self.assertEqual(2, len(scoped),
                         "接管/退回的落盘必须在 profile 作用域内（否则带 profile 的请求写错文件）")

    def test_m1_row_contract_is_not_extended_for_candidates(self):
        """独立性判据：候选走独立 GET，**不**并入 M1 行契约（``test_endpoints.py`` 逐字段钉死）。"""
        zone = self.backend[self.backend.index("区块 M12（追加）"):]
        self.assertNotIn("@router.get(\"/endpoints\")", zone)
        self.assertNotIn("def endpoints(", zone)
        self.assertIn(plugin_api.BUILTIN_CANDIDATES_PATH, zone)


# ═══════════ 用例 m：契约补丁两端的成对静态判据（防单边漂移；编排裁定 2026-09-26）═════

class CloneOriginContractPairLintTest(unittest.TestCase):
    """前端读 ``row[CLONE_ORIGIN_FIELD]`` ⇔ 后端只读列表调第三处补丁，**两处必须同时在场**。

    单边改掉的两种事故：删后端那一行调用 → 前端三处判据恒假（= 本次的真实缺口）；
    只动前端 → 判据与 M1 行契约脱钩，之后没人会再补回来。静态判据、**不是渲染证据**。
    """

    @classmethod
    def setUpClass(cls):
        cls.backend = Path(plugin_api.__file__).read_text(encoding="utf-8")
        cls.frontend = (DESKTOP_DIR / "plugin.js").read_text(encoding="utf-8")

    def _list_source(self) -> str:
        """只取 M1 只读列表**那一个函数**的源码（别处的同名调用不算数）。"""
        for node in ast.walk(ast.parse(self.backend)):
            if isinstance(node, ast.FunctionDef) and node.name == "endpoints":
                segment = ast.get_source_segment(self.backend, node) or ""
                self.assertTrue(segment, "取不到只读列表的函数体")
                return segment
        raise AssertionError("后端没有名为 endpoints 的函数（M1 骨架被改名了？）")

    def test_frontend_reader_and_backend_patch_call_are_both_present(self):
        """(c) 主判据：前端三处判据与后端那一行调用**成对**在场。"""
        frontend_reads = self.frontend.count("row[CLONE_ORIGIN_FIELD]")
        patch_call = self._list_source().count("_apply_clone_origin(")
        self.assertGreaterEqual(frontend_reads, 3,
                                f"前端读来源标记的判据少于三处（实到 {frontend_reads}）")
        self.assertEqual(1, patch_call,
                         "只读列表里必须**恰好一次**第三处契约补丁调用（0 = 本次缺口，>1 = 重复打）")
        self.assertEqual(frontend_reads > 0, patch_call > 0,
                         "前端判据与后端契约补丁单边漂移（一边在、一边没）")

    def test_patch_sits_at_the_tail_of_the_contract_chain_and_in_the_m12_zone(self):
        """形状半边：补丁排在两处既有补丁**之后**（不许插队），helper 归属 M12 区块。"""
        body = self._list_source()
        self.assertGreater(body.index("_apply_clone_origin("), body.index("_apply_allowlist_models("),
                           "第三处补丁必须排在两处既有补丁之后（不许重排他人补丁）")
        self.assertEqual(3, len(re.findall(r"_apply_\w+\(rows, raw_cfg\)", body)),
                         "「只加字段」补丁链份数漂移（M12 不许新增第四处）")
        head, zone = self.backend.split("区块 M12（追加）", 1)
        self.assertIn("def _apply_clone_origin(", zone, "helper 必须坐在 M12 区块里（不许回填他人区块）")
        self.assertNotIn("def _apply_clone_origin(", head, "helper 在 M12 之前还有一份（重复定义 / 越界）")


if __name__ == "__main__":
    unittest.main()
