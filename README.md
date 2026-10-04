# supplier-models —— Hermes 桌面端「供应商与模型」插件

给 Hermes 桌面端加一个页面，用来统一管 Hermes 的模型供应商与模型白名单：把官方设置页看不到的
`custom_providers:`（cc-switch 按旧 schema 写回的 legacy 条目）收进同一个列表、提供「钉住白名单 /
清空白名单 / 设为当前 / 迁移进 `providers:` / 接管内置供应商」等动作。

**本仓现状**：终版代码的补档，**未在现行环境安装运行**（当前 `%LOCALAPPDATA%\hermes\plugins\` 为空）。
代码从 2026-10-02 的 home 归档原样入库，13 件源码/测试逐文件 md5 与归档一致。

---

## 一、目录结构

| 路径 | 作用 |
|---|---|
| `plugin.yaml` | 插件清单，只有 `name` / `version` / `description` 三个字段 |
| `desktop/plugin.js` | 桌面端半边：页面组件、侧栏行、命令面板项、全部 UI 与交互（未编译 ESM） |
| `dashboard/manifest.json` | 后端半边入口声明：`{"name":"supplier-models","api":"plugin_api.py"}` |
| `dashboard/plugin_api.py` | FastAPI 路由，11 条，挂在 `/api/plugins/supplier-models/` 下 |
| `tests/` | stdlib `unittest`，8 个测试文件 + `__init__.py` |

版本号口径：`plugin.yaml` 里是 `0.1.0`（清单版本）。日常说的「v0.0.3」是**需求轮次号**（R1–R4 那一轮），
不是清单版本，别把两者混用。

## 二、后端接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/endpoints` | 归一化后的供应商列表（`providers:` + legacy `custom_providers:` 两类同构行） |
| POST | `/endpoints/validate` | 探测候选模型（base_url + key 打网络，不落盘） |
| POST | `/endpoints` | 保存条目（新增 / 编辑，走官方 upsert 或插件直写两条路径） |
| POST | `/endpoints/{endpoint_id}/pin` | 钉住（置 `discover_models: false`，不动 `models:` 内容） |
| POST | `/endpoints/{endpoint_id}/clear-models` | 清空白名单（原子写，保留每项元数据） |
| POST | `/endpoints/migrate` | 把 legacy 条目迁进 `providers:`（cc-switch 条目收编） |
| POST | `/endpoints/{endpoint_id}/activate` | 设为当前供应商（写顶层 `model.default`） |
| DELETE | `/endpoints/{endpoint_id}` | 删除条目 |
| GET | `/endpoints/builtin-candidates` | 可接管的内置供应商候选 + 是否已配密钥 |
| POST | `/endpoints/{slug}/adopt` | 接管内置供应商（克隆转正为 `managed-<slug>`） |
| POST | `/endpoints/{endpoint_id}/release` | 退回官方管理（关掉克隆条目） |

legacy 行的 id 统一带 `cc:` 前缀，避免与 `providers:` 条目撞 id。

## 三、写插件代码前必须知道的硬边界

桌面端按**未编译 ESM** 加载磁盘插件，三条不可违反：

1. 不能写 JSX 语法，UI 一律用 `react/jsx-runtime` 的 `jsx()` / `jsxs()`；
2. 只能 import 三个 specifier：`@hermes/plugin-sdk`、`react`、`react/jsx-runtime`（应用源码的 `@/…`
   私有模块拿不到）；
3. 零硬编码颜色，全部走 `var(--ui-*)` 主题 token。

另外两条本机实测：

- 磁盘插件**用不了 Tailwind 类**（应用只编译自己源码树里的类名）。要 `:hover`、`::-webkit-scrollbar`
  这类内联 style 表达不了的样式，注入一次性 `<style>` + 前缀类（本插件用 `sm-page` / `sm-row`）。
- 页面本体必须同时注册三条贡献：`ROUTES_AREA`（页面）、`SIDEBAR_NAV_AREA`（侧栏，data 要 `path` +
  `label` + `codicon` 三字段）、`PALETTE_AREA`（命令面板）。少了 `ROUTES_AREA` 侧栏那行点不开。

## 四、要让它跑起来：前置条件

| # | 条件 | 说明 |
|---|---|---|
| 1 | **应用侧补丁** | hermes-agent 仓提交 `84941ad08d`（`routes.ts` 把瓦片形态泛化到所有插件路由 + `runtime-loader.ts` 的 import 正则加词边界）。缺后者时加载器会把 `'managed_from'` 这类以 `from` 结尾的字符串误读成 import 语句，**插件整个加载失败**；升级桌面端后要重打，判据见应用仓 `apps/desktop/PATCH-supplier-tile.md` |
| 2 | 启用开关 | `config.yaml` 的 `plugins.enabled` 里要有 `supplier-models`，且不在 `plugins.disabled`（否则后端半边根本不被 import） |
| 3 | **重启才生效** | 改 `desktop/plugin.js` 或 `plugin_api.py` 后，Ctrl+K 的 Reload desktop plugins **不可靠**（渲染进程持文件锁时 reconcile 的 rm -rf 半途失败且错误被吞）。后端由独立常驻的 gateway 进程加载，退出桌面端不会重启它 |
| 4 | 安装位置 | 本目录整体放到 `$HERMES_HOME/plugins/supplier-models/`。`$HERMES_HOME/desktop-plugins/<name>/` 是应用自动复制出来的产物，**不要手改**——带 `.hermes-package.json` 标记的目录在应用更新时会被重建，手改必丢 |

## 五、跑测试

运行器是 stdlib `unittest`（venv 里没有 pytest，也不装）。破坏性用例只碰 `HERMES_HOME` 的**临时副本**：

```bash
cd "D:/hermeswork/supplier-models-plugin"          # 必须 cd 到含 tests/ 的目录，discover 依赖 cwd
V="H:/application/hermesnew/hermes-agent/venv/Scripts/python.exe"
T="$LOCALAPPDATA/Temp/supplier-models-harness"
rm -rf "$T" && mkdir -p "$T"
cp <一份 config.yaml> "$T/config.yaml"
cp <一份 .env>        "$T/.env"
HERMES_HOME="$(cygpath -w "$T")" "$V" -m unittest discover -s tests -v
rm -rf "$T"
```

三条注意事项：

- **副本源必须是「还留有 legacy `custom_providers:` 条目」的状态**。多个用例的 `setUp` 会以副本里
  真实存在的 legacy/`providers:` 条目作取材对象并断言其存在；喂一份 `custom_providers: []` 的副本会
  有成批 `setUp` 前置断言失败，那是**输入不匹配，不是代码缺陷**。本机 2026-09-24 那对基线备份已随
  Temp 清理丢失，现存的两份 10-02 归档都是迁移后的 `custom_providers: []`，需要另找或手工构造副本。
- **副本里含明文密钥**，跑完务必删掉临时目录，别留在盘上；断言一律走 sha256 / 存在性比较，
  不许把 key 值打印进测试输出。
- **真配置只读**。任何用例都不许写 `H:\application\hermesnew\home\` 或现行 `%LOCALAPPDATA%\hermes\`
  下的 `config.yaml` / `.env`。

## 六、已知边界与坑（设计使然，别当 bug 改）

- 官方配置写入器对 `models:` **只增不减**，还会把默认模型强制折回名单。所以「保存即所得」的写法是：
  本次有删除就走插件自己的直写路径（原子写、保留每项元数据、先备份），无删除才走官方 upsert；
  **回执里报「本次删除 N 个模型」是唯一护栏**，不加确认弹窗。存量残留不自动清理。
- 白名单可以为空，但模型池永远至少含默认模型；删掉默认模型时名单第一个自动升为默认。
- 规范内置供应商（anthropic / openai / gemini / openrouter）**不吃 `providers:` 覆盖**——运行时在扫配置
  之前就短路了，唯一对内置名生效的字段是 `providers.<name>.enabled: false`。所以「接管内置」走的是
  克隆转正：造一个 `managed-<slug>` 条目 + 关掉内置。克隆卡的 API Key 输入框置灰禁改，密钥沿用官方
  既有环境变量，杜绝两份真相。
- 钉住不提供「解除」入口，解除只能去官方页。
- **cc-switch 是独立第三方 App，自己读并缓存 Hermes 的 `config.yaml`**。迁移后必须彻底退出 cc-switch
  进程再重开才会显示只读态；在它没重载的状态下保存，会把条目写回 `custom_providers:`、**冲掉迁移**。
  插件管不着 cc-switch 怎么显示。

## 七、改动前的一把锁

`tests/` 里有对源码做**静态切片**断言的用例（`test_activate_delete.py` 的 `_slice` 锚点钉的是「全卡唯一
写入口 + 无死占位」）。要重命名 `plugin.js` / `plugin_api.py` 里的函数或动标记字符串，先查这些锚点，
否则会红一片。

## 八、交付状态

2026-09-24 人工清单全清后收束：**F/T 38 项 = 通过 31 / 结案（非缺陷）5 / 未跑待人工 2 / 未通过 0 / 阻塞 0**。
两项未跑均不阻塞：T13/5e（用户裁定不跑 `hermes update`，它会动整个安装）、F6（移除单个模型后选择器消失的
GUI 子句，机制同已结案的 F4）。台账在 `D:\hermeswork\doc\providerchange\tasks\`（权威计数只在
`m8-integration-verification.md` §十一 与 `progress.md` §十九，过程快照数字别引用）。

安全提示：迁移核验时 sensenova 的明文 key 曾进过命令行输出，若那段会话内容外发过，建议轮换该 key。
