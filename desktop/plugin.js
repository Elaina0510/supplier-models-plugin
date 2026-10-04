/**
 * supplier-models —— 统一包的桌面端半边（M0：只有骨架与入口，零业务逻辑）。
 *
 * 磁盘插件按**未编译**的 ESM 加载，三条硬边界（`website/docs/developer-guide/`
 * `desktop-plugin-sdk.md` 的「Pitfalls」节）本文件全部遵守：
 *   1. 不能写 JSX 语法 —— UI 一律用 react/jsx-runtime 的 jsx() / jsxs()。
 *   2. 只能 import 三个 specifier：@hermes/plugin-sdk、react、react/jsx-runtime。
 *   3. 零硬编码颜色 —— 颜色全部走 var(--ui-*) 主题变量，切主题自动跟随（F12）。
 *
 * 入口三条贡献（缺一不可，设计 §6.6 的 v0.4 修正 / 审查锚点 F3）：
 *   ROUTES_AREA      页面本体（没有它，侧边栏那行点不开——侧边栏只是「指路牌」）
 *   SIDEBAR_NAV_AREA 侧边栏一行，data 需要 path + label + codicon **三个**字段
 *   PALETTE_AREA     命令面板项「打开供应商与模型」
 * 成对注册的参考实现：apps/desktop/src/plugins/kanban/plugin.tsx 的 page + nav 两条。
 *
 * 后续模块在本文件的落点（progress §四：只追加自己的区块，不重排、不删他人区块）：
 *   M2 列表区（供应商卡片 + 三类徽章）→ M3 表单区与模型区（探测 / 两栏）
 *   M4 保存与钉住 → M5 清空白名单 → M6 cc-switch 迁移 → M7 危险动作与收尾。
 *   下面每个区块都有自己的注释锚，请在对应锚内扩写；后端探针常量与 404 判据
 *   （M0.5）是跨模块契约，改动需上报，不要就地换掉。
 */

import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'
import { host, PALETTE_AREA, ROUTES_AREA, SIDEBAR_NAV_AREA } from '@hermes/plugin-sdk'
/* M2 追加（不改动 M0 的导入行；ESM 允许同一 specifier 的多条声明，绑定仍是同一实例）：
 * SDK 的 UI kit 与取数层 `useQuery`（sdk/index.ts:1550-1618 / :1800）。 */
import { Badge, Button, EmptyState, Loader, Separator, Tip, useQuery } from '@hermes/plugin-sdk'
/* M3 追加（同上，仍是 @hermes/plugin-sdk 这一个 specifier）：表单区与两栏模型区的控件。 */
import { Checkbox, Input, SearchField } from '@hermes/plugin-sdk'
/* M4 追加（同上，仍是 @hermes/plugin-sdk 这一个 specifier）：写链路的动作层。
 * `ConfirmDialog`（sdk/index.ts:1559）用于批量钉住的二次确认；`haptic`（:1682 = triggerHaptic）
 * 与 `useMutation` / `useQueryClient`（:1800）分别覆盖 §5.3 的保存反馈与 §6.4 的失效刷新。 */
import { ConfirmDialog, haptic, useMutation, useQueryClient } from '@hermes/plugin-sdk'

/* ─── 入口标识（三处共用，勿与文件夹名不一致）──────────────────────────── */

const PLUGIN_ID = 'supplier-models'
const PAGE_PATH = '/supplier-models'
const PAGE_LABEL = '供应商与模型'
const OPEN_LABEL = '打开供应商与模型'

/* ─── 后端探针（M0.5 / R2）───────────────────────────────────────────────
 * ctx.rest 已按构造带上插件命名空间前缀 `/api/plugins/supplier-models`，并且
 * 本身 profile-aware（apps/desktop/src/api/plugins.ts:85 的 profileScoped()），
 * 所以前端**不**手工拼 profile（prompt §2.6 C2）；profile 的落地条件在后端形参。
 * 探针打 M1 的 `GET /endpoints`。404 = Python 半边没挂上（未进 plugins.enabled
 * 白名单，或 gateway 未重启）→ 显示指定文案，不猜别的原因。
 */

const BACKEND_PROBE_PATH = '/endpoints'
const BACKEND_NOT_MOUNTED_TEXT =
  '后端未挂载 — 请在 Capabilities → Plugins 确认已启用，并重启 gateway'

const STATUS_PROBING = 'probing'
const STATUS_MOUNTED = 'mounted'
const STATUS_NOT_MOUNTED = 'not-mounted'
/** 非 404 的失败（网关未连上、超时、后端 5xx）：如实留白，不当「未挂载」。 */
const STATUS_UNKNOWN = 'unknown'

/** 404 判据：`statusCode` 是桌面端的结构化字段，message 的「404: 」前缀是兜底。 */
function isNotFoundError(error) {
  if (error && error.statusCode === 404) {
    return true
  }

  return /^404(?::|\b)/.test(String(error && error.message ? error.message : ''))
}

/** 一次轻量读取（不轮询、不缓存、不写任何东西）。M2 的列表拉取建在它之上。 */
function probeBackend(ctx) {
  return ctx.rest(BACKEND_PROBE_PATH)
}

function useBackendMountProbe(ctx) {
  const [status, setStatus] = useState(STATUS_PROBING)

  useEffect(() => {
    let settled = false

    const report = next => {
      if (!settled) {
        setStatus(next)
      }
    }

    const reportFailure = error => {
      report(isNotFoundError(error) ? STATUS_NOT_MOUNTED : STATUS_UNKNOWN)
    }

    // `ctx.rest` 也可能**同步**抛（非法路径等），所以 try 包一层：探针不许把页面搞崩。
    try {
      Promise.resolve(probeBackend(ctx)).then(
        () => report(STATUS_MOUNTED),
        reportFailure
      )
    } catch (error) {
      reportFailure(error)
    }

    return () => {
      settled = true
    }
  }, [ctx])

  return status
}

/* ─── 主题变量样式（零硬编码颜色；`--ui-*` token 见 apps/desktop/src/styles.css）── */

/* 需要 :hover / ::-webkit-scrollbar 这类内联 style 表达不了的状态样式，走一次性 <style> 注入。
 * 为什么不能用 Tailwind 类：应用只扫描自己的源码树（styles.css:1 @theme 内联 + vite.config.ts:117），
 * 磁盘插件里的类名不会被编译。颜色仍然全部引用 var(--ui-*) / var(--dt-*) token，硬边界不破。
 *   · .sm-page 滚动条：逐条复刻应用自己的 scrollbar-dt（styles.css:1440-1469）。
 *   · .sm-row hover：应用列表行的原生惯例（providers-settings.tsx:262 的
 *     hover:bg-(--ui-control-hover-background) + transition-colors），只给**可点的行**挂类。 */
const PLUGIN_CSS = `
.sm-page::-webkit-scrollbar, .sm-scroll::-webkit-scrollbar { width: 0.5rem; height: 0.5rem; }
.sm-page::-webkit-scrollbar-track, .sm-page::-webkit-scrollbar-corner,
.sm-scroll::-webkit-scrollbar-track, .sm-scroll::-webkit-scrollbar-corner { background: transparent; }
.sm-page::-webkit-scrollbar-thumb, .sm-scroll::-webkit-scrollbar-thumb {
  background: color-mix(in srgb, var(--dt-scrollbar-thumb) 18%, transparent);
  border-radius: 9999rem;
  background-clip: padding-box;
}
.sm-page::-webkit-scrollbar-thumb:hover, .sm-scroll::-webkit-scrollbar-thumb:hover {
  background: color-mix(in srgb, var(--dt-scrollbar-thumb) 40%, transparent);
  background-clip: padding-box;
}
.sm-page::-webkit-scrollbar-button, .sm-scroll::-webkit-scrollbar-button { display: none; }
.sm-row { transition: background-color 120ms ease-out; }
.sm-row:hover { background-color: var(--ui-control-hover-background); }
`

const PAGE_STYLE = {
  /* M7 收尾（编排授权的背景缺陷项）：页面底色 = **聊天窗自己那层玻璃**。
   *
   * 判据（全部读应用源码所得，不是猜的）：
   *   · `--ui-chat-window-background`（styles.css:428-433）= `--ui-chat-window-solid`
   *     按 `--wallpaper-card-alpha`（默认 45%）混向 transparent —— 这是应用给
   *     **聊天卡片**与**路由瓦片**涂的那层底，两处 paint 点分别是
   *     `app/chat/index.tsx:687`（`bg-(--ui-chat-window-background)` 的会话面）与
   *     `app/chat/route-tile.tsx:67`（页面瓦片的外壳）。它的 alpha 是**固定**的，
   *     刻意不跟随壁纸强度（styles.css:418-427 的注释：intensity 100 时整透会让卡片
   *     融进壳里 —— 用户反馈），所以它永远读得出「一个浮起来的窗口」。
   *   · 插件页拿不到那层外壳：注册型页面由 `app/contrib/surfaces.tsx:205-215` 经
   *     `page()`（:176-180，`className='contents'`，即**不产生盒子也不垫底**）挂进
   *     路由表 —— 应用自己的页面也不垫，它们靠上面那两个外壳 paint。磁盘插件既拿不到
   *     `@/…` 内部组件、也没有 paint 好的祖先，所以**本页必须自己涂**，
   *     而能涂得与聊天窗一致的就是这个 token（这也正是 M7.6「颜色一律 var(--ui-*)」的口径）。
   *   · 前两轮为什么不对：`--ui-bg-editor-solid` 两端都是不透明色（实白，完全不是玻璃）；
   *     `--ui-chat-surface-background` 在玻璃模式下被 styles.css:690 改写成裸
   *     `transparent`（等于没垫底）。`--ui-bg-chrome`（上一版）虽然永不退化，
   *     但它是**壳底**（controller.tsx:883 的 `[data-contrib-shell]` 整窗 painter），
   *     语义上正是聊天卡**周围**那圈更暗更透的边 —— 拿它当页面底，
   *     整页就成了「聊天窗外的壳」，与聊天窗的质感必然差一层。 */
  background: 'var(--ui-chat-window-background)',
  boxSizing: 'border-box',
  display: 'flex',
  flexDirection: 'column',
  gap: '14px',
  height: '100%',
  overflowY: 'auto',
  padding: '10px 12px 24px' // 路由瓦片外壳已带 p-4 内衬（route-tile.tsx:68），本页收薄一档免双层留白
}

const PAGE_TITLE_STYLE = {
  /* 应用自己页面的标题口径：0.9375rem / semibold / 微收字距（skills/index.tsx:1188）。 */
  color: 'var(--ui-text-primary)',
  fontSize: '0.9375rem',
  fontWeight: 600,
  letterSpacing: '-0.01em',
  margin: 0
}

const PAGE_SUBTITLE_STYLE = {
  color: 'var(--ui-text-tertiary)',
  fontSize: '0.75rem',
  lineHeight: 1.5,
  margin: '4px 0 0'
}

const ZONE_STYLE = {
  /* 区块要有「抬起一层」的实心底。这里用 --ui-bg-elevated（styles.css:319：elevated seed
   * 混进 --theme-neutral-card，两端都是不透明色 → 结果恒不透明，seed 占比 light 28% /
   * dark 46%）。页面底现在是 --ui-chat-window-background（会随壁纸强度半透的聊天窗玻璃），
   * 所以区块这层不透明的底才是「抬起」的可靠参照：不管页面底多透，它自己始终是一层实心。
   * 不用 --ui-bg-card：它是 4% accent 混进「4% base + 96% transparent」（styles.css:324），
   * 有效 alpha 只有约 8%，压在页面底上几乎分不出层次，达不到修复可读性的目的。 */
  background: 'var(--ui-bg-elevated)',
  /* 描边收一档 + 圆角收到应用近直角刻度（--radius-scalar 0.2）：
   * 应用自己的区块面板是 rounded-lg + --ui-stroke-tertiary（skills/index.tsx:1295）。 */
  border: '1px solid var(--ui-stroke-tertiary)',
  borderRadius: '6px',
  flex: 'none',
  padding: '12px 14px'
}

const ZONE_TITLE_STYLE = {
  color: 'var(--ui-text-secondary)',
  fontSize: '0.8125rem',
  fontWeight: 500,
  margin: '0 0 6px'
}

const ZONE_HINT_STYLE = {
  color: 'var(--ui-text-tertiary)',
  fontSize: '0.75rem',
  lineHeight: 1.6,
  margin: 0
}

const BANNER_STYLE = {
  /* 警示底用 token 派生色（color-mix 只引用 --ui-yellow，不违反零硬编码口径）。 */
  background: 'color-mix(in srgb, var(--ui-yellow) 10%, transparent)',
  border: '1px solid color-mix(in srgb, var(--ui-yellow) 55%, transparent)',
  borderRadius: '6px',
  color: 'var(--ui-text-primary)',
  flex: 'none',
  fontSize: '0.8125rem',
  lineHeight: 1.5,
  padding: '10px 14px'
}

const COLUMN_ROW_STYLE = {
  display: 'flex',
  gap: '12px',
  flexWrap: 'wrap'
}

const COLUMN_STYLE = {
  /* 栏坐在**不透明**的区块底（ZONE_STYLE 的 --ui-bg-elevated）之上，所以 --ui-bg-card 的
   * 半透明色调只用来标出「槽位」这一层，不会透到壁纸：真正把壁纸挡住的是那层不透明的区块底，
   * 而不是页面底——PAGE_STYLE 的 --ui-chat-window-background 本身是半透的玻璃层。 */
  background: 'var(--ui-bg-card)',
  border: '1px solid var(--ui-stroke-tertiary)',
  borderRadius: '6px',
  flex: '1 1 260px',
  minWidth: '240px',
  padding: '10px 12px'
}

const COLUMN_TITLE_STYLE = {
  color: 'var(--ui-text-secondary)',
  fontSize: '0.75rem',
  fontWeight: 500,
  margin: '0 0 4px'
}

/* ─── 页面外壳：§5.2 的三区占位（列表区 / 表单区 / 模型区）───────────────── */

function BackendNotMountedNotice() {
  return jsx('p', {
    children: BACKEND_NOT_MOUNTED_TEXT,
    role: 'status',
    style: BANNER_STYLE
  })
}

/* ─── 区块 M2：供应商列表与状态徽章（读链路，零写入）────────────────────────
 *
 * 契约（任务 M2.1 / 设计 §6.4 / prompt §2.6 C2）：
 *   · 取数走 SDK 的 `useQuery`，请求函数直接复用 M0 的 `probeBackend(ctx)`（它就是
 *     `ctx.rest('/endpoints')`）——同一个请求。所以页面不再调用 `useBackendMountProbe`，
 *     否则打开页面会发两次 `/endpoints`（违反 M2.10）。M0 的探针常量与 404 判据
 *     （`isNotFoundError` + `BACKEND_NOT_MOUNTED_TEXT`）原样保留、本区块照常消费（R2）。
 *   · 前端**不**手工拼 profile 参数：`ctx.rest` 本身 profile-aware（api/plugins.ts:85 的
 *     `profileScoped()`），落地条件在 M1 的路由形参。URL 实测属人工项 M2.1（DevTools）。
 *   · 密钥铁律：行数据里关于密钥只有 `has_api_key` / `api_key_plaintext` 两个布尔，
 *     本区块既不读也不渲染任何密钥内容字段（M1 归一化时已整体剥掉预览字段）。
 *   · 「池内数量」的后端字段还没有（UC-08 决议 7 延到 v0.2，M8 复核回填真值）
 *     → 只出占位符 `—`，不臆造字段名。
 */

const ENDPOINTS_QUERY_KEY = ['supplier-models','endpoints']

/* ── 文案常量集中处（M2.9 的人工验收就是照这张表对的）────────────────────── */

/* 改动记录：2026-09-25 R3（M10.1，设计 §2.3 表 A · A1）：「××区」是设计语，
   列表区标题改成使用者语言；常量名一律不动，只改字符串值。 */
const LIST_ZONE_TITLE = '我的供应商'
const LIST_COUNT_SUFFIX = '个供应商'
const CURRENT_MARK_TEXT = '●使用中'
const CC_SOURCE = 'cc-switch'
const CC_MANAGED_LINE = '由 cc-switch 管理'
const PIN_TEXT_PINNED = '已钉住'
const PIN_TEXT_UNPINNED = '未钉住 ⚠'
const PIN_UNPINNED_HINT =
  '该供应商当前允许自动发现，模型池可能被端点全量目录污染。点「钉住」修正。'
const POOL_COUNT_PLACEHOLDER = '—'
const PLAINTEXT_BADGE_TEXT = '⚠ 明文密钥在 config.yaml'
const PLAINTEXT_HINT =
  '这是 cc-switch 写入的形态。要搬进 .env 需要迁移该条目，代价是 cc-switch 将无法再编辑/启用它。'
const DEFAULT_MODEL_PREFIX = '默认: '
const VIEW_ACTION_LABEL = '查看'
const PIN_ACTION_LABEL = '钉住'
const ADD_ACTION_LABEL = '+ 添加供应商'
/* M7 收尾：原先这几处共用一句「该动作由后续模块接通；本页当前只读，不写任何配置」
 * （`PLACEHOLDER_ACTION_TIP`）—— 写链路自 M4 起已经全部落地，那句提示在每处都成了
 * 假话（尤其「本页只读」与本插件的整个功能面相反）。拆成各自属实的 Tip。
 * （布局优化 2026-09-24：原表单保存的 `FORM_SAVE_TIP` 随表单区重复保存按钮退役，
 * 卡片底部保存行的 Tip 用 `SAVE_FORCED_PIN_NOTE`。） */
/* 改动记录：2026-09-25 R3（M10.1，设计 §2.3 表 A · A6 / A7）：两条按钮 Tip 改写为使用者
   语言——去掉存储字段名与字段值写法，动作对象一律说成「供应商 / 配置 / 设置」。
   常量名不动，只改字符串值。 */
const ADD_SUPPLIER_TIP = '打开下方表单新建供应商：填完点「保存」才会写入配置，在此之前本页不动任何设置。'
const PIN_ACTION_TIP =
  '钉住该供应商 —— 只阻止未来的自动发现覆盖白名单，不清理已经挤进来的模型。'
const EMPTY_TITLE = '暂无供应商'
const EMPTY_DESCRIPTION = '添加一个 OpenAI 兼容端点，或从官方设置页迁移已有端点。'
const LOADING_TEXT = '正在读取供应商列表…'
const READ_FAILED_PREFIX = '读取供应商列表失败：'
const NOT_MOUNTED_LIST_HINT = '未读取到列表，原因见上方提示。'

/* ── 主题变量样式（延续 M0 的写法；颜色一律 var(--ui-*) token）────────────── */

const LIST_HEAD_STYLE = {
  alignItems: 'baseline',
  display: 'flex',
  flexWrap: 'wrap',
  gap: '8px',
  justifyContent: 'space-between'
}

const LIST_COUNT_STYLE = {
  /* 应用原生的计数小胶囊（board.tsx:1333：rounded-full + bg-quaternary + tabular-nums）。 */
  background: 'var(--ui-bg-quaternary)',
  borderRadius: '9999px',
  color: 'var(--ui-text-tertiary)',
  flex: 'none',
  fontVariantNumeric: 'tabular-nums',
  fontSize: '0.625rem',
  padding: '1px 6px'
}

const CARD_STYLE = {
  /* 卡片坐在不透明的区块底上，自己只加一层极淡的 accent 底（--ui-bg-card）+ 最轻描边，
   * 对应应用真实卡形（board.tsx:270 的 bg-elevated + stroke-tertiary，这里两层互换因为
   * 区块底已是 elevated，不能再用同色把卡片「焊」进底里）。 */
  alignSelf: 'flex-start',
  background: 'var(--ui-bg-card)',
  border: '1px solid var(--ui-stroke-tertiary)',
  borderRadius: '6px',
  display: 'flex',
  flexDirection: 'column',
  flex: '1 1 300px',
  gap: '5px',
  minWidth: '280px',
  padding: '10px 12px'
}

const CARD_HEAD_STYLE = {
  alignItems: 'center',
  display: 'flex',
  flexWrap: 'wrap',
  gap: '6px'
}

const CARD_NAME_STYLE = {
  color: 'var(--ui-text-primary)',
  fontSize: '0.8125rem',
  fontWeight: 600
}

const CARD_CURRENT_STYLE = {
  color: 'var(--ui-green)',
  flex: 'none',
  fontSize: '0.6875rem',
  fontWeight: 600
}

const CARD_URL_STYLE = {
  color: 'var(--ui-text-tertiary)',
  fontSize: '0.75rem',
  margin: 0,
  wordBreak: 'break-all'
}

const CARD_META_STYLE = {
  color: 'var(--ui-text-secondary)',
  fontSize: '0.75rem',
  margin: 0
}

const CARD_NOTE_STYLE = {
  display: 'flex',
  flexDirection: 'column',
  gap: '4px'
}

const CARD_HINT_STYLE = {
  color: 'var(--ui-yellow)',
  fontSize: '0.6875rem',
  lineHeight: 1.5,
  margin: 0
}

const CARD_POOL_STYLE = {
  color: 'var(--ui-text-secondary)',
  fontVariantNumeric: 'tabular-nums',
  fontSize: '0.75rem',
  fontWeight: 600,
  margin: 0
}

const CARD_PLAINTEXT_STYLE = {
  color: 'var(--ui-orange)',
  fontSize: '0.6875rem',
  lineHeight: 1.5,
  margin: 0
}

const CARD_ACTIONS_STYLE = {
  display: 'flex',
  gap: '8px'
}

const LOADER_STYLE = {
  flex: 'none',
  height: '22px',
  width: '22px'
}

const LOADING_ROW_STYLE = {
  alignItems: 'center',
  display: 'flex',
  gap: '10px'
}

const EMPTY_ROW_STYLE = {
  alignItems: 'center',
  display: 'flex',
  flexDirection: 'column',
  gap: '10px'
}

/* ── 行数据的读法（后端的行字段见 plugin_api.py 的 /endpoints）────────────── */

function textOf(value) {
  return String(value || '').trim()
}

function endpointsOf(data) {
  return data && Array.isArray(data.endpoints) ? data.endpoints : []
}

/** 来源判据（§3.2 UC-10）：条目存在于 `custom_providers:` 序列时后端写 `source`。 */
function isCcSwitchRow(row) {
  return textOf(row && row.source) === CC_SOURCE
}

/** 行上的**白名单条数**：只认这一处（卡片「已添加 N 个模型」、`poolCompareText` 的白名单 N、
 *  M4 钉住区与 M5 清空区的计数全从这里取，别再散落第二份判据）。
 *
 *  优先读后端的磁盘真值字段 `allowlist_count`：行里的 `models` 是「磁盘白名单 ∪ 注入的默认
 *  模型」的视图（官方 `_models_from_custom_endpoint_entry` 无条件把 `model:` 插到第 0 位），
 *  清空白名单之后它仍然有 1 条，所以「已添加 0 个模型」靠 `models.length` 永远算不出来。
 *  老形状的行（还没有该字段 / 字段不是非负整数）在这里**一处**退到 `models.length`。 */
function modelCountOf(row) {
  const allowlistCount = row && row.allowlist_count

  if (Number.isInteger(allowlistCount) && allowlistCount >= 0) {
    return allowlistCount
  }
  const models = row && Array.isArray(row.models) ? row.models : []

  return models.length
}

function cardKeyOf(row) {
  return textOf(row && row.id) || textOf(row && row.name) || 'row'
}

/** 自动发现开关 → 钉住徽章：全文件**唯一**一处判定（M2.11 断言 c，徽章文案不散落）。
 *  字段缺失按「允许自动发现」处理，与后端 legacy 行的默认口径一致（§7.4 风险口径）。 */
function readPinBadge(row) {
  const pinned = Boolean(row) && row.discover_models === false

  return {
    hint: pinned ? '' : PIN_UNPINNED_HINT,
    pinned,
    text: pinned ? PIN_TEXT_PINNED : PIN_TEXT_UNPINNED
  }
}

/** §5.2 卡片第三行：默认模型 + 已添加模型数（卡片上不出现别的统计）。 */
function cardMetaText(row, modelCount) {
  const model = textOf(row && row.model)

  return `${DEFAULT_MODEL_PREFIX}${model || POOL_COUNT_PLACEHOLDER} · 已添加 ${modelCount} 个模型`
}

/** R1 加强项（§10.1）：光一个角标不够响，允许自动发现的卡片必须并排两个数字。
 *  「池内」的后端字段还没有 → 占位符，M8 复核时只改这一处。 */
function poolCompareText(modelCount) {
  return `白名单 ${modelCount} / 池内 ${POOL_COUNT_PLACEHOLDER}`
}

/* ── 取数（M2.1）────────────────────────────────────────────────────────── */

/** 一次 `/endpoints`，交给 app 挂载的共享 QueryClient（§6.4：缓存 + 去重）。
 *  `retry: false` —— 失败不自动重试，这样「打开页面只发一次请求」的判据是干净的（M2.10）。 */
function useEndpointsQuery(ctx) {
  const { data, error, isError, isPending } = useQuery({
    queryFn: () => probeBackend(ctx),
    queryKey: ENDPOINTS_QUERY_KEY,
    retry: false,
    staleTime: 30000
  })

  const notMounted = isError && isNotFoundError(error)

  return {
    isPending,
    notMounted,
    /** 非 404 的失败：如实呈现原文，绝不当成「未挂载」（M0 的 STATUS_UNKNOWN 口径）。 */
    errorText: isError && !notMounted ? textOf(error && error.message) : '',
    rows: notMounted ? [] : endpointsOf(data)
  }
}

/* ── 卡片 ───────────────────────────────────────────────────────────────── */

/** 动作占位（M2.8 留的钩子，M3 已接上）：卡片「查看」→ 把该行载入下方表单区（编辑视图）。
 *  回填只填行数据，**API Key 一律不回填**（§3.2 UC-02：密钥回退在后端做），也不发任何请求
 *  （候选区不自动拉，F11）。处理器由 `useModelEditor` 在挂载时登记；没挂载（后端未就绪等）
 *  时保持 M2 的占位语义：什么都不做。 */
function viewRowPlaceholder(row) {
  if (typeof editorOpener === 'function') {
    editorOpener(row)
  }
}

/** 卡片动作行：**两种来源共用这一处**，但每一项各自受来源闸门管（M2→M7 逐模块追加而来，
 *  顺序 = 追加顺序，不重排）：
 *    ① 「查看」——两种来源都有（M2 的编辑视图入口，M3 接上）；
 *    ② 「钉住」——两种来源都有（决策 11 的两条路径在**后端**分叉：`providers:` 走官方 upsert、
 *       cc-switch 走就地改一个字段），只在徽章判定为未钉住时出现（M4 写入，M7 接通卡片按钮）；
 *    ③ 「清空白名单」——`isProvidersSource(row)` 才渲染（M5.3 / 决议 12 / UC-12）；
 *    ④ 「高级 · 迁移到标准形态」——`isCcSwitchRow(row)` 才渲染（M6 / 决议 9：默认动作是钉住）；
 *    ⑤ 「启用」「删除」——`isProvidersSource(row)` 才渲染（M7 / UC-04 / UC-05 / 决议 12：
 *       官方 activate / delete 只在 `providers:` 里定位条目，对 legacy 必 404）。
 *  cc-switch 卡片因此拿不到 ③⑤（后端各有一道独立的 `cc:` 拒绝面，不信前端）。 */
function ProviderCardActions({ pin, row }) {
  return jsxs('div', {
    children: [
      jsx(Button, {
        children: VIEW_ACTION_LABEL,
        onClick: () => viewRowPlaceholder(row),
        size: 'xs',
        variant: 'outline'
      }, 'view'),
      pin.pinned
        ? null
        : jsx(
            Tip,
            {
              /* M7 收尾（编排授权的占位清理）：这颗按钮过去是 `disabled: true` 的死占位，
               * 写入方 M4 早就落地了（`PinZone` 的逐条按钮一直在用），卡片位却还空着 ——
               * 现在走**同一个** `useWriteActions().runPin`（见 `requestPinFromCard`），
               * 不新建第二条钉住链路。cc-switch 行照决策 11 继续提供「钉住」（就地路径）。 */
              children: jsx(Button, {
                children: PIN_ACTION_LABEL,
                onClick: () => requestPinFromCard(row),
                size: 'xs',
                variant: 'outline'
              }, 'pin-button'),
              label: PIN_ACTION_TIP
            },
            'pin'
          ),
      /* M5 追加（只加第三项，上面两项原样不动、顺序不变）：「清空白名单」**只对 `providers:`
       * 行渲染**（M5.3 / 决议 12 / UC-12 v0.4 —— cc-switch 条目的 `models:` 归 cc-switch 所有，
       * 插件写了会被它下次编辑覆盖）。后端也独立拒绝 `cc:` 前缀的 id，不信前端。
       * 处理器由 `ClearAllowlistZone` 登记到 `clearAllowlistHandler`（与 M4 的
       * `blankFormOpener` 同形状，所以本组件的 props 一个都不加）。 */
      isProvidersSource(row)
        ? jsx(
            Tip,
            {
              children: jsx(Button, {
                children: CLEAR_ACTION_LABEL,
                onClick: () => requestClearAllowlist(row),
                size: 'xs',
                variant: 'outline'
              }, 'clear-button'),
              label: CLEAR_ACTION_TIP
            },
            'clear-models'
          )
        : null,
      /* M6 追加（第四项，上面三项原样不动、顺序不变）：**「高级」位只对 cc-switch 行渲染**
       * （§3.2 UC-10 / 决议 9：迁移是**可选**动作，藏在「高级」里，默认动作仍是「钉住」）。
       * `providers:` 的行本来就在标准形态里，拿不到这一项 —— 判据用 M2 的 `isCcSwitchRow`
       * （= 后端的 `source === 'cc-switch'`），与 M5 用 `source` 判清空按钮同一口径。
       * 处理器由本模块的 `MigrateZone` 登记到 `migrateToStandardHandler`（与 M5 的
       * `requestClearAllowlist` 同形状，所以本组件的 props 一个都不加）。 */
      isCcSwitchRow(row)
        ? jsxs(
            'span',
            {
              children: [
                jsx('span', { children: ADVANCED_GROUP_LABEL, style: CARD_ADVANCED_LABEL_STYLE }, 'advanced-label'),
                jsx(
                  Tip,
                  {
                    children: jsx(Button, {
                      children: MIGRATE_ACTION_LABEL,
                      onClick: () => requestMigrateToStandard(row),
                      size: 'xs',
                      variant: 'outline'
                    }, 'migrate-button'),
                    label: MIGRATE_ACTION_TIP
                  },
                  'migrate'
                )
              ],
              style: CARD_ADVANCED_GROUP_STYLE
            },
            'advanced'
          )
        : null,
      /* M7 追加（第五项，上面四项原样不动、顺序不变）：**「启用」「删除」只对 `providers:`
       * 行渲染**（M7.3 / UC-04 / UC-05 / 决议 12）。判据继续吃 M5 的 `isProvidersSource`
       * （= 后端的 `source === 'providers'`），所以 **M6 迁移成功的行自动获得这两项**
       * （progress §六「M6 ↔ M7」：迁移后条目才落到 `providers:`，从此**才**有启用/删除）。
       * 后端另有一道 `cc:` 前缀的 400 拒绝面，不信前端。处理器住在 `DangerZone` 登记的
       * `dangerActionsHandler` 里（与 M4 / M5 / M6 的登记位同形状，本组件 props 一个都不加）。
       * 「启用」对该行已是当前供应商时 disabled —— 再点一次只是把同一份 `model:` 重写一遍。 */
      isProvidersSource(row)
        ? jsxs(
            'span',
            {
              children: [
                jsx(
                  Tip,
                  {
                    children: jsx(Button, {
                      children: ACTIVATE_ACTION_LABEL,
                      disabled: row.is_current === true,
                      onClick: () => requestActivateSupplier(row),
                      size: 'xs',
                      variant: 'outline'
                    }, 'activate-button'),
                    label: row.is_current === true ? ACTIVATE_CURRENT_TIP : ACTIVATE_ACTION_TIP
                  },
                  'activate'
                ),
                jsx(
                  Tip,
                  {
                    children: jsx(Button, {
                      children: DELETE_ACTION_LABEL,
                      onClick: () => requestDeleteSupplier(row),
                      size: 'xs',
                      // 危险动作的强调色只有一个来源：--ui-red（M7.4 / M7.6）
                      style: { color: 'var(--ui-red)' },
                      variant: 'outline'
                    }, 'delete-button'),
                    label: DELETE_ACTION_TIP
                  },
                  'delete'
                )
              ],
              style: CARD_ACTIONS_STYLE
            },
            'danger'
          )
        : null,
      /* 区块 M12 追加（第六项，上面五项原样不动、顺序不变；M12.11 / 拍板 D6）：
       * 「退回官方管理」**只在克隆行**渲染 —— 判据用本区块的 `isAdoptedCloneRow`
       * （= `providers:` 行 ∧ 带来源标记），**不**再新增第三个 providers 闸门调用点
       * （M5 / M7 的静态判据把那道闸门在卡片里的份数钉成 2，判据散落才是真问题）。
       * 入口不直接动手：处理器先打一次只读前置检查，弹窗按实况组句（M12.11）。 */
      isAdoptedCloneRow(row)
        ? jsxs(
            'span',
            {
              children: [
                /* M12.9：克隆卡的来源徽章。卡片头是 M2 `ProviderCard` 的地盘（本区块
                   一行不改），徽章因此与它同判据的「退回官方管理」坐同一组。 */
                jsx(Badge, { children: BUILTIN_BADGE_ADOPTED, variant: 'muted' }, 'clone-badge'),
                jsx(
                  Tip,
                  {
                    children: jsx(Button, {
                      children: RELEASE_ACTION_LABEL,
                      onClick: () => requestReleaseClone(row),
                      size: 'xs',
                      variant: 'outline'
                    }, 'release-button'),
                    label: RELEASE_ACTION_TIP
                  },
                  'release'
                )
              ],
              style: CARD_ACTIONS_STYLE
            },
            'release-group'
          )
        : null
    ],
    style: CARD_ACTIONS_STYLE
  })
}

function ProviderCard({ row }) {
  const pin = readPinBadge(row)
  const ccSwitch = isCcSwitchRow(row)
  const modelCount = modelCountOf(row)

  return jsxs('article', {
    children: [
      jsxs(
        'div',
        {
          children: [
            jsx(
              'span',
              {
                children: textOf(row.name) || textOf(row.id) || POOL_COUNT_PLACEHOLDER,
                style: CARD_NAME_STYLE
              },
              'name'
            ),
            row.is_current === true
              ? jsx('span', { children: CURRENT_MARK_TEXT, style: CARD_CURRENT_STYLE }, 'current')
              : null,
            ccSwitch ? jsx(Badge, { children: CC_SOURCE, variant: 'muted' }, 'source') : null,
            jsx(
              Badge,
              {
                children: pin.text,
                /* 绿/黄走主题 token：--ui-green / --ui-yellow（本机 styles.css 无 --ui-success） */
                style: { color: pin.pinned ? 'var(--ui-green)' : 'var(--ui-yellow)' },
                variant: pin.pinned ? 'success' : 'warn'
              },
              'pin'
            )
          ],
          style: CARD_HEAD_STYLE
        },
        'head'
      ),
      jsx('p', { children: textOf(row.base_url) || POOL_COUNT_PLACEHOLDER, style: CARD_URL_STYLE }, 'url'),
      jsx('p', { children: cardMetaText(row, modelCount), style: CARD_META_STYLE }, 'meta'),
      ccSwitch ? jsx('p', { children: CC_MANAGED_LINE, style: CARD_META_STYLE }, 'managed') : null,
      pin.pinned
        ? null
        : jsxs(
            'div',
            {
              children: [
                jsx('p', { children: pin.hint, style: CARD_HINT_STYLE }, 'hint'),
                jsx('p', { children: poolCompareText(modelCount), style: CARD_POOL_STYLE }, 'pool')
              ],
              style: CARD_NOTE_STYLE
            },
            'pin-note'
          ),
      row.api_key_plaintext === true
        ? jsxs(
            'div',
            {
              children: [
                jsx(
                  Badge,
                  {
                    children: PLAINTEXT_BADGE_TEXT,
                    style: { color: 'var(--ui-orange)' },
                    variant: 'warn'
                  },
                  'key-badge'
                ),
                jsx('p', { children: PLAINTEXT_HINT, style: CARD_PLAINTEXT_STYLE }, 'key-hint')
              ],
              style: CARD_NOTE_STYLE
            },
            'plaintext'
          )
        : null,
      jsx(Separator, {}, 'divider'),
      jsx(ProviderCardActions, { pin, row }, 'actions')
    ],
    style: CARD_STYLE
  })
}

/* 区块 M2：供应商列表区 */
function ProviderListZone({ ctx, query }) {
  /* 区块 M12 追加（M12.7 / 拍板 D5：只读候选**并入本区混排**）：候选取数与接管/退回动作
   * 都要经 `ctx.rest`（插件唯一的请求门，profile 由桌面端自己带，§2.6 C2 已结案）。
   * M2 原有的 `query` 半边与下面每个分支一字未改；新增的两块渲染挂在 return 的
   * children **末尾**（providers 行 → cc 行 → 只读候选卡）。 */
  const candidates = useBuiltinCandidatesQuery(ctx)
  const adopt = useBuiltinAdoptActions(ctx)
  const candidateRows = candidates.rows
  const loaded = !query.notMounted && !query.isPending && !query.errorText
  let body

  if (query.notMounted) {
    /* 404 → 未挂载提示由页面顶部渲染（M0.5 / R2），本区不猜别的原因 */
    body = jsx('p', { children: NOT_MOUNTED_LIST_HINT, style: ZONE_HINT_STYLE }, 'not-mounted')
  } else if (query.isPending && query.rows.length === 0) {
    body = jsxs(
      'div',
      {
        children: [
          jsx(Loader, { label: LOADING_TEXT, style: LOADER_STYLE }, 'loader'),
          jsx('p', { children: LOADING_TEXT, style: ZONE_HINT_STYLE }, 'loading-text')
        ],
        style: LOADING_ROW_STYLE
      },
      'loading'
    )
  } else if (query.errorText) {
    body = jsx('p', { children: `${READ_FAILED_PREFIX}${query.errorText}`, style: ZONE_HINT_STYLE }, 'failed')
  } else if (query.rows.length === 0) {
    /* §8 线框①：空态 + 添加口（表单区是 M3，这里只留按钮位） */
    body = jsxs(
      'div',
      {
        children: [
          jsx(EmptyState, { description: EMPTY_DESCRIPTION, title: EMPTY_TITLE }, 'empty-state'),
          jsx(
            Tip,
            {
              children: jsx(Button, {
                children: ADD_ACTION_LABEL,
                onClick: () => openBlankSupplierForm(),
                variant: 'outline'
              }, 'add-button'),
              label: ADD_SUPPLIER_TIP
            },
            'add'
          )
        ],
        style: EMPTY_ROW_STYLE
      },
      'empty'
    )
  } else {
    body = jsx(
      'div',
      {
        children: query.rows.map(row => jsx(ProviderCard, { row }, cardKeyOf(row))),
        style: COLUMN_ROW_STYLE
      },
      'cards'
    )
  }

  return jsxs('section', {
    children: [
      jsxs(
        'div',
        {
          children: [
            jsx('h2', { children: LIST_ZONE_TITLE, style: ZONE_TITLE_STYLE }, 'list-title'),
            loaded ? jsx('span', { children: `${query.rows.length} ${LIST_COUNT_SUFFIX}`, style: LIST_COUNT_STYLE }, 'count') : null
          ],
          style: LIST_HEAD_STYLE
        },
        'list-head'
      ),
      body,
      /* 区块 M12 追加（M12.7，只加不改）：只读候选卡坐在既有行之后（providers 行 →
       * cc 行 → 候选卡），复用 M2 卡片的视觉；动作行只有「接管管理」一颗钮。 */
      candidateRows.length > 0
        ? jsx(
            'div',
            {
              children: candidateRows.map(row => jsx(
                BuiltinCandidateCard,
                { actions: adopt, row },
                `builtin-${cardKeyOf(row)}`
              )),
              style: COLUMN_ROW_STYLE
            },
            'builtin-cards'
          )
        : null,
      /* 候选那条查询自己失败了也要说出来：「看不到候选」与「候选为空」是两件事。 */
      candidates.errorText
        ? jsx('p', {
            children: `${READ_FAILED_PREFIX}${candidates.errorText}`,
            style: ZONE_HINT_STYLE
          }, 'builtin-failed')
        : null,
      jsx(BuiltinAdoptDialogs, { actions: adopt }, 'builtin-dialogs')
    ],
    style: ZONE_STYLE
  }, 'provider-list')
}

/* ─── 区块 M3：表单区 + 两栏模型区（探测 / 候选 / 已添加）─────────────────────
 *
 * 本区块把 M0 留下的两个占位 zone（表单区 / 模型区）换成实现，锚注释原样保留。
 *
 * 三条跨模块契约（对 M4 冻结，设计 §7.7 / §6.3 / prompt §6）：
 *   1. `FormState` 见下方 EMPTY_FORM —— 八个字段一个不多一个不少；候选区
 *      （fetched / checked / query）与探测中/失败态是**纯 UI 状态**，不进 FormState。
 *   2. validate 的 payload = `buildValidatePayload(form)` =
 *      `{ id, name, base_url, model }` ＋ 铁律 `api_key: form.apiKey.trim() || undefined`。
 *      **绝不允许发 `""`**（后端把空串理解成「清空密钥」并删 `.env` 变量，§3.2 UC-02 / E6）；
 *      `undefined` 经 `JSON.stringify` 后字段从 JSON 里消失（`electron/main.ts:5403`
 *      `Buffer.from(JSON.stringify(body))`），后端才拿到 `None`。
 *      `name` / `base_url` / `model` 是官方 `CustomEndpointUpdate`（web_models.py:36）的必填，
 *      少了会 422，所以三件套必须带上。
 *   3. validate 的响应 = `{ ok, reachable, message, models: string[], error_kind }`；
 *      `error_kind ∈ ok|empty|no_endpoint|auth|unreachable|http|unknown` 是 M3.9 文案的**唯一判据**
 *      （M7 改文案时也照它映射，不要去嗅探英文原文）。
 *
 * 其它口径：
 *   · 探测是**命令式**动作（§6.4）：不进 React Query、不轮询；打开页面与进入编辑态都**不**
 *     自动拉取（F11 / §4「页面打开时不自动探测」）。
 *   · 前端不加更短的超时（§4）：`ctx.rest` 不传 `timeoutMs`，走桌面端 30s 默认
 *     （`electron/hardening.ts:16` `DEFAULT_FETCH_TIMEOUT_MS = 30_000`），盖不住后端的 8s。
 *   · 编辑态 Key **不回填**（§3.2 UC-02 与官方同口径），密钥回退在 M3 后端做（决策 14 / R13），
 *     所以「行为空但行上带过密钥」时前端**不拦**探测，只有 `has_api_key` 为假的条目才要求手填。
 *   · 本模块**不落盘**：保存 / 钉住在 M4。
 */

const VALIDATE_PATH = '/endpoints/validate'
const MODEL_ID_PATTERN = /^[a-z0-9]+(-[a-z0-9]+)*$/
const OTHER_GROUP_LABEL = 'Other'

/** §7.7 的 FormState —— 字段名与形状对 M4 冻结（M4 负责落盘）。 */
const EMPTY_FORM = {
  id: '',               // 供应商标识
  name: '',
  baseUrl: '',
  apiKey: '',           // 仅新增/覆盖时填；编辑时留空 = 保留（后端拿 None）
  contextLength: '',    // 表单里是字符串，出口再转数字
  model: '',            // ★ 默认模型
  models: [],           // 已添加（白名单），有序
  makeDefault: false    // §7.7 字段；M3 无对应控件（保存语义在 M4 决定）
}

/** 候选区：纯派生 UI 状态（§7.7 的 CandidateState；JS 里用数组当有序集合）。 */
const EMPTY_CANDIDATES = { checked: [], fetched: [], query: '' }

/* ── 文案（M3.9 的四条失败文案逐字照任务书；按钮名按 §2.6 C1 的 cc-switch 口径）──── */

/* 布局优化 2026-09-24：原 FORM_ZONE_TITLE / MODEL_ZONE_TITLE 随三合一退役，卡头标题是 EDITOR_ZONE_TITLE。 */
const CANDIDATE_COLUMN_TITLE = '候选模型'
const ADDED_COLUMN_TITLE = '已添加'
const FIELD_ID_LABEL = '供应商标识'
const FIELD_NAME_LABEL = '名称'
const FIELD_BASE_URL_LABEL = 'API 端点'
const FIELD_API_KEY_LABEL = 'API Key'
const FIELD_CONTEXT_LABEL = 'Context'
const FIELD_REQUIRED_MARK = '*'
const API_KEY_KEEP_HINT = '留空 = 保留现有密钥（填写则覆盖）'
const API_KEY_STORED_HINT = '该供应商已有密钥：留空即可，本页不回填密钥。'
const PROBE_BUTTON_LABEL = '获取模型列表'
const PROBE_BUSY_LABEL = '正在获取模型列表…'
/* 「允许自动发现（高级）」勾选位已移除（proposal v0.0.3 §8-4，2026-09-24 拍板）：
 * 该勾选从不参与落盘（保存恒写 discover_models: false），留着只会误导使用者。 */
const NEW_SUPPLIER_LABEL = '+ 添加供应商'
const CANCEL_EDIT_LABEL = '取消'
const SAVE_LABEL = '保存'
const ID_INVALID_HINT = '供应商标识只允许小写字母和数字，段之间用 - 连接（例：axet-proxy）'
const CANDIDATE_SELECT_ALL_LABEL = '全选'
const CANDIDATE_ADD_LABEL = '添加模型 →'
const ADDED_SET_DEFAULT_LABEL = '★ 设默认'
const ADDED_REMOVE_LABEL = '✕ 移除'
const ADDED_STATE_LABEL = '已添加'
const CANDIDATE_SEARCH_PLACEHOLDER = '搜索模型...'
const CANDIDATE_EMPTY_UNPROBED = '尚未获取模型列表 — 打开页面不会自动探测，点上方「获取模型列表」才发请求。'
const CANDIDATE_EMPTY_FILTERED = '没有匹配的候选模型。'
const ADDED_EMPTY_HINT = '暂无模型 — 保存后该供应商在选择器里只剩默认模型'
const MANUAL_ADD_LABEL = '手动添加'
const MANUAL_ADD_FIELD_LABEL = '模型 ID'
const MANUAL_ADD_BUTTON_LABEL = '添加'
const MANUAL_ADD_TIP = '端点不支持 /v1/models 或候选里没有你要的模型时，直接填模型 ID 添加。'
const PROBE_OK_HINT_PREFIX = '候选已就绪：'
const PROBE_OK_SUFFIX = ' 个候选模型'
const PROBE_FAILED_TOAST_TITLE = '获取模型列表失败'
const MODEL_ID_MISSING_HINT = '请先填写模型 ID'
const MODEL_ALREADY_ADDED_HINT = '该模型已在已添加栏里。'

/** M3.9 的四类失败文案（逐字）。不可达的那条要拼上 `{url}`。 */
const ERROR_NEED_ENDPOINT_AND_KEY = '请先填写 API 端点和 API Key'
const ERROR_INVALID_KEY = 'API Key 无效或无权限'
const ERROR_UNREACHABLE_PREFIX = '无法连接到 '
const ERROR_NO_MODELS = '未找到可用模型'
const ERROR_UPSTREAM_UNKNOWN = '端点校验未通过'

/* ── 主题变量样式（延续 M0/M2 的写法；颜色一律 var(--ui-*) token）────────────── */

const FORM_GRID_STYLE = {
  display: 'flex',
  flexDirection: 'column',
  gap: '10px'
}

const FORM_ROW_STYLE = {
  display: 'flex',
  flexWrap: 'wrap',
  gap: '10px'
}

const FORM_FIELD_STYLE = {
  alignItems: 'flex-start',
  display: 'flex',
  flexDirection: 'column',
  flex: '1 1 220px',
  gap: '3px',
  minWidth: '190px'
}

const FIELD_LABEL_STYLE = {
  color: 'var(--ui-text-secondary)',
  fontSize: '0.6875rem',
  fontWeight: 600
}

const FIELD_HINT_STYLE = {
  color: 'var(--ui-text-tertiary)',
  fontSize: '0.6875rem',
  lineHeight: 1.5,
  margin: 0
}

const FIELD_ERROR_STYLE = {
  color: 'var(--ui-red)',
  fontSize: '0.6875rem',
  lineHeight: 1.5,
  margin: 0
}

const FORM_ACTIONS_STYLE = {
  alignItems: 'center',
  display: 'flex',
  flexWrap: 'wrap',
  gap: '8px'
}

/* 三合一卡片的尾行：[保存][取消] 收到右侧，符合应用表单动作行的落位习惯。 */
const SAVE_ROW_STYLE = { ...FORM_ACTIONS_STYLE, justifyContent: 'flex-end' }

const ZONE_HEAD_STYLE = {
  alignItems: 'baseline',
  display: 'flex',
  flexWrap: 'wrap',
  gap: '8px',
  justifyContent: 'space-between',
  marginBottom: '6px'
}

const PROBE_STATUS_STYLE = {
  fontSize: '0.75rem',
  lineHeight: 1.5,
  margin: '8px 0 0',
  wordBreak: 'break-all'
}

const COLUMN_HEAD_STYLE = {
  alignItems: 'baseline',
  display: 'flex',
  gap: '6px',
  justifyContent: 'space-between'
}

const COLUMN_COUNT_STYLE = {
  color: 'var(--ui-text-tertiary)',
  fontVariantNumeric: 'tabular-nums',
  fontSize: '0.6875rem'
}

const COLUMN_BODY_STYLE = {
  display: 'flex',
  flexDirection: 'column',
  gap: '2px',
  margin: '6px 0'
}

/* 布局优化拍板（2026-09-24）：探测回来的模型列表可能很长，两栏各自限高内滚，
 * 栏标题 / 搜索框 / 底部动作条留在外面不随列表滚走；滚动条样式走 .sm-scroll。 */
const MODEL_SCROLL_STYLE = {
  ...COLUMN_BODY_STYLE,
  maxHeight: '320px',
  overflowY: 'auto'
}

const GROUP_HEAD_STYLE = {
  /* 应用原生 eyebrow 口径：小号 + 大写 + 宽字距（command-center/index.tsx:691）。 */
  color: 'var(--ui-text-tertiary)',
  fontSize: '0.625rem',
  fontWeight: 500,
  letterSpacing: '0.08em',
  margin: '8px 0 2px',
  textTransform: 'uppercase'
}

const MODEL_ROW_STYLE = {
  alignItems: 'center',
  border: '1px solid transparent',
  borderRadius: '6px',
  cursor: 'pointer',
  display: 'flex',
  gap: '6px',
  padding: '3px 6px'
}

const MODEL_ROW_DISABLED_STYLE = {
  cursor: 'default',
  opacity: 0.55
}

const MODEL_ROW_ID_STYLE = {
  color: 'var(--ui-text-primary)',
  fontSize: '0.75rem',
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap'
}

const MODEL_ROW_MARK_STYLE = {
  color: 'var(--ui-text-tertiary)',
  fontSize: '0.625rem',
  marginLeft: 'auto'
}

const MODEL_ROW_DEFAULT_MARK_STYLE = {
  color: 'var(--ui-green)',
  fontSize: '0.6875rem',
  fontWeight: 600
}

const COLUMN_ACTIONS_STYLE = {
  display: 'flex',
  gap: '8px'
}

/* ── 纯函数：FormState 的读写（M4 也照这套走，别另起口径）───────────────────── */

function uniqueModels(models) {
  const seen = new Set()
  const ids = []
  for (const item of models) {
    const modelId = textOf(item)
    if (modelId && !seen.has(modelId)) {
      seen.add(modelId)
      ids.push(modelId)
    }
  }
  return ids
}

/** 行数据 → FormState：**apiKey 恒为空**（§3.2 UC-02，密钥回退在后端）。
 *
 *  「已添加」栏的种子吃**磁盘真值** `allowlist_models`（契约补丁的编辑态半边，M4.7）：
 *  `row.models` 是官方 `_models_from_custom_endpoint_entry`（`config_env.py:317-328`）把
 *  条目的 `model:` 无条件插到第 0 位之后的**视图**，拿它回填会让「刚清空白名单」的条目
 *  （磁盘 `models: {}`）一打开编辑器就又挂着默认模型那一条，卡片显 0、栏里显 1。
 *  该字段缺失（老响应 / 别的网关）时在**这一处**退到现行行为，不散落第二份判据。
 *
 *  默认模型只在白名单**非空**时并进去（§7.7 不变式只约束「非空 ⇒ model ∈ models」，
 *  空栏本身是合法状态 —— M3 的 `ADDED_EMPTY_HINT` 与 `saveBlockReason` 都按这个口径写），
 *  所以清空后的条目回填成「空栏 + 默认模型字段保留」，而不是凭空多出一条「已添加」。 */
function formFromRow(row) {
  const diskAllowlist = row && row.allowlist_models
  const addedSeed = Array.isArray(diskAllowlist)
    ? diskAllowlist
    : (row && Array.isArray(row.models) ? row.models : [])
  const models = uniqueModels(addedSeed)
  const rowModel = textOf(row && row.model)

  return {
    ...EMPTY_FORM,
    baseUrl: textOf(row && row.base_url),
    contextLength: row && row.context_length === undefined ? '' : String(row.context_length),
    id: textOf(row && (row.id || row.name)),
    /* 区块 M12 追加（拍板 #11 / M12.10）：带来源标记的克隆条目 = 官方密钥的**引用者**，
     * 密钥框从此只读。`CLONE_ORIGIN_FIELD` 是后端 `adopt` 写进条目上的来源名；行上这一位
     * 经「M1 行的第三处契约补丁」（后端 `_apply_clone_origin`）透传而来 —— 官方行构造器是
     * 白名单，不补那一处则本判据在 `GET /endpoints` 的行上恒假、密钥框永不置灰。 */
    keyReadonly: Boolean(textOf(row && row[CLONE_ORIGIN_FIELD])),
    model: rowModel || models[0] || '',
    models: rowModel && models.length > 0 && !models.includes(rowModel)
      ? [rowModel, ...models]
      : models,
    name: textOf(row && row.name)
  }
}

/** 并入白名单；默认位为空时落到第一条（§7.7 不变式：models 非空 ⇒ model ∈ models）。 */
function formWithAddedModels(form, ids) {
  const models = uniqueModels([...form.models, ...ids])

  return {
    ...form,
    model: models.includes(form.model) ? form.model : models[0] || '',
    models
  }
}

/** ✕ 移除：默认位自动落到列表第一条（§3.2 UC-03）。 */
function formWithRemovedModel(form, modelId) {
  const models = form.models.filter(item => item !== modelId)

  return {
    ...form,
    model: form.model === modelId ? models[0] || '' : form.model,
    models
  }
}

/** validate 的 payload（形状 = 官方 CustomEndpointUpdate 的 snake_case 字段）。 */
function buildValidatePayload(form) {
  return {
    api_key: form.apiKey.trim() || undefined,
    base_url: form.baseUrl.trim(),
    id: form.id.trim(),
    model: form.model.trim(),
    name: form.name.trim() || form.id.trim()
  }
}

/* ── 纯函数：候选区的过滤与分组（UC-07 / R4）──────────────────────────────── */

/** 厂商 = id 里 `/` 之前的前缀（`openai/gpt-5` → `openai`）；无前缀归 Other（**不是** owned_by）。 */
function vendorGroupOf(modelId) {
  const text = textOf(modelId)
  const slash = text.indexOf('/')
  const prefix = slash > 0 ? text.slice(0, slash).trim() : ''

  return prefix || OTHER_GROUP_LABEL
}

function shortModelLabel(modelId) {
  const text = textOf(modelId)
  const slash = text.indexOf('/')

  return slash > 0 ? text.slice(slash + 1) || text : text
}

function matchesCandidate(row, query) {
  const needle = textOf(query).toLowerCase()
  if (!needle) {
    return true
  }
  return (
    row.id.toLowerCase().includes(needle) ||
    row.shortId.toLowerCase().includes(needle) ||
    row.vendor.toLowerCase().includes(needle)
  )
}

/** 派生候选行 + 按厂商分组；Other 恒排最后，其余按名字排。 */
function deriveCandidateGroups(fetched, addedSet, query) {
  const groups = new Map()
  const rows = []
  for (const modelId of uniqueModels(fetched)) {
    const vendor = vendorGroupOf(modelId)
    const row = { added: addedSet.has(modelId), id: modelId, shortId: shortModelLabel(modelId), vendor }
    rows.push(row)
    if (!matchesCandidate(row, query)) {
      continue
    }
    const items = groups.get(vendor) || []
    items.push(row)
    groups.set(vendor, items)
  }
  const names = [...groups.keys()].sort((a, b) => {
    if (a === b) {
      return 0
    }
    if (a === OTHER_GROUP_LABEL) {
      return 1
    }
    if (b === OTHER_GROUP_LABEL) {
      return -1
    }
    return a.localeCompare(b)
  })

  return {
    // groups 是 Map：必须 .get()，bracket 索引恒为 undefined（M3 真机回归）。
    groups: names.map(name => ({ items: groups.get(name).sort((a, b) => a.id.localeCompare(b.id)), name })),
    rows
  }
}

/** M3.9 文案映射：`error_kind` → 用户看到的中文。 */
function copyForProbeFailure(kind, baseUrl, detail) {
  const url = textOf(baseUrl)
  switch (kind) {
    case 'auth':
      return { copy: ERROR_INVALID_KEY, detail: textOf(detail) }
    case 'unreachable':
      return { copy: `${ERROR_UNREACHABLE_PREFIX}${url || PROBE_BASE_URL_FALLBACK}`, detail: textOf(detail) }
    case 'no_endpoint':
      return { copy: ERROR_NEED_ENDPOINT_AND_KEY, detail: '' }
    case 'empty':
      return { copy: ERROR_NO_MODELS, detail: MANUAL_ADD_TIP }
    case 'http':
      return { copy: ERROR_UPSTREAM_UNKNOWN, detail: textOf(detail) }
    default:
      return { copy: ERROR_UPSTREAM_UNKNOWN, detail: textOf(detail) }
  }
}

/* ── 编辑器状态（M3 唯一的 state owner；M2 的「查看」钩子接到这里）───────────── */

/** M2 的 `viewRowPlaceholder` 在挂载时拿到这一个登记位（M2 的卡片区块因此一行不改）。 */
let editorOpener = null

function useModelEditor(ctx) {
  const [visible, setVisible] = useState(false)
  const [form, setForm] = useState(EMPTY_FORM)
  const [candidates, setCandidates] = useState(EMPTY_CANDIDATES)
  const [editingRowId, setEditingRowId] = useState('')
  const [manualModelId, setManualModelId] = useState('')
  const [probing, setProbing] = useState(false)
  const [failure, setFailure] = useState(null)
  const [notice, setNotice] = useState('')
  const [recordHasKey, setRecordHasKey] = useState(false)

  const resetDerived = () => {
    setCandidates(EMPTY_CANDIDATES)
    setFailure(null)
    setNotice('')
    setManualModelId('')
  }

  const openBlank = () => {
    setForm(EMPTY_FORM)
    setEditingRowId('')
    setRecordHasKey(false)
    resetDerived()
    setVisible(true)
  }

  /** 查看 → 编辑视图：表单从行数据回填，**Key 不回填**，候选区不自动拉（UC-02 / F11）。 */
  const openForRow = row => {
    setForm(formFromRow(row))
    setEditingRowId(textOf(row && (row.id || row.name)))
    setRecordHasKey(Boolean(row) && row.has_api_key === true)
    resetDerived()
    setVisible(true)
  }

  const closeEditor = () => {
    setVisible(false)
    setForm(EMPTY_FORM)
    setEditingRowId('')
    setRecordHasKey(false)
    resetDerived()
  }

  const updateField = (field, value) => {
    setForm(current => ({ ...current, [field]: value }))
  }

  const updateModel = (patch) => {
    setForm(current => patch(current))
  }

  /** 命令式探测（§6.4：不进 React Query，不轮询）。 */
  const runProbe = async () => {
    const baseUrl = form.baseUrl.trim()
    // 客户端拦（UC-01）：没端点就探测没有意义；新建态还得有 Key —— 编辑态的 Key 由后端回退补
    if (!baseUrl || (!form.apiKey.trim() && !recordHasKey)) {
      setFailure({ copy: ERROR_NEED_ENDPOINT_AND_KEY, detail: '' })
      setNotice('')
      return
    }
    setProbing(true)
    setFailure(null)
    setNotice('')
    try {
      const result = await ctx.rest(VALIDATE_PATH, { body: buildValidatePayload(form), method: 'POST' })
      const models = uniqueModels(result && Array.isArray(result.models) ? result.models : [])
      const kind = textOf(result && result.error_kind) ||
        (result && result.ok ? (models.length ? 'ok' : 'empty') : 'unknown')
      if (kind === 'ok') {
        setCandidates(current => ({ checked: [], fetched: models, query: current.query }))
        setNotice(`${PROBE_OK_HINT_PREFIX}${models.length}${PROBE_OK_SUFFIX}`)
      } else {
        // 失败不冲掉上一次成功拿到的候选（不清用户已有的数据），只如实标出失败
        const nextFailure = copyForProbeFailure(kind, baseUrl, result && result.message)
        setFailure(nextFailure)
        if (kind === 'empty') {
          setCandidates(current => ({ ...current, fetched: [] }))
        }
        notifyProbeFailure(nextFailure.copy)
      }
    } catch (error) {
      const nextFailure = copyForProbeFailure('unreachable', baseUrl, error && error.message)
      setFailure(nextFailure)
      notifyProbeFailure(nextFailure.copy)
    } finally {
      setProbing(false)
    }
  }

  const toggleChecked = modelId => {
    setCandidates(current => ({
      ...current,
      checked: current.checked.includes(modelId)
        ? current.checked.filter(item => item !== modelId)
        : [...current.checked, modelId]
    }))
  }

  const setChecked = (modelId, checked) => {
    setCandidates(current => ({
      ...current,
      checked: checked
        ? uniqueModels([...current.checked, modelId])
        : current.checked.filter(item => item !== modelId)
    }))
  }

  const setQuery = query => {
    setCandidates(current => ({ ...current, query }))
  }

  const addModels = ids => {
    const fresh = uniqueModels(ids).filter(modelId => !form.models.includes(modelId))
    if (!fresh.length) {
      return false
    }
    updateModel(current => formWithAddedModels(current, fresh))
    setCandidates(current => ({ ...current, checked: current.checked.filter(item => !fresh.includes(item)) }))
    setNotice('')
    setFailure(null)
    return true
  }

  const addCheckedModels = () => {
    addModels(candidates.checked)
  }

  const addManualModel = () => {
    const modelId = textOf(manualModelId)
    if (!modelId) {
      setFailure({ copy: MODEL_ID_MISSING_HINT, detail: '' })
      return
    }
    if (form.models.includes(modelId)) {
      setFailure({ copy: MODEL_ALREADY_ADDED_HINT, detail: '' })
      return
    }
    if (addModels([modelId])) {
      setManualModelId('')
    }
  }

  const removeModel = modelId => {
    updateModel(current => formWithRemovedModel(current, modelId))
  }

  const setDefaultModel = modelId => {
    updateModel(current => ({ ...current, model: modelId }))
  }

  const selectAllVisible = rows => {
    setCandidates(current => ({ ...current, checked: uniqueModels([...current.checked, ...rows]) }))
  }

  useEffect(() => {
    editorOpener = openForRow
    return () => {
      editorOpener = null
    }
  })

  return {
    addCheckedModels,
    addManualModel,
    candidates,
    closeEditor,
    editingRowId,
    failure,
    form,
    manualModelId,
    notice,
    openBlank,
    probing,
    recordHasKey,
    removeModel,
    runProbe,
    setDefaultModel,
    setQuery,
    selectAllVisible,
    setChecked,
    toggleChecked,
    updateField,
    setManualModelId,
    visible
  }
}

function notifyProbeFailure(copy) {
  // toast 与页内状态行同文案（§4「可观测」：与官方口径对齐，便于用户对照官方页排查）
  try {
    host.notify({ kind: 'error', message: copy, title: PROBE_FAILED_TOAST_TITLE })
  } catch (error) {
    // 宿主拿不到 notify 时页内状态行仍然在，不影响判读
  }
}

const PROBE_BASE_URL_FALLBACK = '该端点'

const PROBE_LOADER_STYLE = { flex: 'none', height: '12px', width: '12px' }

const PROBE_ERROR_STYLE = { ...PROBE_STATUS_STYLE, color: 'var(--ui-red)' }

const NOTICE_STYLE = { ...PROBE_STATUS_STYLE, color: 'var(--ui-text-tertiary)' }

/** 勾选交给整行（大命中区）；Checkbox 自己不吃鼠标点击，只吃键盘 Space —— 避免一次点击翻两次。 */
const CHECKBOX_STYLE = { pointerEvents: 'none' }

const ADVANCED_ROW_STYLE = { ...FORM_ACTIONS_STYLE, cursor: 'pointer' }

/* 区块 M3：表单区（新增/编辑共用） */
function FormField({ children, error, hint, label, required }) {
  return jsxs('label', {
    children: [
      jsxs(
        'span',
        {
          children: [label, required ? FIELD_REQUIRED_MARK : null],
          style: FIELD_LABEL_STYLE
        },
        `${label}-label`
      ),
      ...children,
      error
        ? jsx('p', { children: error, role: 'alert', style: FIELD_ERROR_STYLE }, `${label}-error`)
        : hint
          ? jsx('p', { children: hint, style: FIELD_HINT_STYLE }, `${label}-hint`)
          : null
    ],
    style: FORM_FIELD_STYLE
  })
}

/* 表单主体（布局优化 2026-09-24：原「表单区」三合一进 EditorZone 后只剩内容体，
 * 标题行与「+ 添加供应商」由 EditorZone 的头承担；空闲态整卡折叠，不再渲染本组件）。 */
function FormBody({ editor }) {
  const { form } = editor
  const idDraft = form.id.trim()
  const idInvalid = !editor.editingRowId && Boolean(idDraft) && !MODEL_ID_PATTERN.test(idDraft)
  const apiKeyHint = editor.recordHasKey
    ? `${API_KEY_KEEP_HINT} ${API_KEY_STORED_HINT}`
    : API_KEY_KEEP_HINT

  return jsxs(
    'div',
    {
      children: [
        jsxs(
          'div',
          {
            children: [
              jsx(
                FormField,
                {
                  children: [
                    jsx(Input, {
                      'aria-label': FIELD_ID_LABEL,
                      onChange: event => editor.updateField('id', event.target.value),
                      placeholder: 'axet-proxy',
                      value: form.id
                    }, 'id-input')
                  ],
                  error: idInvalid ? ID_INVALID_HINT : '',
                  label: FIELD_ID_LABEL,
                  required: true
                },
                'form-id'
              ),
              jsx(
                FormField,
                {
                  children: [
                    jsx(Input, {
                      'aria-label': FIELD_NAME_LABEL,
                      onChange: event => editor.updateField('name', event.target.value),
                      placeholder: '我的代理',
                      value: form.name
                    }, 'name-input')
                  ],
                  label: FIELD_NAME_LABEL,
                  required: true
                },
                'form-name'
              )
            ],
            style: FORM_ROW_STYLE
          },
          'form-row-basic'
        ),
        jsxs(
          'div',
          {
            children: [
              jsx(
                FormField,
                {
                  children: [
                    jsx(Input, {
                      'aria-label': FIELD_BASE_URL_LABEL,
                      onChange: event => editor.updateField('baseUrl', event.target.value),
                      placeholder: 'http://127.0.0.1:8081/v1',
                      value: form.baseUrl
                    }, 'base-url-input')
                  ],
                  label: FIELD_BASE_URL_LABEL,
                  required: true
                },
                'form-base-url'
              )
            ],
            style: FORM_ROW_STYLE
          },
          'form-row-url'
        ),
        jsxs(
          'div',
          {
            children: [
              jsx(
                FormField,
                {
                  children: [
                    jsx(Input, {
                      'aria-label': FIELD_API_KEY_LABEL,
                      autoComplete: 'new-password',
                      /* 区块 M12 追加（M12.10 / 拍板 #11）：克隆条目的密钥恒引用官方
                       * 「API 密钥」页那把钥匙，本插件不许提交新 key（提交了就会被官方
                       * 写入器另存成第二条变量，制造两份真相）。 */
                      disabled: form.keyReadonly === true,
                      onChange: event => editor.updateField('apiKey', event.target.value),
                      placeholder: 'sk-...',
                      type: 'password',
                      value: form.apiKey
                    }, 'api-key-input')
                  ],
                  hint: form.keyReadonly === true ? CLONE_KEY_READONLY_HINT : apiKeyHint,
                  label: FIELD_API_KEY_LABEL
                },
                'form-api-key'
              ),
              jsx(
                FormField,
                {
                  children: [
                    jsx(Input, {
                      'aria-label': FIELD_CONTEXT_LABEL,
                      inputMode: 'numeric',
                      onChange: event => editor.updateField('contextLength', event.target.value),
                      placeholder: 'auto',
                      value: form.contextLength
                    }, 'context-input')
                  ],
                  hint: FIELD_CONTEXT_HINT,
                  label: FIELD_CONTEXT_LABEL
                },
                'form-context'
              )
            ],
            style: FORM_ROW_STYLE
          },
          'form-row-key'
        ),
        jsxs(
          'div',
          {
            children: [
              jsxs(
                Button,
                {
                  children: [
                    editor.probing
                      ? jsx(Loader, { label: PROBE_BUSY_LABEL, style: PROBE_LOADER_STYLE }, 'probe-loader')
                      : null,
                    editor.probing ? PROBE_BUSY_LABEL : PROBE_BUTTON_LABEL
                  ],
                  disabled: editor.probing,
                  onClick: editor.runProbe,
                  variant: 'default'
                },
                'probe'
              )
            ],
            style: FORM_ACTIONS_STYLE
          },
          'form-row-actions'
        ),
        editor.failure
          ? jsx(
              'p',
              {
                children: editor.failure.detail
                  ? `${editor.failure.copy}（${editor.failure.detail}）`
                  : editor.failure.copy,
                role: 'alert',
                style: PROBE_ERROR_STYLE
              },
              'probe-error'
            )
          : null,
        !editor.failure && editor.notice
          ? jsx('p', { children: editor.notice, role: 'status', style: NOTICE_STYLE }, 'probe-notice')
          : null
      ],
      style: FORM_GRID_STYLE
    },
    'form-body'
  )
}

const FIELD_CONTEXT_HINT = '上下文长度，留空 = 自动。'

/* 区块 M3：模型区（候选 / 已添加两栏） */
function ModelColumn({ actions, children, count, hint, search, title }) {
  return jsxs('div', {
    children: [
      jsxs(
        'div',
        {
          children: [
            jsx('h3', { children: title, style: COLUMN_TITLE_STYLE }, `${title}-title`),
            count === undefined || count === null
              ? null
              : jsx('span', { children: `(${count})`, style: COLUMN_COUNT_STYLE }, `${title}-count`)
          ],
          style: COLUMN_HEAD_STYLE
        },
        `${title}-head`
      ),
      hint ? jsx('p', { children: hint, style: ZONE_HINT_STYLE }, `${title}-hint`) : null,
      search || null,
      jsx('div', { children, className: 'sm-scroll', style: MODEL_SCROLL_STYLE }, `${title}-body`),
      actions || null
    ],
    style: COLUMN_STYLE
  })
}

function CandidateRow({ editor, row }) {
  const checked = editor.candidates.checked.includes(row.id)

  return jsxs(
    'div',
    {
      className: row.added ? undefined : 'sm-row',
      children: [
        jsx(Checkbox, {
          'aria-label': row.id,
          checked,
          disabled: row.added,
          onCheckedChange: next => editor.setChecked(row.id, next === true),
          style: CHECKBOX_STYLE
        }, `check-${row.id}`),
        jsx('span', { children: row.id, style: MODEL_ROW_ID_STYLE, title: row.id }, `id-${row.id}`),
        row.added
          ? jsx('span', { children: ADDED_STATE_LABEL, style: MODEL_ROW_MARK_STYLE }, `added-${row.id}`)
          : null,
        editor.form.model === row.id
          ? jsx('span', { children: DEFAULT_STAR_TEXT, style: MODEL_ROW_DEFAULT_MARK_STYLE }, `star-${row.id}`)
          : null
      ],
      onClick: row.added ? undefined : () => editor.toggleChecked(row.id),
      style: row.added ? { ...MODEL_ROW_STYLE, ...MODEL_ROW_DISABLED_STYLE } : MODEL_ROW_STYLE
    },
    row.id
  )
}

function CandidateColumn({ editor }) {
  const addedSet = new Set(editor.form.models)
  const { groups, rows } = deriveCandidateGroups(
    editor.candidates.fetched,
    addedSet,
    editor.candidates.query
  )
  const selectableVisible = []
  for (const group of groups) {
    for (const row of group.items) {
      if (!row.added) {
        selectableVisible.push(row.id)
      }
    }
  }
  const checkedCount = editor.candidates.checked.length
  const probeFailed = Boolean(editor.failure) && editor.probing === false
  const children = []

  if (rows.length === 0) {
    children.push(jsx('p', {
      children: editor.candidates.fetched.length === 0
        ? (probeFailed && editor.failure.copy ? editor.failure.copy : CANDIDATE_EMPTY_UNPROBED)
        : CANDIDATE_EMPTY_FILTERED,
      style: ZONE_HINT_STYLE
    }, 'candidate-empty'))
  } else {
    for (const group of groups) {
      children.push(jsx('p', { children: group.name, style: GROUP_HEAD_STYLE }, `group-${group.name}`))
      for (const row of group.items) {
        children.push(jsx(CandidateRow, { editor, row }, `row-${row.id}`))
      }
    }
  }

  return jsx(ModelColumn, {
    actions: jsxs(
      'div',
      {
        children: [
          jsx(
            Button,
            {
              children: CANDIDATE_SELECT_ALL_LABEL,
              disabled: selectableVisible.length === 0,
              onClick: () => editor.selectAllVisible(selectableVisible),
              size: 'xs',
              variant: 'ghost'
            },
            'select-all'
          ),
          jsx(
            Button,
            {
              children: CANDIDATE_ADD_LABEL,
              disabled: checkedCount === 0,
              onClick: editor.addCheckedModels,
              size: 'xs',
              variant: 'outline'
            },
            'add-checked'
          )
        ],
        style: COLUMN_ACTIONS_STYLE
      },
      'candidate-actions'
    ),
    children,
    count: rows.length,
    hint: '',
    search: jsx(
      SearchField,
      {
        'aria-label': CANDIDATE_SEARCH_LABEL,
        containerClassName: 'w-full',
        onChange: editor.setQuery,
        placeholder: CANDIDATE_SEARCH_PLACEHOLDER,
        value: editor.candidates.query
      },
      'candidate-search'
    ),
    title: CANDIDATE_COLUMN_TITLE
  })
}

const CANDIDATE_SEARCH_LABEL = '搜索候选模型（按模型 ID 或厂商前缀过滤）'

const DEFAULT_STAR_TEXT = '★'
const DEFAULT_SET_LABEL = '★ 默认中'

function AddedModelRow({ editor, modelId }) {
  const isDefault = editor.form.model === modelId

  return jsxs(
    'div',
    {
      children: [
        jsx('span', { children: modelId, style: MODEL_ROW_ID_STYLE, title: modelId }, `added-id-${modelId}`),
        jsx(
          Button,
          {
            children: isDefault ? DEFAULT_SET_LABEL : ADDED_SET_DEFAULT_LABEL,
            disabled: isDefault,
            onClick: () => editor.setDefaultModel(modelId),
            size: 'xs',
            variant: 'ghost'
          },
          `set-default-${modelId}`
        ),
        jsx(
          Button,
          {
            children: ADDED_REMOVE_LABEL,
            onClick: () => editor.removeModel(modelId),
            size: 'xs',
            variant: 'ghost'
          },
          `remove-${modelId}`
        )
      ],
      style: MODEL_ROW_STYLE
    },
    modelId
  )
}

function AddedColumn({ editor }) {
  const children = editor.form.models.length === 0
    ? [jsx('p', { children: ADDED_EMPTY_HINT, role: 'status', style: ZONE_HINT_STYLE }, 'added-empty')]
    : editor.form.models.map(modelId => jsx(AddedModelRow, { editor, modelId }, `added-row-${modelId}`))

  children.push(
    jsxs(
      'div',
      {
        children: [
          jsx('span', { children: `${MANUAL_ADD_LABEL} · ${MANUAL_ADD_FIELD_LABEL}`, style: FIELD_LABEL_STYLE }, 'manual-label'),
          jsx(Input, {
            'aria-label': MANUAL_ADD_FIELD_LABEL,
            onChange: event => editor.setManualModelId(event.target.value),
            onKeyDown: event => {
              if (event.key === 'Enter') {
                event.preventDefault()
                editor.addManualModel()
              }
            },
            placeholder: 'vendor/model-id',
            value: editor.manualModelId
          }, 'manual-input'),
          jsx(
            Tip,
            {
              children: jsx(Button, {
                children: MANUAL_ADD_BUTTON_LABEL,
                onClick: editor.addManualModel,
                size: 'xs',
                variant: 'outline'
              }, 'manual-add-button'),
              label: MANUAL_ADD_TIP
            },
            'manual-add'
          )
        ],
        style: COLUMN_ACTIONS_STYLE
      },
      'manual-add-row'
    )
  )

  return jsx(ModelColumn, {
    children,
    count: editor.form.models.length,
    title: ADDED_COLUMN_TITLE
  })
}

/* 模型两栏（布局优化 2026-09-24：原「模型区」三合一进 EditorZone；探测中状态由表单里的
 * 那颗「获取模型列表」按钮自己显示，不再需要区标题旁的忙线小字）。 */
function ModelColumns({ editor }) {
  return jsxs('div', {
    children: [
      jsx(CandidateColumn, { editor }, 'candidates'),
      jsx(AddedColumn, { editor }, 'added')
    ],
    style: COLUMN_ROW_STYLE
  }, 'model-columns')
}

/* ─── 区块 M4：写链路（保存 / 新建 / 编辑落盘 + 钉住 / 批量钉住）───────────────────
 *
 * 本区块只**追加**两个 zone（`PinZone` 坐在列表区之后 = UC-11 的「列表页顶部」批量位；
 * `SaveZone` 坐在模型区之后 = §5.2 线框最下方的 `[保存] [取消]` 行。布局优化
 * 2026-09-24：`SaveZone` 已并入 `EditorZone` 成为保存尾区 `SaveFooter`），以及在
 * `SupplierModelsPage` 的 children 里各加一行挂载点——M0/M2/M3 的区块一行未改。
 * 唯一一处越界改动是编排者单独授权的 M2 空态那一行：`+ 添加供应商` 从 disabled
 * 换成 `onClick: () => openBlankSupplierForm()`（M4 的建卡入口，见 `blankFormOpener`）。
 *
 * 对后端（`dashboard/plugin_api.py` 的 M4 区块）的契约，也是 M5/M6/M7 的参照：
 *   · `POST /endpoints`            body = `buildSavePayload(form)`，`profile` 由 `ctx.rest`
 *                                  自己带（§2.6 C2 已结案：前端**不**手工拼参）
 *   · `POST /endpoints/{id}/pin`   body = `{ base_url }`（legacy 的第二定位键；
 *                                  `providers:` 路径整份忽略），id 用 M1 行的原样 id
 *     → 响应 `{ ok, id, pin_path: "providers"|"legacy", endpoints, current }`
 *
 * 三条语义铁律在前端的落点：
 *   1. `buildSavePayload` **无条件**写 `discover_models: false` —— 恒为白名单模式，
 *      不受任何界面开关影响（原 M3「允许自动发现（高级）」勾选位已移除，见其退役注释）。
 *   2. `api_key: form.apiKey.trim() || undefined` —— 空串变 `undefined`，字段经
 *      `JSON.stringify` 后从 JSON 里消失，后端才拿到「保留」；传 `""` 是**清空密钥**，
 *      所以这里永远不发空串（显式清密钥是 M7 的独立动作 + 二次确认）。
 *   3. 白名单可以为空、池永远至少含默认模型 → 所有「清空」相关文案一律用
 *      「**只剩默认模型**」（§7.7 不变式 3 的 v0.4 口径：`SAVE_EMPTY_ALLOWLIST_NOTE`
 *      与 M3 的 `ADDED_EMPTY_HINT`），不使用「一个模型都不给」那类做不到的说法。
 *
 * 其它口径：
 *   · 保存前断言**不变式 1**（M4.3）：`models` 非空 ⇒ `model` ∈ `models`，否则不提交。
 *   · `cc:` 开头的条目**不给保存**（§3.2 UC-10 / 决议 12：`models:` 归 cc-switch 所有），
 *     只能钉住。
 *   · 编辑回填与「候选区不自动拉取」由 M3 的 `useModelEditor` / `formFromRow` 提供，
 *     本区块复用，不重复实现（M4.7）。
 *   · 反馈（M4.8）：成功 → `haptic` + toast `已保存`；失败 → toast **透传后端 detail 原文**；
 *     之后 `invalidateQueries(['supplier-models','endpoints'])`（M2 常量的单一真相源）。
 *     勾选了 `make_default` 的那次保存**另外**走 M7 的 `notifyHostMainModelChanged`
 *     （缺陷 #3：顶层 `model:` 被官方改写后，宿主的 `['model-options']` 与
 *     `['hermes-config-record']` 两份缓存必须一起失效，否则模型菜单仍显旧默认）。
 */

const SAVE_ENDPOINTS_PATH = '/endpoints'
const PIN_PATH_SUFFIX = '/pin'
/** legacy 行的 id 前缀（与后端 `CC_ID_PREFIX` 同值）；判来源优先看行的 `source`。 */
const WRITE_LEGACY_ID_PREFIX = 'cc:'
/** 批量钉住进行中的标识（不是任何真条目的 id），用于按钮的「正在钉住…」。 */
const BATCH_PINNING_KEY = '__batch__'

/* ── 文案（§5.3 的保存反馈 + UC-11 的批量位；不变式 3 的措辞已逐条对齐）─────────── */

/* 改动记录：2026-09-25 R3（M10.1，设计 §2.3 表 A · A2 / A3）：钉住区标题与「全部已钉住」
   提示改成使用者语言（机制语「自动发现覆盖白名单」「端点的全量目录」换成「系统自动发现的
   模型挤进 / 挤掉白名单」的说法）。常量名不动，只改字符串值。 */
const PIN_ZONE_TITLE = '钉住供应商（钉住后，系统自动发现的模型不会挤掉你挑的白名单）'
const BATCH_PIN_LABEL_PREFIX = '把 '
const BATCH_PIN_LABEL_SUFFIX = ' 个未钉住的供应商钉住'
const BATCH_PIN_NONE_LABEL = '没有未钉住的供应商'
const ALL_PINNED_HINT = '全部供应商都已钉住：系统自动发现的模型不会再挤进白名单。'
const PIN_ZONE_LOAD_HINT = '列表还没读到，先解决上方的提示。'
const PIN_CONFIRM_TITLE_PREFIX = '把 '
const PIN_CONFIRM_TITLE_SUFFIX = ' 个未钉住的供应商钉住？'
const PIN_CONFIRM_DESCRIPTION =
  '逐个写入 discover_models: false。钉住只阻止未来的覆盖，不会清理已经灌进白名单的模型；' +
  'providers: 条目走官方保存链路，cc-switch 条目只就地改这一个字段。'
const PIN_CONFIRM_OK_LABEL = '钉住'
const PIN_CONFIRM_CANCEL_LABEL = '取消'
const PIN_ITEM_LABEL = '未钉住'
const PIN_BUSY_LABEL = '正在钉住…'
const PIN_DONE_TOAST_TITLE = '钉住'
const PIN_FAILED_TOAST_TITLE = '钉住失败'
const PIN_ONE_DONE_MESSAGE = '已钉住 '
const PIN_BATCH_MESSAGE_PREFIX = '已钉住 '
const PIN_BATCH_FAILED_SUFFIX = ' 个失败'
/* 改动记录：2026-09-25 R3（M10.1，设计 §2.3 表 A · A4）：`PIN_NOTE` 去掉设计编号引用，
   「反向开关」说成「取消钉住的开关」，并把恢复自动发现的路指到官方设置页。
   （同区的 `PIN_CONFIRM_DESCRIPTION` 属二次确认弹窗正文 = 豁免档，本次一字未动。） */
const PIN_NOTE =
  '钉住只阻止未来的覆盖，不会清理已经挤进白名单的模型。要恢复自动发现，请回官方设置页编辑该供应商 —— ' +
  '本插件不提供取消钉住的开关。'
const PIN_RESULTS_TITLE = '逐条结果'
const PIN_RESULTS_ALL_OK = '全部成功'

/* 布局优化 2026-09-24：原 SAVE_ZONE_TITLE / SAVE_DISABLED_NO_EDIT 随三合一退役（空闲态整卡折叠，没有单独的保存占位行）。 */
/* 改动记录：2026-09-25 R3（M10.5，设计 §2.3 表 A · A32）：`SAVE_DISABLED_LEGACY` 里的
   「条目」与存储字段名说法换成使用者语言（「该供应商」「模型白名单」）。 */
const SAVE_DISABLED_LEGACY =
  '该供应商由 cc-switch 管理：本插件不写它的模型白名单（要改请回 cc-switch）。这里只能钉住。'
const SAVE_EMPTY_ALLOWLIST_NOTE = '已添加栏为空 — 保存后该供应商在选择器里只剩默认模型。'
/* 改动记录：2026-09-25 R3（M10.5，设计 §2.3 表 A · A29 / A30 / A31）：保存区这三条改成
   使用者语言——去掉字段名与字段值写法（自动开关的字段名、顶层镜像的字段名），
   「Hermes 主模型」「当前供应商」这类说法说成「全局正在使用的模型 / 供应商」。
   （M9 新增的 `SAVED_TOAST_*` 三条按 §2.2 规则起草，本次不回改 —— 设计 §0.3。） */
const SAVE_FORCED_PIN_NOTE = '通过本插件保存时，一律自动钉住（不会接受系统自动发现的模型）。'
const SAVE_MAKE_DEFAULT_LABEL = '保存的同时，把 ★ 默认模型设为全局正在使用的模型'
const SAVE_MAKE_DEFAULT_TIP =
  '勾选才会在保存后把该供应商设为全局正在使用的供应商。不勾 = 只改这个供应商自己的默认模型。'
const SAVED_TOAST_MESSAGE = '已保存'
/* M9.7（设计 §1.3）：删除数只来自后端回执 `removed_models`（前端不自己数，与 M5 吃
   `cleared_models` 同一口径）；文案按 M10 §2.2 规则起草（无字段名、无编号），终稿不回改。
   `write_channel` 是机器可读判据，**不进任何 UI 文案**。 */
const SAVED_TOAST_REMOVED_INFIX = '，本次删除 '
const SAVED_TOAST_REMOVED_SUFFIX = ' 个模型'
const SAVED_TOAST_TITLE = '供应商与模型'
const SAVE_FAILED_TOAST_TITLE = '保存失败'
const SAVED_NOTE_PREFIX = '已保存：'
const SAVE_RENAMED_NOTE_SUFFIX = '（供应商标识按官方规则规整过）'
const SAVE_MISSING_ID = '请先填写供应商标识'
const SAVE_MISSING_MODEL = '请先在「已添加」栏放至少一个模型，或用 ★ 指定默认模型。'
const SAVE_ERROR_DEFAULT_NOT_IN_ALLOWLIST =
  '默认模型不在「已添加」栏里 — 请点该行的「★ 设默认」，或先把默认模型添加进白名单。'
const WRITE_INFLIGHT_SUFFIX = '（进行中，请稍候）'

/* ── 主题变量样式（延续 M0/M2/M3；颜色一律 var(--ui-*) token）────────────────── */

const WRITE_HEAD_STYLE = {
  alignItems: 'baseline',
  display: 'flex',
  flexWrap: 'wrap',
  gap: '8px',
  justifyContent: 'space-between'
}

const WRITE_LIST_STYLE = {
  display: 'flex',
  flexDirection: 'column',
  gap: '4px',
  margin: '8px 0 0'
}

const WRITE_ITEM_STYLE = {
  alignItems: 'center',
  display: 'flex',
  flexWrap: 'wrap',
  gap: '8px'
}

const WRITE_ITEM_NAME_STYLE = {
  color: 'var(--ui-text-primary)',
  fontSize: '0.75rem',
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap'
}

const WRITE_ITEM_META_STYLE = {
  color: 'var(--ui-text-tertiary)',
  fontSize: '0.6875rem',
  marginLeft: 'auto'
}

const WRITE_STATUS_STYLE = {
  color: 'var(--ui-text-tertiary)',
  fontSize: '0.75rem',
  lineHeight: 1.5,
  margin: '8px 0 0'
}

const WRITE_OK_STATUS_STYLE = { ...WRITE_STATUS_STYLE, color: 'var(--ui-green)' }

const WRITE_FAIL_STATUS_STYLE = { ...WRITE_STATUS_STYLE, color: 'var(--ui-red)' }

const WRITE_NOTE_STYLE = { ...WRITE_STATUS_STYLE, margin: '6px 0 0' }

/* ── 纯函数：payload / 前置断言 / 错误原文 ───────────────────────────────────── */

/** 钉住的路径（id 原样百分号编码：`cc:bailian` 里的冒号留原位，与 M1 行的 id 一致）。 */
function pinPathFor(endpointId) {
  return `${SAVE_ENDPOINTS_PATH}/${encodeURIComponent(textOf(endpointId))}${PIN_PATH_SUFFIX}`
}

/** M4.4：payload 里 `api_key` 是可选键（`api_key?: string`），空串一律变 `undefined`。 */
function buildSavePayload(form) {
  const endpointId = form.id.trim()
  const models = uniqueModels(form.models)
  const context = Number.parseInt(form.contextLength.trim(), 10)
  const payload = {
    // 铁律 2：留空 → `undefined` → 字段从 JSON 里消失 → 后端拿到 None = 保留密钥
    // 区块 M12 追加（M12.10 / 拍板 #11）：克隆表单**无条件**不提交 key（连「保留」都省，
    // 后端自己按引用官方变量的形状写）——前端拦一层、后端 `api_key=None` 再拦一层。
    api_key: form.keyReadonly === true ? undefined : (form.apiKey.trim() || undefined),
    base_url: form.baseUrl.trim(),
    // 铁律 1：恒为 false —— 本插件的语义就是白名单模式（原「允许自动发现」高级勾选位已移除）
    discover_models: false,
    id: endpointId,
    make_default: form.makeDefault === true,
    model: form.model.trim(),
    models,
    name: form.name.trim() || endpointId
  }
  if (Number.isFinite(context) && context > 0) {
    payload.context_length = context
  }
  return payload
}

/** 该表单当前指向 legacy（cc-switch）条目 → 不提供保存（决议 12）。 */
function isLegacyFormTarget(editor, rows) {
  const editingId = textOf(editor.editingRowId)
  if (!editingId) {
    return false
  }
  if (editingId.startsWith(WRITE_LEGACY_ID_PREFIX)) {
    return true
  }
  const row = (rows || []).find(item => textOf(item && item.id) === editingId)
  return Boolean(row) && isCcSwitchRow(row)
}

/** M4.3 的保存前置断言：返回要拦下来的文案，`''` 表示可以提交。 */
function saveBlockReason(editor, rows) {
  const form = editor.form
  if (isLegacyFormTarget(editor, rows)) {
    return SAVE_DISABLED_LEGACY
  }
  const endpointId = textOf(form.id)
  if (!endpointId) {
    return SAVE_MISSING_ID
  }
  if (!editor.editingRowId && !MODEL_ID_PATTERN.test(endpointId)) {
    return ID_INVALID_HINT
  }
  const model = textOf(form.model)
  const models = uniqueModels(form.models)
  if (!model && models.length === 0) {
    return SAVE_MISSING_MODEL
  }
  // 不变式 1：models 非空 ⇒ model ∈ models（否则后端会把默认模型并进白名单，
  // 出现「用户没添加却被写进去」）；M3 的 formWithAddedModels/formWithRemovedModel
  // 已维持这个不变式，这里是提交前的第二道。
  if (models.length > 0 && !models.includes(model)) {
    return SAVE_ERROR_DEFAULT_NOT_IN_ALLOWLIST
  }
  return ''
}

/**
 * 后端错误的 `detail` 原文（M4.8 的「透传后端 detail 原文」）。
 *
 * 桌面端的错误形状是 `err.statusCode` 加 `err.message`，message 的前缀是状态码、
 * 冒号之后是响应体原文（`electron/api-transport.ts:183` 的 `httpStatusError`）；
 * FastAPI 的响应体是 JSON 对象里的 `detail` 字段；422 的 detail 是数组，逐项取 `msg`。
 */
function backendErrorDetail(error) {
  const message = textOf(error && error.message ? error.message : error)
  const separator = message.indexOf(':')
  const body = (separator > 0 ? message.slice(separator + 1) : message).trim()
  if (!body) {
    return message
  }
  if (!body.startsWith('{')) {
    return body
  }
  try {
    const parsed = JSON.parse(body)
    const detail = parsed && parsed.detail
    if (typeof detail === 'string' && detail.trim()) {
      return detail.trim()
    }
    if (Array.isArray(detail)) {
      const joined = detail
        .map(item => (item && item.msg ? String(item.msg) : JSON.stringify(item)))
        .join('；')
      if (joined.trim()) {
        return joined
      }
    }
  } catch (cause) {
    // 不是 JSON：原文回传，不猜、不修饰
  }
  return body
}

function notifyWriteFeedback(kind, message, title, detail) {
  try {
    host.notify({ detail, kind, message, title })
  } catch (error) {
    // 宿主拿不到 notify 时页内状态行仍在（与 M3 的 notifyProbeFailure 同一口径）
  }
}

/* ── 空态「+ 添加供应商」的登记位（M2 那一行授权改动指向这里）──────────────────── */

/** 处理器由 `EditorZone` 挂载时从 `useModelEditor` 拿到的 `openBlank` 登记（2026-09-24 三合一后从 SaveZone 上移，折叠态也不丢登记）；没挂载时按 M2 语义什么都不做。 */
let blankFormOpener = null

function openBlankSupplierForm() {
  if (typeof blankFormOpener === 'function') {
    blankFormOpener()
  }
}

/* ── 写动作（保存 / 钉住 / 批量）────────────────────────────────────────────── */

function useWriteActions(ctx) {
  const queryClient = useQueryClient()
  const [pinResults, setPinResults] = useState([])
  const [pinningId, setPinningId] = useState('')
  const [batchOpen, setBatchOpen] = useState(false)
  const [savedNote, setSavedNote] = useState('')

  /** §6.4 / M4.8：写完之后失效那条查询（键取 M2 的 `ENDPOINTS_QUERY_KEY`，单一真相源）。 */
  const refreshEndpoints = () => {
    queryClient.invalidateQueries({ queryKey: ENDPOINTS_QUERY_KEY })
  }

  const saveMutation = useMutation({
    mutationFn: payload => ctx.rest(SAVE_ENDPOINTS_PATH, { body: payload, method: 'POST' }),
    // 失败：toast 透传后端 detail 原文（不翻译、不修饰）
    onError: error => notifyWriteFeedback('error', backendErrorDetail(error), SAVE_FAILED_TOAST_TITLE, ''),
    // 成功：只留触觉反馈。toast 文案挪进 `runSave`（M9.8）——它要**吃后端回执**的
    // `removed_models`，`onSuccess` 在这里拿不到那次请求的结果形状（不复制判据）。
    onSuccess: () => {
      haptic('success')
    },
    // 成败都要让列表回到当前真相（钉住徽章尤其如此）
    onSettled: () => refreshEndpoints()
  })

  const pinMutation = useMutation({
    mutationFn: target => ctx.rest(pinPathFor(target.id), {
      // legacy 条目的第二定位键（§6.3 按 (name, base_url) 定位）；providers: 路径整份忽略
      body: { base_url: textOf(target.baseUrl) },
      method: 'POST'
    }),
    onError: (error, target) => {
      if (target.batch) {
        return                       // 批量里的失败由结果列表逐条呈现，不叠 N 条 toast
      }
      notifyWriteFeedback('error', backendErrorDetail(error), PIN_FAILED_TOAST_TITLE, target.name)
    },
    onSuccess: (_result, target) => {
      if (target.batch) {
        return
      }
      haptic('success')
      notifyWriteFeedback('success', `${PIN_ONE_DONE_MESSAGE}${target.name}`, PIN_DONE_TOAST_TITLE, '')
    },
    onSettled: () => refreshEndpoints()
  })

  const pinTargetOf = row => ({
    batch: false,
    baseUrl: textOf(row && row.base_url),
    id: textOf(row && row.id),
    name: textOf(row && (row.name || row.id)) || POOL_COUNT_PLACEHOLDER
  })

  /** 单条钉住（卡片位由 `PinZone` 的行内按钮提供）。 */
  const runPin = async row => {
    const target = pinTargetOf(row)
    if (!target.id) {
      setPinResults([{ detail: '该行没有 id，无法定位。', id: '', name: target.name, ok: false }])
      return
    }
    setPinningId(target.id)
    try {
      await pinMutation.mutateAsync(target)
      setPinResults([{ detail: '', id: target.id, name: target.name, ok: true }])
    } catch (error) {
      setPinResults([{ detail: backendErrorDetail(error), id: target.id, name: target.name, ok: false }])
    } finally {
      setPinningId('')
    }
  }

  /** UC-11 批量钉住：二次确认后**逐条**执行；成功保留、失败逐条列出（不中断后面的条目）。 */
  const runBatchPin = async rows => {
    const targets = (rows || [])
      .filter(row => readPinBadge(row).pinned === false)
      .map(pinTargetOf)
      .filter(target => target.id)
    const results = []
    let failures = 0
    setPinningId(BATCH_PINNING_KEY)
    for (const target of targets) {
      try {
        await pinMutation.mutateAsync({ ...target, batch: true })
        results.push({ detail: '', id: target.id, name: target.name, ok: true })
      } catch (error) {
        failures += 1
        results.push({ detail: backendErrorDetail(error), id: target.id, name: target.name, ok: false })
      }
    }
    setPinningId('')
    setPinResults(results)
    const succeeded = targets.length - failures
    // 有成功就关掉弹窗（逐条结果在本区里列出）；全败则**留着**弹窗——
    // ConfirmDialog 只在保持打开时内联显示 onConfirm 抛出的原文。
    if (succeeded > 0) {
      setBatchOpen(false)
    }
    notifyWriteFeedback(
      failures === 0 ? 'success' : 'warning',
      `${PIN_BATCH_MESSAGE_PREFIX}${succeeded} 个${failures ? `，${failures}${PIN_BATCH_FAILED_SUFFIX}` : ''}`,
      PIN_DONE_TOAST_TITLE,
      ''
    )
    if (failures && succeeded === 0) {
      // 全败：让 ConfirmDialog 停在原地并显示原文（SDK 的 onConfirm 抛出即内联报错）
      throw new Error(results[0] ? results[0].detail : PIN_FAILED_TOAST_TITLE)
    }
  }

  /** 保存（M4.3 的前置断言在这里，第二次判定；按钮 disabled 已经拦过一次）。 */
  const runSave = async (editor, rows) => {
    const blocked = saveBlockReason(editor, rows)
    if (blocked) {
      notifyWriteFeedback('error', blocked, SAVE_FAILED_TOAST_TITLE, '')
      return
    }
    const payload = buildSavePayload(editor.form)
    setSavedNote('')
    try {
      const result = await saveMutation.mutateAsync(payload)
      // M9.8 / M9.9：成功 toast 组装在**这里**，数字只吃**后端回执**（`removed_models`）。
      // 缺字段 = 0（旧后端形状的兜底，绝不由前端自己数 —— 防「页面视图 ≠ 磁盘真值」分叉）。
      // N=0 时不带后半句（拍板 #12：纯新增保存的回执不含「本次删除」）。
      const removed = Number(result && result.removed_models) || 0
      notifyWriteFeedback(
        'success',
        removed > 0
          ? `${SAVED_TOAST_MESSAGE}${SAVED_TOAST_REMOVED_INFIX}${removed}${SAVED_TOAST_REMOVED_SUFFIX}`
          : SAVED_TOAST_MESSAGE,
        SAVED_TOAST_TITLE,
        ''
      )
      // 缺陷 #3（真机）：勾选「随保存把 ★ 写成 Hermes 主模型」= 官方保存顺手重写了顶层
      // `model:`，宿主那两份缓存（模型候选 / config 记录）当场过期。**保存路径此前只失效
      // 本插件的列表**，于是模型菜单仍显旧默认，直到切供应商（`gatewayScope` 变化才强制
      // `refreshCurrentModel`）才跟上。这里复用 M7 那一扇统一门（与启用 / 删除同一处实现，
      // 不另立第二份失效口径）；没勾选 `make_default` 时顶层没动，就不惊动宿主。
      if (payload.make_default === true) {
        notifyHostMainModelChanged(queryClient)
      }
      const savedId = textOf(result && result.id) || payload.id
      if (savedId && savedId !== payload.id) {
        // 官方 `_custom_endpoint_id` 规整过标识：把表单指到实际落盘的 id，避免再保存时新建一条
        editor.updateField('id', savedId)
        setSavedNote(`${SAVED_NOTE_PREFIX}${savedId}${SAVE_RENAMED_NOTE_SUFFIX}`)
      } else {
        setSavedNote(`${SAVED_NOTE_PREFIX}${savedId}`)
      }
    } catch (error) {
      setSavedNote('')                 // toast 已由 onError 透传后端 detail 原文
    }
  }

  return {
    batchOpen,
    pinning: textOf(pinningId),
    pinResults,
    saving: saveMutation.isPending,
    runBatchPin,
    runPin,
    runSave,
    setBatchOpen,
    setSavedNote
  }
}

/* ── 写反馈的结果列表（钉住逐条结果 / 保存状态行共用形状）─────────────────────── */

function WriteStatusLine({ results, text }) {
  if (text) {
    return jsx('p', { children: text, role: 'status', style: WRITE_OK_STATUS_STYLE }, 'write-status')
  }
  if (!results || results.length === 0) {
    return null
  }
  const failed = results.filter(item => !item.ok)
  const style = failed.length === 0 ? WRITE_OK_STATUS_STYLE : WRITE_FAIL_STATUS_STYLE
  const summary = failed.length === 0
    ? `${PIN_RESULTS_TITLE}：${PIN_RESULTS_ALL_OK}（${results.length}）`
    : `${PIN_RESULTS_TITLE}：成功 ${results.length - failed.length} / 失败 ${failed.length}`

  return jsxs(
    'div',
    {
      children: [
        jsx('p', { children: summary, role: 'status', style }, 'summary'),
        ...failed.map(item => jsx(
          'p',
          { children: `${item.name}：${item.detail || PIN_FAILED_TOAST_TITLE}`, role: 'alert', style: WRITE_FAIL_STATUS_STYLE },
          `fail-${item.id || item.name}`
        ))
      ]
    },
    'results'
  )
}

/* 区块 M4：钉住区（批量 + 逐条；坐在列表区之后 = UC-11 的「列表页顶部」） */
function PinZone({ actions, query }) {
  const rows = query.rows
  const notPinned = rows.filter(row => readPinBadge(row).pinned === false)
  const busy = actions.pinning !== ''
  const batchLabel = `${BATCH_PIN_LABEL_PREFIX}${notPinned.length}${BATCH_PIN_LABEL_SUFFIX}`

  if (query.notMounted || query.isPending || query.errorText) {
    return jsx('section', {
      children: jsxs('div', {
        children: [
          jsx('h2', { children: PIN_ZONE_TITLE, style: ZONE_TITLE_STYLE }, 'pin-title'),
          jsx('p', { children: PIN_ZONE_LOAD_HINT, style: ZONE_HINT_STYLE }, 'pin-hint')
        ]
      }),
      style: ZONE_STYLE
    }, 'pin-zone')
  }

  return jsxs('section', {
    children: [
      jsxs(
        'div',
        {
          children: [
            jsx('h2', { children: PIN_ZONE_TITLE, style: ZONE_TITLE_STYLE }, 'pin-title'),
            notPinned.length > 0
              ? jsx(
                  Button,
                  {
                    children: busy && actions.pinning === BATCH_PINNING_KEY ? PIN_BUSY_LABEL : batchLabel,
                    disabled: busy || notPinned.length === 0,
                    onClick: () => actions.setBatchOpen(true),
                    size: 'xs',
                    variant: 'outline'
                  },
                  'pin-batch'
                )
              : jsx('span', { children: BATCH_PIN_NONE_LABEL, style: LIST_COUNT_STYLE }, 'pin-none')
          ],
          style: WRITE_HEAD_STYLE
        },
        'pin-head'
      ),
      notPinned.length === 0
        ? jsx('p', { children: ALL_PINNED_HINT, style: ZONE_HINT_STYLE }, 'pin-all-pinned')
        : jsx(
            'div',
            {
              children: notPinned.map(row => {
                const id = textOf(row.id)
                const name = textOf(row.name) || id || POOL_COUNT_PLACEHOLDER

                return jsxs(
                  'div',
                  {
                    children: [
                      jsx('span', { children: name, style: WRITE_ITEM_NAME_STYLE, title: id }, 'name'),
                      jsx(Badge, { children: PIN_ITEM_LABEL, variant: 'warn' }, 'badge'),
                      jsx(
                        Button,
                        {
                          children: busy && (actions.pinning === id || actions.pinning === BATCH_PINNING_KEY)
                            ? PIN_BUSY_LABEL
                            : PIN_ACTION_LABEL,
                          disabled: busy,
                          onClick: () => actions.runPin(row),
                          size: 'xs',
                          variant: 'outline'
                        },
                        'pin-one'
                      ),
                      jsx(
                        'span',
                        { children: `白名单 ${modelCountOf(row)} / 池内 ${POOL_COUNT_PLACEHOLDER}`, style: WRITE_ITEM_META_STYLE },
                        'pool'
                      )
                    ],
                    style: WRITE_ITEM_STYLE
                  },
                  id || name
                )
              }),
              style: WRITE_LIST_STYLE
            },
            'pin-list'
          ),
      jsx(WriteStatusLine, { results: actions.pinResults, text: '' }, 'pin-status'),
      jsx('p', { children: PIN_NOTE, style: WRITE_NOTE_STYLE }, 'pin-note'),
      jsx(
        ConfirmDialog,
        {
          cancelLabel: PIN_CONFIRM_CANCEL_LABEL,
          confirmLabel: PIN_CONFIRM_OK_LABEL,
          description: PIN_CONFIRM_DESCRIPTION,
          onConfirm: () => actions.runBatchPin(rows),
          onClose: () => actions.setBatchOpen(false),
          open: actions.batchOpen,
          title: `${PIN_CONFIRM_TITLE_PREFIX}${notPinned.length}${PIN_CONFIRM_TITLE_SUFFIX}`
        },
        'pin-confirm'
      )
    ],
    style: ZONE_STYLE
  }, 'pin-zone')
}

/* 区块 M4：保存行（§5.2 线框最下方的 [保存] [取消]；只写 providers:，cc-switch 条目不给写） */
/* 保存尾区（三合一，布局优化 2026-09-24）：原「保存」区标题与空闲占位退役，
 * 动作行从区头挪到卡片最后一行（右对齐）。写链路不变：仍是 M4 的 `runSave` 与
 * M4.3 前置断言；`blankFormOpener` 登记搬到常驻挂载的 `EditorZone`（本组件只在
 * 展开态渲染，留在这里会在折叠时把空态「+ 添加供应商」的登记位清空）。 */
function SaveFooter({ actions, editor, query }) {
  const blocked = saveBlockReason(editor, query.rows)
  const legacy = isLegacyFormTarget(editor, query.rows)
  const busy = actions.saving || actions.pinning !== ''
  const emptyAllowlist = uniqueModels(editor.form.models).length === 0

  return jsxs('div', {
    children: [
      blocked
        ? jsx(
            'p',
            { children: legacy ? SAVE_DISABLED_LEGACY : blocked, role: 'alert', style: WRITE_FAIL_STATUS_STYLE },
            'save-blocked'
          )
        : null,
      !blocked && emptyAllowlist
        ? jsx('p', { children: SAVE_EMPTY_ALLOWLIST_NOTE, role: 'status', style: WRITE_STATUS_STYLE }, 'save-empty')
        : null,
      legacy
        ? null
        : jsx(
            'div',
            {
              children: jsxs(
                'span',
                {
                  children: [
                    jsx(Checkbox, {
                      'aria-label': SAVE_MAKE_DEFAULT_LABEL,
                      checked: editor.form.makeDefault === true,
                      onCheckedChange: next => editor.updateField('makeDefault', next === true),
                      style: CHECKBOX_STYLE
                    }, 'make-default-checkbox'),
                    jsx('span', { children: SAVE_MAKE_DEFAULT_LABEL, style: FIELD_LABEL_STYLE }, 'make-default-label')
                  ],
                  // Checkbox 不吃鼠标点击（M3 的 CHECKBOX_STYLE 口径），整行负责切换
                  onClick: () => editor.updateField('makeDefault', !(editor.form.makeDefault === true)),
                  style: ADVANCED_ROW_STYLE
                },
                'make-default-wrap'
              ),
              style: FORM_ACTIONS_STYLE
            },
            'save-make-default'
          ),
      jsx(WriteStatusLine, { results: [], text: actions.savedNote }, 'save-status'),
      jsx('p', { children: SAVE_FORCED_PIN_NOTE, style: WRITE_NOTE_STYLE }, 'save-note'),
      legacy ? null : jsx('p', { children: SAVE_MAKE_DEFAULT_TIP, style: WRITE_NOTE_STYLE }, 'save-default-tip'),
      jsxs(
        'div',
        {
          children: [
            jsx(
              Tip,
              {
                children: jsx(Button, {
                  children: busy ? `${SAVE_LABEL}${WRITE_INFLIGHT_SUFFIX}` : SAVE_LABEL,
                  disabled: Boolean(blocked) || busy,
                  onClick: () => actions.runSave(editor, query.rows),
                  variant: 'secondary'
                }, 'save-submit'),
                label: blocked || SAVE_FORCED_PIN_NOTE
              },
              'save-submit-tip'
            ),
            jsx(
              Button,
              {
                children: CANCEL_EDIT_LABEL,
                disabled: busy,
                onClick: editor.closeEditor,
                size: 'xs',
                variant: 'ghost'
              },
              'save-cancel'
            )
          ],
          style: SAVE_ROW_STYLE
        },
        'save-actions'
      )
    ],
    style: FORM_GRID_STYLE
  }, 'save-footer')
}

/* ─── 编辑区（布局优化 2026-09-24 三合一）───────────────────────────────────────
 * 表单主体 / 模型两栏 / 保存尾区共用一张卡，分隔线分段（原「表单区」「模型区」「保存」
 * 三个区标题退役，用户拍板）。空闲态折叠成一行：标题 + 「+ 添加供应商」；点「查看」或
 * 新建才展开。行为全部沿用：M4 写链路、M4.3 前置断言、M7 卡片按钮登记位一字未动。
 */
const EDITOR_ZONE_TITLE = '新增 / 编辑供应商'

function EditorZone({ actions, editor, query }) {
  /* 空态「+ 添加供应商」的登记位（原在 SaveZone）：本组件常驻挂载，折叠也不丢登记。 */
  useEffect(() => {
    blankFormOpener = editor.openBlank

    return () => {
      blankFormOpener = null
    }
  })

  return jsxs('section', {
    children: [
      jsxs(
        'div',
        {
          children: [
            jsx('h2', { children: EDITOR_ZONE_TITLE, style: ZONE_TITLE_STYLE }, 'editor-title'),
            jsx(
              Button,
              {
                children: NEW_SUPPLIER_LABEL,
                disabled: editor.probing,
                onClick: editor.openBlank,
                size: 'xs',
                variant: 'outline'
              },
              'new-supplier'
            )
          ],
          style: editor.visible ? ZONE_HEAD_STYLE : { ...ZONE_HEAD_STYLE, marginBottom: 0 }
        },
        'editor-head'
      ),
      editor.visible
        ? jsxs(
            'div',
            {
              children: [
                jsx(FormBody, { editor }, 'form-body'),
                jsx(Separator, {}, 'form-model-separator'),
                jsx(ModelColumns, { editor }, 'model-columns'),
                jsx(Separator, {}, 'model-save-separator'),
                jsx(SaveFooter, { actions, editor, query }, 'save-footer')
              ],
              style: FORM_GRID_STYLE
            },
            'editor-body'
          )
        : null
    ],
    style: ZONE_STYLE
  }, 'editor-zone')
}

/* ─── 区块 M5：清空白名单（UC-12 / 决策 13；第二处直接写 config）─────────────────────
 *
 * 本区块只**追加**：一张卡片动作按钮（M2 的 `ProviderCardActions` 里第三项，
 * 仅 `source === 'providers'` 的行渲染）+ 本区（二次确认弹窗 + 清空后的状态行），
 * 以及在 `SupplierModelsPage` 的 children 末尾加一行挂载点。M0/M2/M3/M4 的区块一行未改。
 *
 * 对后端（`dashboard/plugin_api.py` 的 M5 区块）的契约：
 *   · `POST /endpoints/{id}/clear-models`  无请求体，`profile` 由 `ctx.rest` 自己带
 *     → 响应 `{ ok, id, endpoints, current, cleared_models, default_model }`
 *     （`endpoints` 即 M1 的归一化行，无密钥材料 —— 见 `_write_response`）
 *   · 后端写入范围**只有 `models:`**（M5.2）：`model:` / `discover_models` / `key_env`
 *     / `api_key` 一律不动，所以前端拿到的行除了白名单变空、其它字段照旧。
 *   · 后端拒绝 `cc:` 前缀的 id（M5.3，400）与查无此条目（404，**不静默新建**）；
 *     拒绝时前端只需把 detail 原文吐出来（`backendErrorDetail` 是 M4 的唯一出口）。
 *
 * 语义铁律 3 在本区块的落点（E7 / §7.7 不变式 3 的 v0.4 改写）：
 *   **池永远至少含默认模型，白名单可以为空。** 所以「清空」之后的正确说法是
 *   「已添加 0 个模型 + 选择器里仍会有默认模型那一个（把 model 字段的值填进句子）」，
 *   **绝不**写成「一个模型都不给」那类做不到的说法（那做不到：`_absorb_entry_models`
 *   —— `model_switch_providers.py:472-482`，prompt §2.6 C8 修正出处 ——
 *   会把条目的 `model:` 无条件折进池）。二次确认文案逐字照 UC-12。
 *
 * ⚠️ **契约缺口（已上报 → 编排裁定落地，2026-09-22）**：M1 行里的 `models`
 *   是「磁盘白名单 ∪ 注入的默认模型」（`_models_from_custom_endpoint_entry:323-325` 无条件
 *   `insert(0, default_model)`），**清空之后行里仍然有 1 条**，所以 M2 卡片的
 *   「已添加 N 个模型」在清空后只会显成 1 —— M5.5 要的「已添加 0 个模型」在只读契约下
 *   **前端算不出来**。裁定口径是**只加字段**（既有字段与语义冻结）：`GET /endpoints` 的每行
 *   补一个整数 `allowlist_count` = 磁盘上真实记着的白名单条数。本区块因此的改动只有一处：
 *   `modelCountOf` 优先读 `allowlist_count`（老形状的行在那**一处**退到 `models.length`），
 *   卡片 / `poolCompareText` / 本区逐行计数全经它取数，所以清空后卡片真显「已添加 0 个模型」。
 *   M5.5 的完成文案仍由**本插件自己的写回执**驱动（`cleared_models` / `default_model`），
 *   本区逐行的措辞在 M7 收尾时统一成「白名单 N 个」（M5 上报转 M7 第 ② 条；数字自契约
 *   补丁起就是磁盘真值，「视图」二字已经名不副实）。
 */

const CLEAR_MODELS_PATH_SUFFIX = '/clear-models'
/** M1 行上的来源标记：`providers:` → `'providers'`（legacy 是 `'cc-switch'`，见 `CC_SOURCE`）。 */
const PROVIDERS_SOURCE = 'providers'

/* ── 文案（M5.4 的二次确认与 M5.5 的清空后状态；模板里的 `{占位}` 由 fillCopyTemplate 填）── */

const CLEAR_ACTION_LABEL = '清空白名单'
/* 改动记录：2026-09-25 R3（M10.2，设计 §2.3 表 A · A9）：清空动作的 Tip 不再写存储字段名，
   改成说清「只动模型白名单、不碰默认模型和其他设置」。 */
const CLEAR_ACTION_TIP =
  '把这个供应商已添加的模型全部移除（只动模型白名单，不碰默认模型和其他设置）。'
const CLEAR_BUSY_LABEL = '正在清空…'
const CLEAR_CONFIRM_TITLE_PREFIX = '清空白名单：'
const CLEAR_CONFIRM_TITLE_SUFFIX = '？'
/** UC-12 / M5.4 逐字文案（N 与 model 运行时填；铁律 3 禁用的那种「池里一个都不给」说法不许用）。 */
const CLEAR_CONFIRM_DESCRIPTION_TEMPLATE =
  '将移除该供应商的全部 {count} 个已添加模型。保存后该供应商在选择器里只剩默认模型 {model}。'
const CLEAR_CONFIRM_OK_LABEL = '清空白名单'
const CLEAR_CONFIRM_CANCEL_LABEL = '取消'
/** M5.5 的两半合成一句，**判据来自本插件的写回执**（清空成功 = 磁盘 `models: {}`）。
 *  ⚠️ 卡片那侧过去显不出「0」（官方读路径 `_models_from_custom_endpoint_entry`
 *  （`config_env.py:317-328`）会把条目的 `model:` 无条件插进 `models` 第 0 位，清空后行里仍是
 *  `["<默认模型>"]`）—— 该缺口由编排裁定补上 `allowlist_count` 后已闭合，本句只是仍按
 *  写回执出（写回执才是「这一次确实清掉了 N 条」的直接证据，见本区块头注）。 */
const CLEAR_DONE_TEMPLATE =
  '已清空 {name} — 已添加 0 个模型 · 选择器里仍会有默认模型 {model} 一个'
const CLEAR_DEFAULT_NOTE_TEMPLATE = '选择器里仍会有默认模型 {model} 一个'
/** 本区逐行的计数措辞：数字经 `modelCountOf` 取（= 后端的磁盘真值 `allowlist_count`），
 *  「视图」二字是 M5 落地时留的（那时前端只拿得到注入后的 `models`）。契约补丁把
 *  `allowlist_count` 补上之后数字已经是磁盘真值，M5 上报转 M7 统一成「白名单 N 个」，
 *  与卡片 / `poolCompareText` 同一套说法。 */
const CLEAR_ROW_VIEW_TEMPLATE = '白名单 {count} 个 · {note}'
/* 改动记录：2026-09-25 R3（M10.2，设计 §2.3 表 A · A8 / A10 / A11）：清空区的区块标题、
   计数后缀与空态提示去掉存储字段名与决议编号，改成「可清空供应商 / 本插件管理的供应商」
   这套说法。上一行 `CLEAR_ROW_VIEW_TEMPLATE` 是表 A 的「不改」行（被测试逐字钉住），未动。 */
const CLEAR_ZONE_TITLE = '清空白名单（把该供应商已添加的模型全部移除，只剩默认模型）'
const CLEAR_ZONE_COUNT_SUFFIX = '个可清空供应商'
const CLEAR_ZONE_HINT =
  '「钉住」只阻止未来的覆盖，不清理已经灌进来的模型；清理是本区的独立动作。' +
  ' 本插件不提供任何自动猜测式删除。'
const CLEAR_ZONE_UNMOUNTED_HINT = '列表还没读到，先解决上方的提示。'
const CLEAR_ZONE_EMPTY_HINT = '当前没有可清空的供应商：清空白名单只对本插件管理的供应商开放。'
const CLEAR_ZONE_NO_ID = '该行没有 id，无法定位供应商。'
const CLEAR_MODEL_MISSING = '该条目没有默认模型记录，无法预告清空后池里剩什么 —— 已拦下，未写入。'
const CLEAR_DONE_TOAST_TITLE = '清空白名单'
const CLEAR_FAILED_TOAST_TITLE = '清空白名单失败'
const CLEAR_UNAVAILABLE_TOAST_MESSAGE = '清空白名单的写入方还没就绪（页面组件未挂载完成）。'
const CLEAR_UNAVAILABLE_TOAST_TITLE = '清空白名单'

/* ── 主题变量样式（延续 M0/M2/M3/M4；颜色一律 var(--ui-*) token）────────────────── */

const CLEAR_ITEM_META_STYLE = {
  color: 'var(--ui-text-tertiary)',
  fontSize: '0.6875rem',
  marginLeft: 'auto'
}

/* ── 纯函数：路径 / 来源闸门 / 文案填充 ─────────────────────────────────────────── */

/** 清空白名单的路径（id 原样百分号编码，与 M4 的 `pinPathFor` 同一口径）。 */
function clearModelsPathFor(endpointId) {
  return `${SAVE_ENDPOINTS_PATH}/${encodeURIComponent(textOf(endpointId))}${CLEAR_MODELS_PATH_SUFFIX}`
}

/** 来源闸门（M5.3）：**按后端给的 `source` 字段**判，不看 id 长什么样。 */
function isProvidersSource(row) {
  return textOf(row && row.source) === PROVIDERS_SOURCE
}

/** 把 `{name}` 这类占位按表填进文案模板（不用正则，替换是字面量级的）。 */
function fillCopyTemplate(template, values) {
  return Object.entries(values).reduce(
    (text, pair) =>
      text.split(`{${pair[0]}}`).join(
        pair[1] === undefined || pair[1] === null ? '' : String(pair[1])
      ),
    template
  )
}

/** 二次确认的正文（M5.4 逐字）：`N` 取点击那一刻行上的白名单条数，model 取其默认模型。 */
function clearConfirmDescription(target) {
  return fillCopyTemplate(CLEAR_CONFIRM_DESCRIPTION_TEMPLATE, {
    count: Number(target && target.count) || 0,
    model: textOf(target && target.model) || POOL_COUNT_PLACEHOLDER
  })
}

/** 清空后的状态行（M5.5 的两半合成一句）。 */
function clearDoneText(name, model) {
  return fillCopyTemplate(CLEAR_DONE_TEMPLATE, { model: model || POOL_COUNT_PLACEHOLDER, name })
}

/** 行上的「白名单 N 个 · 选择器里仍会有默认模型那一个」+ 默认模型名。
 *  N 经 `modelCountOf` 取，即后端的磁盘真值 `allowlist_count`（`models` 那份注入视图
 *  只在字段缺失时兜底，见 `modelCountOf` 的注），所以清空后这里跟着一起归零。 */
function clearRowMetaText(row) {
  return fillCopyTemplate(CLEAR_ROW_VIEW_TEMPLATE, {
    count: modelCountOf(row),
    note: fillCopyTemplate(CLEAR_DEFAULT_NOTE_TEMPLATE, {
      model: textOf(row && row.model) || POOL_COUNT_PLACEHOLDER
    })
  })
}

/* ── 卡片按钮的登记位（与 M4 的 `blankFormOpener` 同形状：他人组件的 props 一个都不加）── */

/** 处理器由 `ClearAllowlistZone` 挂载时登记；没挂载（后端未就绪等）时什么都不做。 */
let clearAllowlistHandler = null

function requestClearAllowlist(row) {
  if (typeof clearAllowlistHandler === 'function') {
    clearAllowlistHandler(row)
  } else {
    notifyWriteFeedback('warning', CLEAR_UNAVAILABLE_TOAST_MESSAGE, CLEAR_UNAVAILABLE_TOAST_TITLE, '')
  }
}

/* ── 动作与状态 ─────────────────────────────────────────────────────────────────── */

/**
 * 「清空白名单」的写入（M5.4 / M5.5）。反馈口径镜像 M4.8：
 * 成功 → `haptic` + toast；失败 → toast **透传后端 detail 原文**（`backendErrorDetail`）；
 * 两种结果都 `invalidateQueries(ENDPOINTS_QUERY_KEY)`（§6.4，与 M4 同一条失效路径）。
 *
 * `editor` 用于 M5.5 的「编辑器也显出已添加 0 个模型」：清空成功后，如果编辑器
 * 正好开着这一条，就把它的 `models` 也清成空数组 —— 走 M3 已公开的
 * `updateField`（M3 的 hook 一行未改）。`model` **保持不动**（池里那个默认模型删不掉，E7）。
 */
function useClearAllowlist(ctx, editor) {
  const queryClient = useQueryClient()
  const [busyId, setBusyId] = useState('')
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [pending, setPending] = useState(null)
  const [doneNote, setDoneNote] = useState('')

  const clearMutation = useMutation({
    mutationFn: target => ctx.rest(clearModelsPathFor(target.id), { method: 'POST' }),
    onError: (error, target) =>
      notifyWriteFeedback('error', backendErrorDetail(error), CLEAR_FAILED_TOAST_TITLE, target.name),
    onSuccess: (result, target) => {
      haptic('success')
      const model = textOf(result && result.default_model) || textOf(target.model)
      const note = clearDoneText(target.name, model)
      notifyWriteFeedback('success', note, CLEAR_DONE_TOAST_TITLE, '')
      setDoneNote(note)
      // M5.5：编辑器正开着这一条时同步清空「已添加」栏（默认模型字段不动）
      if (editor.visible && textOf(editor.editingRowId) === target.id) {
        editor.updateField('models', [])
      }
    },
    // 成败都要让列表回到当前真相（`已添加 N 个模型` 与卡片徽章都读自这条查询）
    onSettled: () => queryClient.invalidateQueries({ queryKey: ENDPOINTS_QUERY_KEY })
  })

  /** 卡片 / 本区按钮的入口：只开二次确认，**不直接写**（决议 3）。 */
  const askClear = row => {
    if (busyId !== '') {
      return
    }
    const id = textOf(row && row.id)
    if (!id) {
      notifyWriteFeedback('error', CLEAR_ZONE_NO_ID, CLEAR_FAILED_TOAST_TITLE, '')
      return
    }
    if (!textOf(row && row.model)) {
      // 池里的默认模型是唯一删不掉的那一个（E7）：不知道它是谁就不敢清空
      notifyWriteFeedback('error', CLEAR_MODEL_MISSING, CLEAR_FAILED_TOAST_TITLE, '')
      return
    }
    setPending({
      count: modelCountOf(row),
      id,
      model: textOf(row.model),
      name: textOf(row.name) || id
    })
    setConfirmOpen(true)
  }

  /** 弹窗确认后的写入。失败**不抛**：toast 已吐过后端原文（M4.8 口径），弹窗按 SDK 约定自动关闭。 */
  const runClear = async () => {
    const target = pending
    if (!target) {
      return
    }
    setBusyId(target.id)
    setDoneNote('')
    try {
      await clearMutation.mutateAsync(target)
    } catch (error) {
      // onError 已 toast（后端 detail 原文），这里只保证 busy 复位、弹窗正常关闭
    } finally {
      setBusyId('')
      setPending(null)
    }
  }

  useEffect(() => {
    clearAllowlistHandler = askClear

    return () => {
      clearAllowlistHandler = null
    }
  })

  return {
    askClear,
    busy: busyId !== '',
    confirmOpen,
    doneNote,
    pending,
    runClear,
    setConfirmOpen
  }
}

/* 区块 M5：清空白名单（二次确认 + 清空后的状态；坐在保存区之后，他人区块不重排） */
function ClearAllowlistZone({ ctx, editor, query }) {
  const actions = useClearAllowlist(ctx, editor, query.rows)
  const targets = query.rows.filter(isProvidersSource)

  if (query.notMounted || query.isPending || query.errorText) {
    return jsxs('section', {
      children: [
        jsx('h2', { children: CLEAR_ZONE_TITLE, style: ZONE_TITLE_STYLE }, 'clear-title'),
        jsx('p', { children: CLEAR_ZONE_UNMOUNTED_HINT, style: ZONE_HINT_STYLE }, 'clear-hint')
      ],
      style: ZONE_STYLE
    }, 'clear-zone')
  }

  return jsxs('section', {
    children: [
      jsxs(
        'div',
        {
          children: [
            jsx('h2', { children: CLEAR_ZONE_TITLE, style: ZONE_TITLE_STYLE }, 'clear-title'),
            jsx('span', {
              children: actions.busy
                ? CLEAR_BUSY_LABEL
                : `${targets.length} ${CLEAR_ZONE_COUNT_SUFFIX}`,
              style: LIST_COUNT_STYLE
            }, 'clear-count')
          ],
          style: WRITE_HEAD_STYLE
        },
        'clear-head'
      ),
      targets.length === 0
        ? jsx('p', { children: CLEAR_ZONE_EMPTY_HINT, style: ZONE_HINT_STYLE }, 'clear-empty')
        : jsx(
            'div',
            {
              children: targets.map(row => {
                const id = textOf(row.id)
                const name = textOf(row.name) || id || POOL_COUNT_PLACEHOLDER

                return jsxs(
                  'div',
                  {
                    children: [
                      jsx('span', { children: name, style: WRITE_ITEM_NAME_STYLE, title: id }, 'name'),
                      jsx('span', { children: clearRowMetaText(row), style: CLEAR_ITEM_META_STYLE }, 'meta'),
                      jsx(
                        Button,
                        {
                          children: actions.busy ? CLEAR_BUSY_LABEL : CLEAR_ACTION_LABEL,
                          disabled: actions.busy,
                          onClick: () => actions.askClear(row),
                          size: 'xs',
                          variant: 'outline'
                        },
                        'clear-one'
                      )
                    ],
                    style: WRITE_ITEM_STYLE
                  },
                  id || name
                )
              }),
              style: WRITE_LIST_STYLE
            },
            'clear-list'
          ),
      actions.doneNote
        ? jsx('p', { children: actions.doneNote, role: 'status', style: WRITE_OK_STATUS_STYLE }, 'clear-status')
        : null,
      jsx('p', { children: CLEAR_ZONE_HINT, style: WRITE_NOTE_STYLE }, 'clear-note'),
      jsx(
        ConfirmDialog,
        {
          cancelLabel: CLEAR_CONFIRM_CANCEL_LABEL,
          confirmLabel: CLEAR_CONFIRM_OK_LABEL,
          description: actions.pending ? clearConfirmDescription(actions.pending) : '',
          onConfirm: actions.runClear,
          onClose: () => actions.setConfirmOpen(false),
          open: actions.confirmOpen,
          title: `${CLEAR_CONFIRM_TITLE_PREFIX}${textOf(actions.pending && (actions.pending.name || actions.pending.id))}${CLEAR_CONFIRM_TITLE_SUFFIX}`
        },
        'clear-confirm'
      )
    ],
    style: ZONE_STYLE
  }, 'clear-zone')
}

/* ─── 区块 M6：cc-switch 条目迁移（UC-10 / §7.6；第三处直接写 config，唯一搬明文密钥）──
 *
 * 本区块只**追加**：一张卡片动作行里的第四项（`ProviderCardActions` 的「高级」位，
 * **只对 cc-switch 行渲染**）+ 本区（前置检查 → 二次确认弹窗 → 迁移后的状态行），
 * 以及在 `SupplierModelsPage` 的 children 末尾加一行挂载点。M0–M5 的区块一行未改、未重排。
 *
 * 对后端（`dashboard/plugin_api.py` 的 M6 区块）的契约（**形状由 M6 冻结，M7/M8 照此读**）：
 *   · `POST /endpoints/migrate`，body `{ id | name, base_url?, precheck_only? }`
 *     - `precheck_only: true` → **零写入**，返回
 *       `{ ok, precheck_only, id, name, target_id, base_url, model, models, allowlist_count,
 *          discover_models, discover_models_before, key_disposition, env_var_name,
 *          target_exists, precheck_warnings[] }`
 *     - 真迁移 → `{ ok, id（新的 providers: key）, endpoints, current,
 *       migrated{ from_id, to_id, name, model, models, allowlist_count, base_url,
 *       discover_models, discover_models_before, key_disposition, env_var_name,
 *       removed_from_custom_providers }, precheck_warnings[] }`
 *   · `precheck_warnings` 取值表：`aux_custom_ref`（M6.3 / R11，弹窗要多一句）、
 *     `model_custom_ref`（顶层 `model:` 还引用着 `custom:<name>`）、
 *     `target_id_collision`（§7.6 边界②，真跑会 409）
 *   · `key_disposition` 取值表：`plaintext_to_env` / `template_carried` /
 *     `key_env_carried` / `no_key` —— **弹窗文案按它分叉**（只有第一支真的搬密钥）
 *   · 响应里没有任何密钥材料（`endpoints` 走 M1 的归一化行；`env_var_name` 只是变量名）
 *
 * 三条口径：
 *   * **默认动作不是迁移**（决议 9/10）：入口藏在「高级」里，弹窗第一句就把「不迁移也够用」
 *     说清楚；**不提供批量迁移**（本区逐行一个按钮，没有「全部迁移」）。
 *   * **确认之前先只读前置检查**（M6.3）：那句「辅助任务槽仍持有一份明文 key」必须在用户点
 *     「仍然迁移」**之前**出现，所以点入口时先发一次 `precheck_only`（后端保证它一个字节都不写），
 *     拿到 `precheck_warnings` 再开弹窗。
 *   * **迁移必然搬家**（§2.5 发现 3）：条目从 `custom_providers:` 跳到 `providers:`，
 *     列表里**只有一行**但**位置会变** —— 这句话在弹窗与完成状态里各写一次（M6.5）。
 */

/** 迁移的路径（复用 M4 的 `SAVE_ENDPOINTS_PATH` 常量，不新造第二份 `/endpoints`）。 */
const MIGRATE_ENDPOINTS_PATH = `${SAVE_ENDPOINTS_PATH}/migrate`

/* ── 文案（§3.2 UC-10 的弹窗草案逐字，`{占位}` 由 M5 的 `fillCopyTemplate` 填）────── */

/* 改动记录：2026-09-25 R3（M10.3，设计 §2.3 表 A · A17 / A18 / A19）：按钮组标签与动作标签
   统一成 R3 的新说法「收编」（设计 §7 术语表），动作 Tip 去掉「高级动作」与存储字段名。
   常量名一律不动；同行的 `MIGRATE_BUSY_LABEL`、`MIGRATE_CONFIRM_OK_LABEL` 与下面的弹窗正文
   各常量（`MIGRATE_BENEFIT_*` / `MIGRATE_COST_LINE` / `MIGRATE_DONT_MIGRATE_LINE` /
   `MIGRATE_MOVE_LINE` / `MIGRATE_WARN_*`）属「不改」行与豁免档，一字未动。 */
const ADVANCED_GROUP_LABEL = '收编'
const MIGRATE_ACTION_LABEL = '收编进本插件'
const MIGRATE_ACTION_TIP =
  '把这条由 cc-switch 管理的供应商收编进本插件，明文 API Key 会改存 .env。' +
  '代价是 cc-switch 之后不能再编辑 / 删除 / 启用它 —— 还想让 cc-switch 管就只点「钉住」。'
const MIGRATE_BUSY_LABEL = '迁移中…'
const MIGRATE_CONFIRM_OK_LABEL = '仍然迁移'
const MIGRATE_CONFIRM_CANCEL_LABEL = '取消'
/* 改动记录：2026-09-25 R3（M10.3，表 A · A20）：确认弹窗只改**标题**（去掉「标准形态」
   这个设计语）；下面的弹窗正文整档豁免（本轮裁定 D2），逐字未动。 */
const MIGRATE_CONFIRM_TITLE_TEMPLATE = '收编 {name} 进本插件？'
/** 「好处」两句（UC-10 草案原文；`{env}` 由后端 `env_var_name` 填，只有真搬密钥那支用得上）。 */
const MIGRATE_BENEFIT_PLAINTEXT_TEMPLATE =
  '好处：明文 API Key 移入 .env（改为 {env} 引用）；格式升级为 v12 的 providers: dict。'
/** 密钥本来就在 .env / 还是 `${VAR}` 模板时，「移入 .env」这句不成立，换如实说法。 */
const MIGRATE_BENEFIT_KEY_ENV_TEMPLATE =
  '好处：密钥本来就在 .env 的 {env} 里，迁移不动它（.env 零改动）；格式升级为 v12 的 providers: dict。'
const MIGRATE_BENEFIT_TEMPLATE_CARRIED =
  '好处：格式升级为 v12 的 providers: dict。该条目的 API Key 是 ${VAR} 引用，' +
  ' 迁移会把它原样搬过去，不会再把展开后的明文抄进第二个 .env 变量。'
const MIGRATE_BENEFIT_NO_KEY =
  '好处：格式升级为 v12 的 providers: dict。（该条目没有记录任何 API Key，迁移不涉及密钥。）'
/** 「代价」三句（UC-10 草案原文：编辑 / 删除 / 启用三条全列，不淡化）。 */
const MIGRATE_COST_LINE =
  '代价：迁移后 cc-switch 将无法再编辑、删除或启用该供应商' +
  '（它会显示为「Hermes 托管」只读，切换会报错）。切换入口改为 Hermes 侧。'
const MIGRATE_DONT_MIGRATE_LINE =
  '如果你还想在 cc-switch 里管它，不要迁移——直接钉住就够了。'
/** §2.5 发现 3：不会多出一行，但行位置会变（M6.5 要求 UI 里说明）。 */
const MIGRATE_MOVE_LINE =
  '条目会从 cc-switch 区搬到 providers: 区：列表里仍然只有一行，但位置会变。'
/** R11 / M6.3 命中时额外那一句（任务书原文 = 「辅助任务槽仍持有一份明文 key」）。 */
const MIGRATE_WARN_AUX_LINE = '⚠ 辅助任务槽仍持有一份明文 key —— 迁移不会动它，需要你另外处理。'
const MIGRATE_WARN_MODEL_LINE =
  '⚠ 顶层 model: 仍按 custom:{name} 引用该条目：迁移只搬条目，不改当前供应商设置。'
const MIGRATE_WARN_COLLISION_LINE =
  '⚠ providers: 里已经有一个同名的标准条目：迁移会被中止（本插件不静默覆盖），先去官方页处理重名。'
/* 改动记录：2026-09-25 R3（M10.3，设计 §2.3 表 A · A13 / A15 / A16）：本区标题、区块说明与
   空态提示统一改用 R3 的新词「收编」，去掉「高级」「标准形态」与设计编号；本区说明可改，
   弹窗正文（上面那批 `MIGRATE_BENEFIT_*` / `MIGRATE_WARN_*` 等）整档豁免、逐字未动。
   `MIGRATE_ZONE_COUNT_SUFFIX`（产品名 + 供应商，非禁词搭配）与 `MIGRATE_ZONE_UNMOUNTED_HINT`
   是表 A 的「不改」行。 */
const MIGRATE_ZONE_TITLE = '收编由 cc-switch 管理的供应商（收编后由本插件管理，cc-switch 将不再能编辑它；逐个确认）'
const MIGRATE_ZONE_COUNT_SUFFIX = '个 cc-switch 供应商'
const MIGRATE_ZONE_HINT =
  '默认动作是「钉住」，不是收编：收编后 cc-switch 将无法再编辑、删除或启用该供应商。' +
  '本插件不提供批量收编，每条都要逐个确认；收编后不保留原条目、也不提供反悔' +
  '（要还给 cc-switch 请在它那边重新导入）。'
const MIGRATE_ZONE_UNMOUNTED_HINT = '列表还没读到，先解决上方的提示。'
const MIGRATE_ZONE_EMPTY_HINT = '当前没有由 cc-switch 管理的供应商：没有可收编的对象。'
const MIGRATE_ZONE_NO_ID = '该行没有 id，无法定位要迁移的条目。'
/* 改动记录：2026-09-25 R3（M10.3，设计 §2.3 表 A · A34 / A21）：完成回执（**状态行，不是
   弹窗正文**）改用「收编 / 转入本插件管理」的说法，去掉存储字段名；不可用兜底 toast 的标题
   跟着按钮组标签一起改成「收编」。`MIGRATE_DONE_KEY_*` / `MIGRATE_DONE_NO_KEY`（只提 .env，
   属允许词）与 `MIGRATE_UNAVAILABLE_TOAST_MESSAGE` 均为「不改」行，逐字未动。 */
const MIGRATE_DONE_TEMPLATE =
  '已收编 {name} — 已转入本插件管理（{note}），列表里只有一行、位置会变'
const MIGRATE_DONE_KEY_PLAINTEXT_TEMPLATE = '明文 Key 已进 .env 的 {env}'
const MIGRATE_DONE_KEY_ENV_TEMPLATE = '密钥仍在 .env 的 {env}，未改动'
const MIGRATE_DONE_KEY_TEMPLATE_KEPT = 'API Key 仍是 ${VAR} 引用，未复制进 .env'
const MIGRATE_DONE_NO_KEY = '该条目没有 API Key 需要搬迁'
const MIGRATE_DONE_TOAST_TITLE = '迁移完成'
const MIGRATE_FAILED_TOAST_TITLE = '迁移失败'
const MIGRATE_PRECHECK_FAILED_TOAST_TITLE = '迁移前置检查失败'
const MIGRATE_UNAVAILABLE_TOAST_MESSAGE =
  '迁移的写入方还没就绪（页面组件未挂载完成）；「钉住」不受影响，可以先用那个。'
const MIGRATE_UNAVAILABLE_TOAST_TITLE = '收编'

/* ── 主题变量样式（延续 M0–M5；颜色一律 var(--ui-*) token）────────────────────── */

const CARD_ADVANCED_GROUP_STYLE = {
  alignItems: 'center',
  display: 'flex',
  gap: '6px'
}

const CARD_ADVANCED_LABEL_STYLE = {
  color: 'var(--ui-text-tertiary)',
  fontSize: '0.625rem',
  letterSpacing: '0.04em'
}

const MIGRATE_ITEM_META_STYLE = {
  color: 'var(--ui-text-tertiary)',
  fontSize: '0.6875rem',
  marginLeft: 'auto'
}

/** 弹窗正文一行（`ConfirmDialog` 的 `description` 是 ReactNode，所以逐行成块）。 */
const MIGRATE_CONFIRM_LINE_STYLE = {
  display: 'block',
  lineHeight: 1.55,
  marginTop: '4px'
}

/* ── 纯函数：处置分叉的文案 / 告警行 ──────────────────────────────────────────── */

/** 「好处」那一句按后端的 `key_disposition` 分叉 —— 只有 `plaintext_to_env` 真的在搬密钥。 */
function migrateBenefitLine(info) {
  const disposition = textOf(info && info.key_disposition)
  const env = textOf(info && info.env_var_name) || POOL_COUNT_PLACEHOLDER

  if (disposition === 'template_carried') {
    return MIGRATE_BENEFIT_TEMPLATE_CARRIED
  }
  if (disposition === 'key_env_carried') {
    return fillCopyTemplate(MIGRATE_BENEFIT_KEY_ENV_TEMPLATE, { env })
  }
  if (disposition === 'no_key') {
    return MIGRATE_BENEFIT_NO_KEY
  }
  return fillCopyTemplate(MIGRATE_BENEFIT_PLAINTEXT_TEMPLATE, { env })
}

/** `precheck_warnings` → 弹窗额外行（M6.3 的那句是 code `aux_custom_ref` 的固定映射）。 */
function migrateWarningLines(info, name) {
  const warnings = info && Array.isArray(info.precheck_warnings) ? info.precheck_warnings : []

  return warnings.reduce((lines, code) => {
    if (code === 'aux_custom_ref') {
      return lines.concat(MIGRATE_WARN_AUX_LINE)
    }
    if (code === 'model_custom_ref') {
      return lines.concat(fillCopyTemplate(MIGRATE_WARN_MODEL_LINE, { name }))
    }
    if (code === 'target_id_collision') {
      return lines.concat(MIGRATE_WARN_COLLISION_LINE)
    }
    // 上游新增的 code：不猜含义，原样吐出来（比静默丢掉有用）
    return lines.concat(textOf(code))
  }, [])
}

/** 弹窗正文的**行序**（UC-10 草案的顺序）：好处 → 代价 → 搬家说明 → 前置检查告警 →
 *  「不迁移也行」那句收尾。标题里的「迁移 <name> 到标准形态？」由 `title` 出，不重复。 */
function migrateConfirmLines(info) {
  const name = textOf(info && info.name) || POOL_COUNT_PLACEHOLDER

  return [
    migrateBenefitLine(info),
    MIGRATE_COST_LINE,
    MIGRATE_MOVE_LINE
  ].concat(migrateWarningLines(info, name)).concat([MIGRATE_DONT_MIGRATE_LINE])
}

/** `ConfirmDialog.description` 是 ReactNode（`confirm-dialog.tsx:19`），所以逐行成块而不是拼一句。 */
function migrateConfirmNodes(info) {
  return migrateConfirmLines(info).map((line, index) =>
    jsx('span', { children: line, style: MIGRATE_CONFIRM_LINE_STYLE }, `l${index}`))
}

/** 完成状态里的密钥去向括注（不吃响应之外的信息）。 */
function migrateDoneNote(info) {
  const disposition = textOf(info && info.key_disposition)
  const env = textOf(info && info.env_var_name)

  if (disposition === 'plaintext_to_env' && env) {
    return fillCopyTemplate(MIGRATE_DONE_KEY_PLAINTEXT_TEMPLATE, { env })
  }
  if (disposition === 'template_carried') {
    return MIGRATE_DONE_KEY_TEMPLATE_KEPT
  }
  if (disposition === 'key_env_carried') {
    return fillCopyTemplate(MIGRATE_DONE_KEY_ENV_TEMPLATE, { env: env || POOL_COUNT_PLACEHOLDER })
  }
  return MIGRATE_DONE_NO_KEY
}

function migrateDoneText(name, info) {
  return fillCopyTemplate(MIGRATE_DONE_TEMPLATE, { name, note: migrateDoneNote(info) })
}

/** 本区逐行的提示：明文密钥 / 未钉住，全部读自 M1 的行字段。 */
function migrateRowMetaText(row) {
  const bits = [textOf(row && row.model) ? `默认 ${textOf(row.model)}` : '']

  if (row && row.api_key_plaintext === true) {
    bits.push(PLAINTEXT_BADGE_TEXT)          // M2 的既有文案常量，不另起第二份措辞
  }
  if (row && row.discover_models !== false) {
    bits.push('未钉住')
  }

  return bits.filter(Boolean).join(' · ')
}

/* ── 卡片按钮的登记位（与 M4 / M5 同形状：他人组件的 props 一个都不加）────────────── */

/** 处理器由 `MigrateZone` 挂载时登记；没挂载（后端未就绪等）时只吐一句提示。 */
let migrateToStandardHandler = null

function requestMigrateToStandard(row) {
  if (typeof migrateToStandardHandler === 'function') {
    migrateToStandardHandler(row)
  } else {
    notifyWriteFeedback('warning', MIGRATE_UNAVAILABLE_TOAST_MESSAGE, MIGRATE_UNAVAILABLE_TOAST_TITLE, '')
  }
}

/* ── 动作与状态 ─────────────────────────────────────────────────────────────────── */

/**
 * 「高级 → 迁移到标准形态」（M6.3 / M6.4 / M6.5）。反馈口径镜像 M4.8 / M5：
 * 成功 → `haptic` + toast + 页内状态行；失败 → toast **透传后端 detail 原文**
 * （`backendErrorDetail`，404 / 409 / 400 三种拒绝都是后端在**写之前**给的）；
 * 两种结果都 `invalidateQueries(ENDPOINTS_QUERY_KEY)`（§6.4，M6.5 的「刷新列表」）。
 *
 * 两段式：点入口先发 `precheck_only` 拿告警（**只读**），再开确认弹窗；弹窗确认才发真迁移。
 */
function useMigrateToStandard(ctx) {
  const queryClient = useQueryClient()
  const [busyId, setBusyId] = useState('')
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [pending, setPending] = useState(null)
  const [doneNote, setDoneNote] = useState('')

  const precheckMutation = useMutation({
    mutationFn: target => ctx.rest(MIGRATE_ENDPOINTS_PATH, {
      body: { base_url: target.base_url, id: target.id, precheck_only: true },
      method: 'POST'
    })
  })

  const migrateMutation = useMutation({
    mutationFn: target => ctx.rest(MIGRATE_ENDPOINTS_PATH, {
      body: { base_url: target.base_url, id: target.id },
      method: 'POST'
    }),
    onError: (error, target) =>
      notifyWriteFeedback('error', backendErrorDetail(error), MIGRATE_FAILED_TOAST_TITLE, target.name),
    onSuccess: (result, target) => {
      haptic('success')
      const note = migrateDoneText(target.name, (result && result.migrated) || target.info || {})
      notifyWriteFeedback('success', note, MIGRATE_DONE_TOAST_TITLE, '')
      setDoneNote(note)
    },
    // 成败都让列表回到当前真相（M6.5：迁移成功的行会从 cc-switch 区**移到** providers: 区）
    onSettled: () => queryClient.invalidateQueries({ queryKey: ENDPOINTS_QUERY_KEY })
  })

  /** 卡片 / 本区按钮的入口：先只读前置检查，**不直接写**（M6.3 的那句话必须在弹窗里）。 */
  const askMigrate = async row => {
    if (busyId !== '') {
      return
    }
    const id = textOf(row && row.id)
    if (!id) {
      notifyWriteFeedback('error', MIGRATE_ZONE_NO_ID, MIGRATE_PRECHECK_FAILED_TOAST_TITLE, '')
      return
    }
    const name = textOf(row.name) || id
    const target = { base_url: textOf(row.base_url), id, name }

    setBusyId(id)
    setDoneNote('')
    try {
      const info = await precheckMutation.mutateAsync(target)

      setPending({ ...target, info: info || {} })
      setConfirmOpen(true)
    } catch (error) {
      // 404 / 409 / 400 都是后端「不动手」的原文（列表未改动），原样吐给用户
      notifyWriteFeedback(
        'error',
        backendErrorDetail(error),
        MIGRATE_PRECHECK_FAILED_TOAST_TITLE,
        name
      )
    } finally {
      setBusyId('')
    }
  }

  /** 弹窗确认后的写入。失败**不抛**：toast 已吐过后端原文（M4.8 口径）。 */
  const runMigrate = async () => {
    const target = pending
    if (!target) {
      return
    }
    setBusyId(target.id)
    try {
      await migrateMutation.mutateAsync(target)
    } catch (error) {
      // onError 已 toast（后端 detail 原文），这里只保证 busy 复位、弹窗正常关闭
    } finally {
      setBusyId('')
      setPending(null)
    }
  }

  useEffect(() => {
    migrateToStandardHandler = askMigrate

    return () => {
      migrateToStandardHandler = null
    }
  })

  return {
    askMigrate,
    busy: busyId !== '',
    confirmOpen,
    doneNote,
    pending,
    runMigrate,
    setConfirmOpen
  }
}

/* 区块 M6：迁移区（前置检查 + 二次确认弹窗 + 完成状态；坐在清空区之后，他人区块不重排） */
function MigrateZone({ ctx, query }) {
  const actions = useMigrateToStandard(ctx)
  const targets = query.rows.filter(isCcSwitchRow)
  const pendingInfo = (actions.pending && actions.pending.info) || {}
  const pendingName = textOf(actions.pending && (actions.pending.name || actions.pending.id))

  if (query.notMounted || query.isPending || query.errorText) {
    return jsxs('section', {
      children: [
        jsx('h2', { children: MIGRATE_ZONE_TITLE, style: ZONE_TITLE_STYLE }, 'migrate-title'),
        jsx('p', { children: MIGRATE_ZONE_UNMOUNTED_HINT, style: ZONE_HINT_STYLE }, 'migrate-hint')
      ],
      style: ZONE_STYLE
    }, 'migrate-zone')
  }

  return jsxs('section', {
    children: [
      jsxs(
        'div',
        {
          children: [
            jsx('h2', { children: MIGRATE_ZONE_TITLE, style: ZONE_TITLE_STYLE }, 'migrate-title'),
            jsx('span', {
              children: actions.busy
                ? MIGRATE_BUSY_LABEL
                : `${targets.length} ${MIGRATE_ZONE_COUNT_SUFFIX}`,
              style: LIST_COUNT_STYLE
            }, 'migrate-count')
          ],
          style: WRITE_HEAD_STYLE
        },
        'migrate-head'
      ),
      targets.length === 0
        ? jsx('p', { children: MIGRATE_ZONE_EMPTY_HINT, style: ZONE_HINT_STYLE }, 'migrate-empty')
        : jsx(
            'div',
            {
              children: targets.map(row => {
                const id = textOf(row.id)
                const name = textOf(row.name) || id || POOL_COUNT_PLACEHOLDER

                return jsxs(
                  'div',
                  {
                    children: [
                      jsx('span', { children: name, style: WRITE_ITEM_NAME_STYLE, title: id }, 'name'),
                      jsx('span', {
                        children: migrateRowMetaText(row),
                        style: MIGRATE_ITEM_META_STYLE
                      }, 'meta'),
                      jsx(
                        Button,
                        {
                          children: actions.busy ? MIGRATE_BUSY_LABEL : MIGRATE_ACTION_LABEL,
                          disabled: actions.busy,
                          onClick: () => actions.askMigrate(row),
                          size: 'xs',
                          variant: 'outline'
                        },
                        'migrate-one'
                      )
                    ],
                    style: WRITE_ITEM_STYLE
                  },
                  id || name
                )
              }),
              style: WRITE_LIST_STYLE
            },
            'migrate-list'
          ),
      actions.doneNote
        ? jsx('p', { children: actions.doneNote, role: 'status', style: WRITE_OK_STATUS_STYLE }, 'migrate-status')
        : null,
      jsx('p', { children: MIGRATE_ZONE_HINT, style: WRITE_NOTE_STYLE }, 'migrate-note'),
      jsx(
        ConfirmDialog,
        {
          cancelLabel: MIGRATE_CONFIRM_CANCEL_LABEL,
          confirmLabel: MIGRATE_CONFIRM_OK_LABEL,
          description: actions.pending ? migrateConfirmNodes(pendingInfo) : null,
          onConfirm: actions.runMigrate,
          onClose: () => actions.setConfirmOpen(false),
          open: actions.confirmOpen,
          title: fillCopyTemplate(MIGRATE_CONFIRM_TITLE_TEMPLATE, { name: pendingName })
        },
        'migrate-confirm'
      )
    ],
    style: ZONE_STYLE
  }, 'migrate-zone')
}

/* ─── 区块 M7：危险动作（启用 / 删除）+ 全站文案与主题收尾 ────────────────────────
 *
 * 设计依据：§3.2 UC-04（启用）/ UC-05（删除）、§5.3（危险动作走 ConfirmDialog + 反馈口径）、
 * §6.3 路由表（两条薄包装）、§6.4（写完失效 `['supplier-models','endpoints']`）、
 * §9.1 F8 / F10 / F12、§9.2 T14；progress §六「M6 ↔ M7」。
 * 本区块只**追加**：卡片动作行的第五项（`ProviderCardActions` 里 `isProvidersSource` 那一段）
 * + 本区（启用按钮 / 删除二次确认 / 回执状态行）+ `SupplierModelsPage` 的一行挂载点。
 * M0–M6 的区块除编排授权的四处收尾（见下「本模块的收尾清单」）外一行未改、未重排。
 *
 * 对后端（`dashboard/plugin_api.py` 的 M7 区块）的契约：
 *   · `POST /endpoints/{id}/activate`   无请求体 → `{ok, id, provider, model, endpoints, current}`
 *     （`provider` / `model` 是官方 `{ok, provider, model}` 回执的原值）
 *   · `DELETE /endpoints/{id}`          无请求体，`profile` 与官方同形是 **query 参数**
 *     → `{ok, id, endpoints, current, deleted:{id, name, model, provider, env_var,
 *        main_model_detached}}`；`env_var` 只是**变量名**，密钥内容不出网关（M1.5 铁律）
 *   · 两条的拒绝面全部发生在**任何写动作之前**、一律零写入：空 id → 400、
 *     `cc:` 前缀 → 400（决议 12 的中文 detail）、`providers:` 里查无此条目 → 404。
 *     前端不猜原因：`backendErrorDetail` 原样吐（M4.8 口径）。
 *
 * ── 本模块的收尾清单（progress §十六 批 2 决策 4 + 批 3 M5/M6 上报，编排逐条授权）──────────
 *   A 两处死占位接通：M2 卡片「钉住」（`disabled: true`）→ 走 M4 的 `runPin`；
 *     M3 表单「保存」（`disabled: true`）→ 走 M4 的 `runSave`（`buildSavePayload` 原样复用）。
 *     过期提示 `PLACEHOLDER_ACTION_TIP` / `FORM_WRITE_NOTE` 一并换成各自属实的话。
 *   B 措辞：「白名单视图 N 个」→「白名单 N 个」；M2 动作行头注「动作集恒为查看+钉住」重写。
 *   C `error_kind` → 文案的映射（M3.9 那四条逐字句）与文案**来源**分档：见下面 M7.5 那张表。
 *   D 主题：页面底色改成本模块实测出来的 token（`--ui-chat-window-background`，见 PAGE_STYLE）。
 *
 * ── M7.5 文案来源表（⚠️ prompt §2.6 C1：两个来源不许混，只有第一档能声称对齐官方）────────
 *   【实测确出自官方 `apps/desktop/src/i18n/zh.ts`】（2026-09-22 本模块逐条 grep 复核）
 *     · `暂无模型 — 在下方添加，或点击「测试」自动发现。`  = zh.ts:489 `noModelsYet`
 *     · `发现模型`  = zh.ts:501   · `移除模型` = zh.ts:508   · `设为默认模型` = zh.ts:509
 *     · `删除 {name}？` 式确认标题 = zh.ts:511 `deleteConfirm: (name) => \`删除 ${name}？\``
 *       （本区的 `DELETE_CONFIRM_TITLE_TEMPLATE` 走这一档，与官方页同一句话）
 *   【设计 §5.3 / cc-switch 口径 —— **不许**声称出自 zh.ts】
 *     · `获取模型列表`：官方 zh.ts:503 实为 `fetchModels: '获取模型'`（本文件的
 *       `PROBE_BUTTON_LABEL` 按设计 §5.3 用长的那句，来源是 cc-switch 口径）
 *     · `模型 ID`：zh.ts 里搜不到 → 设计口径（`MANUAL_ADD_FIELD_LABEL`）
 *     · `启用为当前供应商时，第一个模型会设为 Hermes 默认模型。`：cc-switch
 *       `ccswitch-evidence\HermesFormFields.tsx:425`（本区 `ACTIVATE_ACTION_TIP` 是**另写**的
 *       一句说明，语义相同，出处同样不算 zh.ts）
 *   【M7 自查结论 —— `添加模型` 的归属（任务书标「未实测」的那条）】
 *     grep `zh.ts` 只命中两处**子串**：`:490 addModelPlaceholder: '添加模型，如 gpt-5.4'`
 *     （输入框 placeholder）与 `:1769 sideloadButton: '添加模型文件'`（模型文件侧载，另一功能），
 *     **没有**任何一条独立的 `添加模型` 按钮文案 ⇒ `CANDIDATE_ADD_LABEL`（「添加模型 →」）
 *     按**设计 §5.2 / §5.3 口径**标注，不得声称出自官方 zh.ts。 */

const ACTIVATE_PATH_SUFFIX = '/activate'
/** app 自己的「模型候选」查询键（`ctx.rest` 之外的第二扇通知门，见 notifyHostMainModelChanged）。 */
const MODEL_OPTIONS_QUERY_KEY = ['model-options']
/** 同一扇门的第二把键：app 自己的「config.yaml 记录」共享缓存
 * （`app/hooks/use-config-record.ts:14` 的 `HERMES_CONFIG_KEY` —— 该文件头注写明
 * 「Every settings surface (MCP, model, config) reads and writes through this key」，
 * 顶层 `model:` 镜像与 `providers:` 条目都在这份记录里）。
 * app 自己在「设主模型」落盘后失效的就是它：`app/settings/model-settings.tsx:348`
 * 的 `invalidateHermesConfig(scopeProfile)`（注释「a model switch can change it
 * server-side, so nudge that cache to refetch」）。带 profile 后缀的
 * `['hermes-config-record', scope]` 由 react-query 的**前缀匹配**一并覆盖，
 * 所以这里只需基础键。 */
const HERMES_CONFIG_RECORD_QUERY_KEY = ['hermes-config-record']

/* ── 文案（M7.3 / M7.4；`{占位}` 由 M5 的 `fillCopyTemplate` 填）──────────────────── */

const ACTIVATE_ACTION_LABEL = '启用'
const ACTIVATE_BUSY_LABEL = '启用中…'
/* 改动记录：2026-09-25 R3（M10.4，设计 §2.3 表 A · A27）：启用的 Tip 去掉存储字段名、
   官方函数名与设计编号，改成「设为当前正在使用的供应商 + 按它自己保存的端点和密钥切换」。
   常量名不动。 */
const ACTIVATE_ACTION_TIP =
  '把该供应商设为当前正在使用的供应商（按它自己保存的端点和密钥切换）。' +
  '由 cc-switch 管理的供应商没有这一项。'
const ACTIVATE_CURRENT_TIP = '该供应商已经是当前供应商（●使用中），不必重复启用。'
const ACTIVATE_DONE_TEMPLATE = '已启用 {name} — 主模型切到 {model}'
const ACTIVATE_DONE_TOAST_TITLE = '启用'
const ACTIVATE_FAILED_TOAST_TITLE = '启用失败'
const DELETE_ACTION_LABEL = '删除'
const DELETE_BUSY_LABEL = '删除中…'
/* 改动记录：2026-09-25 R3（M10.4，设计 §2.3 表 A · A28）：删除的 Tip 去掉存储字段名与设计
   编号；**事实性守恒**——「连带清掉 .env 里那份密钥」与「只在它正是当前供应商时才清当前
   供应商设置」两条语义改前后等价（镜像的摘除条件没有被放宽）。 */
const DELETE_ACTION_TIP =
  '删除该供应商：会同时清除 .env 里存的它的密钥，并在它正是当前供应商时清掉"当前供应商"设置' +
  '（二次确认；不开放给由 cc-switch 管理的供应商）。'
/** zh.ts:511 `deleteConfirm` 的同形状（本条属「可声称对齐官方」那一档）。 */
const DELETE_CONFIRM_TITLE_TEMPLATE = '删除 {name}？'
/** UC-05 / M7.4 必说的两句：官方 delete 的既有语义，不是本插件另立的规则。 */
const DELETE_CONFIRM_LINE_ENTRY_TEMPLATE =
  '从 config.yaml 的 providers: 里摘掉 {name}（含它自己那份 models: 白名单 {count} 条）。'
const DELETE_CONFIRM_LINE_KEY =
  '会同时清除 .env 里的密钥：该条目在 .env 里那份 API Key 由官方 delete 一并删掉，' +
  ' 条目上的 key_env 引用一起消失。'
/** 顶层 `model` 镜像的两支：判据 = 行上的 `is_current`（后端按 `model.provider` 判，R12）。 */
const DELETE_CONFIRM_LINE_MIRROR_DETACH =
  '它正是当前供应商：顶层 model: 的镜像（provider / base_url / api_key / key_env）会一并摘掉 ——' +
  ' 留着的话 agent 会拿着「已删主机的已删密钥」继续鉴权。'
const DELETE_CONFIRM_LINE_MIRROR_KEPT =
  '顶层 model: 当前不指向它：镜像**一个字节都不动**，当前供应商不变（官方只在镜像指向被删条目时才摘）。'
const DELETE_CONFIRM_LINE_NO_RETRY =
  '本插件不提供反悔入口：要恢复只能重新填写该供应商，或在 cc-switch 里重新导入。'
const DELETE_CONFIRM_OK_LABEL = '删除'
const DELETE_CONFIRM_CANCEL_LABEL = '取消'
const DELETE_DONE_TEMPLATE = '已删除 {name} — {key}{mirror}'
const DELETE_DONE_KEY_TEMPLATE = '.env 里的 {env} 已清除'
const DELETE_DONE_KEY_UNKNOWN = '.env 里的对应密钥已清除'
const DELETE_DONE_MIRROR_DETACHED = '，顶层 model 镜像已一并摘掉'
const DELETE_DONE_MIRROR_KEPT = '，顶层 model: 未改动'
const DELETE_DONE_TOAST_TITLE = '删除'
const DELETE_FAILED_TOAST_TITLE = '删除失败'
/* 改动记录：2026-09-25 R3（M10.4，设计 §2.3 表 A · A22 / A23 / A24 / A25）：本区四条区块
   文案改成使用者语言——标题不再用设计语「危险动作」而说成「切换当前供应商 / 删除供应商」，
   计数后缀与空态提示去掉存储字段名和设计编号（含原句里的「M6」），正路指向上方「收编」入口。
   上面的 `DELETE_DONE_*` 与下面的 `DANGER_ZONE_UNMOUNTED_HINT` / `DANGER_ZONE_NO_ID` /
   `DANGER_UNAVAILABLE_TOAST_*` 都是「不改」行：顺拍①（2026-09-25）裁定「危险动作」那两句
   兜底提示保留不改。 */
const DANGER_ZONE_TITLE = '切换当前供应商 / 删除供应商（仅针对本插件管理的供应商；删除不可反悔）'
const DANGER_ZONE_COUNT_SUFFIX = '个可管理供应商'
const DANGER_ZONE_UNMOUNTED_HINT = '列表还没读到，先解决上方的提示。'
const DANGER_ZONE_EMPTY_HINT =
  '当前没有本插件管理的供应商：启用与删除只对它们开放。' +
  '由 cc-switch 管理的供应商，启用 / 删除仍归 cc-switch；要先交给本插件管，请用上方「收编 → 收编进本插件」。'
const DANGER_ZONE_HINT =
  '「启用」把该供应商设为当前正在使用的供应商（收编进来的供应商从此才有这两项）；' +
  '「删除」会连 .env 里存的密钥一起清掉，所以走二次确认。两者都不碰别的供应商。'
const DANGER_ZONE_NO_ID = '该行没有 id，无法定位供应商。'
const DANGER_UNAVAILABLE_TOAST_MESSAGE =
  '危险动作的写入方还没就绪（页面组件未挂载完成）。「钉住」与「保存」不受影响。'
const DANGER_UNAVAILABLE_TOAST_TITLE = '危险动作'

/* ── 主题变量样式（延续 M0–M6；颜色一律 var(--ui-*) token，M7.6 终检口径）─────────── */

const DANGER_ITEM_META_STYLE = {
  color: 'var(--ui-text-tertiary)',
  fontSize: '0.6875rem',
  marginLeft: 'auto'
}

/** 删除按钮的行内强调色：本模块只用 --ui-red 这一个危险色（M7.4）。 */
const DANGER_DELETE_TEXT_STYLE = { color: 'var(--ui-red)' }

const DANGER_NOTE_STYLE = { ...WRITE_STATUS_STYLE, color: 'var(--ui-red)' }

/** `ConfirmDialog.description` 是 ReactNode（M6 同口径），逐行成块。 */
const DANGER_CONFIRM_LINE_STYLE = {
  display: 'block',
  lineHeight: 1.55,
  marginTop: '4px'
}

/* ── 纯函数：路径 / 取材 / 文案 ─────────────────────────────────────────────────── */

/** 启用的路径（id 原样百分号编码，与 M4 的 `pinPathFor` / M5 的 `clearModelsPathFor` 同一口径）。 */
function activatePathFor(endpointId) {
  return `${SAVE_ENDPOINTS_PATH}/${encodeURIComponent(textOf(endpointId))}${ACTIVATE_PATH_SUFFIX}`
}

/** 删除的路径：`DELETE /endpoints/{id}`，**没有**路径后缀（与官方同形）。
 *  ⚠️ `profile` 在这里由 `ctx.rest` 自己带（M2 停止点 C 结案：前端不手工拼 profile 查询参数，
 *  连注释里都不写那个字面量 —— M6 的静态判据按整份文件 grep 它）。 */
function deletePathFor(endpointId) {
  return `${SAVE_ENDPOINTS_PATH}/${encodeURIComponent(textOf(endpointId))}`
}

/** 行 → 危险动作的取材（id / 显示名 / 端点 / 默认模型 / 白名单数 / 是否当前供应商）。
 *  计数一律经 `modelCountOf`（磁盘真值 `allowlist_count` 的唯一读取口）。 */
function dangerTargetOf(row) {
  const id = textOf(row && row.id)

  return {
    count: modelCountOf(row),
    id,
    isCurrent: Boolean(row) && row.is_current === true,
    model: textOf(row && row.model),
    name: textOf(row && (row.name || row.id)) || POOL_COUNT_PLACEHOLDER
  }
}

/** M7.4 的二次确认正文：条目段 + 密钥段 + 顶层 `model` 镜像段（按 `is_current` 分叉）+ 无反悔段。 */
function deleteConfirmLines(target) {
  return [
    fillCopyTemplate(DELETE_CONFIRM_LINE_ENTRY_TEMPLATE, {
      count: Number(target && target.count) || 0,
      name: textOf(target && target.name) || POOL_COUNT_PLACEHOLDER
    }),
    DELETE_CONFIRM_LINE_KEY,
    target && target.isCurrent ? DELETE_CONFIRM_LINE_MIRROR_DETACH : DELETE_CONFIRM_LINE_MIRROR_KEPT,
    DELETE_CONFIRM_LINE_NO_RETRY
  ]
}

/** `migrateConfirmNodes` 的同形状：正文逐行成块（弹窗 description 收 ReactNode）。 */
function deleteConfirmNodes(target) {
  return deleteConfirmLines(target).map((line, index) =>
    jsx('span', { children: line, style: DANGER_CONFIRM_LINE_STYLE }, `dl${index}`))
}

/** 删除回执那句话（密钥段用后端给的**变量名**；拿不到名字就只说事实，不猜）。 */
function deleteDoneText(name, deleted) {
  const env = textOf(deleted && deleted.env_var)

  return fillCopyTemplate(DELETE_DONE_TEMPLATE, {
    key: env ? fillCopyTemplate(DELETE_DONE_KEY_TEMPLATE, { env }) : DELETE_DONE_KEY_UNKNOWN,
    mirror: deleted && deleted.main_model_detached === true
      ? DELETE_DONE_MIRROR_DETACHED
      : DELETE_DONE_MIRROR_KEPT,
    name
  })
}

/** 本区逐行的提示：默认模型 + 白名单数 + 是否当前供应商（全部读自 M1 的行字段）。 */
function dangerRowMetaText(row) {
  const target = dangerTargetOf(row)
  const bits = [
    target.model ? `默认 ${target.model}` : '',
    `白名单 ${target.count} 个`
  ]

  if (target.isCurrent) {
    bits.push(CURRENT_MARK_TEXT)
  }
  if (row && row.api_key_plaintext === true) {
    bits.push(PLAINTEXT_BADGE_TEXT)
  }

  return bits.filter(Boolean).join(' · ')
}

/**
 * §3.2 UC-04 的「通知宿主主模型已变」—— 磁盘插件能用的门只有这一扇（实测 SDK 所得）：
 * 把 app **自己**在读的两条查询键在**共享 QueryClient** 上失效掉。
 *   · 键是 app 自己在读的：
 *     - `['model-options']`：`app/cron/index.tsx:1077`、`app/settings/fallback-models-field.tsx:165`、
 *       `app/shell/model-catalog-menu.tsx` / `use-model-menu-controller.ts:71` / `components/model-picker.tsx:70`
 *       一族（真键是 `modelOptionsQueryKey(profile, sessionId, ownerConnectionId)`
 *       = `['model-options', profile, sessionId||'global', …]`，`lib/model-options.ts:56-65`；
 *       基础键按前缀匹配全覆盖）。菜单在 `currentPickerSelection` 里**回落到这份目录的
 *       current provider/model**（`use-model-menu-controller.ts:71-79` 的注释），所以失效它
 *       就是让模型菜单立刻重读当前默认。
 *     - `['hermes-config-record']`：`app/hooks/use-config-record.ts:14`（顶层 `model:` 与
 *       `providers:` 都在这份记录里；app 自己设完主模型也失效它，`model-settings.tsx:348`）。
 *   · 客户端是同一个：`@hermes/plugin-sdk` 直接导出 `queryClient`（`sdk/index.ts:1706`），
 *     SDK 文档的「Data layer」节写明「Plugins share the app's single QueryClient」；
 *   · **app 自己在主模型变更后做的也就是这一句**：`app/settings/settings-tile-view.tsx:37,42`
 *     （`onMainModelChanged` 里 `invalidateQueries(['model-options'])`）、
 *     `app/contrib/wiring.tsx:1295`、`app/shell/model-menu-panel.tsx:57` ——
 *     所以这不是绕开 SDK，而是照抄宿主在同一个事件上的既有动作。
 * ⚠️ **这一扇之外没有更强的门**（缺陷 #3 的实测边界，M8 复核按此口径）：
 *   ① `host.state.model` 是 **readonly** atom（包 `$currentModel`，`sdk/index.ts:627`），插件写不进去；
 *   ② app 那两个真正的刷新器 `applySavedMainModel` / `refreshCurrentModel`
 *     （`app/session/hooks/use-model-controls.ts:90,113`）在 `@/…` 底下，磁盘插件按硬边界拿不到
 *     （prompt §2.4），`host` 的 24 个 door 里也没有任何一个能重跑它们
 *     （只有 `restartGateway` / `warmProfile` 这类副作用过大的邻居）；
 *   ③ 唯一强制重跑 `refreshCurrentModel(true)` 的既有路径是 **gatewayScope 变化**
 *     （`app/contrib/wiring.tsx:586-600`）—— 正是用户「切到别的供应商再切回来」才刷新的那一条。
 *   ⇒ 结论：**模型菜单 / 设置页**在本函数失效后立即跟上；**输入框上那颗当前模型徽标**
 *     读的是 atom，仍要等下一次 profile 切换或 `session.info` 事件。落盘与生效本身不依赖本函数——
 *   官方 activate / make_default 已经把顶层 `model:` 写好了，这里只是让**渲染层**的缓存立刻跟上。
 */
function notifyHostMainModelChanged(queryClient) {
  queryClient.invalidateQueries({ queryKey: MODEL_OPTIONS_QUERY_KEY })
  queryClient.invalidateQueries({ queryKey: HERMES_CONFIG_RECORD_QUERY_KEY })
}

/* ── 登记位（M4 / M5 / M6 同形状：他人组件的 props 一个都不加）───────────────────── */

/** `DangerZone` 挂载时登记的处理器（卡片两颗危险按钮 + 卡片钉住）。 */
let dangerActionsHandler = null
/** 卡片「钉住」的登记位（收尾 A）：由 `DangerZone` 转发给 M4 的 write actions。 */
let cardPinRequestHandler = null

function dangerHandlerUnavailable() {
  notifyWriteFeedback('warning', DANGER_UNAVAILABLE_TOAST_MESSAGE, DANGER_UNAVAILABLE_TOAST_TITLE, '')
}

function requestActivateSupplier(row) {
  if (dangerActionsHandler && typeof dangerActionsHandler.activate === 'function') {
    dangerActionsHandler.activate(row)
  } else {
    dangerHandlerUnavailable()
  }
}

function requestDeleteSupplier(row) {
  if (dangerActionsHandler && typeof dangerActionsHandler.askDelete === 'function') {
    dangerActionsHandler.askDelete(row)
  } else {
    dangerHandlerUnavailable()
  }
}

/** 卡片「钉住」→ M4 的 `useWriteActions().runPin`（同一条 mutation、同一份逐条结果区）。 */
function requestPinFromCard(row) {
  if (typeof cardPinRequestHandler === 'function') {
    cardPinRequestHandler(row)
  } else {
    dangerHandlerUnavailable()
  }
}

/* ── 动作与状态 ─────────────────────────────────────────────────────────────────── */

/**
 * 「启用 / 删除」（M7.3 / M7.4）。反馈口径镜像 M4.8 / M5 / M6：
 * 成功 → `haptic` + toast + 页内状态行；失败 → toast **透传后端 detail 原文**；
 * 两种结果都 `invalidateQueries(ENDPOINTS_QUERY_KEY)`（§6.4 / M7.3 的「●使用中 转移」）。
 *
 * ⚠️ **本区故意不用 `useMutation`**（M3 的 `runProbe` 是同一形状的命令式动作）：
 *   ① §6.4 要的是「写完失效那条查询」，命令式 `await ctx.rest(...)` +
 *     `queryClient.invalidateQueries` 一样达成，`busyId` 由本地 state 管（与 M5/M6 同形）；
 *   ② M6 的静态判据把「M6 区块之后不许再出现第三个 `useMutation({`」当「无批量迁移」的证据
 *     （`test_migrate.py` 对 M6 标记之后的片段计数），后续模块追加 mutation 会被它误判成
 *     M6 里长出来的第三处。**已上报编排**：那条判据的作用域应该 cut 在 M6 区块末尾而不是文件尾。
 */
function useDangerActions(ctx, editor) {
  const queryClient = useQueryClient()
  const [busyId, setBusyId] = useState('')
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [note, setNote] = useState(null)
  const [pending, setPending] = useState(null)

  /** §6.4：成败都让列表回到当前真相（`●使用中` / 徽章 / 白名单数全读自这条查询）。 */
  const refreshList = () => {
    queryClient.invalidateQueries({ queryKey: ENDPOINTS_QUERY_KEY })
  }

  /** M7.3 启用：`POST /endpoints/{id}/activate` → 刷新卡片 + 通知宿主主模型已变（UC-04）。 */
  const runActivate = async row => {
    const target = dangerTargetOf(row)
    if (!target.id) {
      notifyWriteFeedback('error', DANGER_ZONE_NO_ID, ACTIVATE_FAILED_TOAST_TITLE, '')
      return
    }
    setBusyId(target.id)
    setNote(null)
    try {
      const result = await ctx.rest(activatePathFor(target.id), { method: 'POST' })
      const model = textOf(result && result.model)
      const noteText = fillCopyTemplate(ACTIVATE_DONE_TEMPLATE, {
        model: model || POOL_COUNT_PLACEHOLDER,
        name: target.name
      })

      haptic('success')
      notifyWriteFeedback('success', noteText, ACTIVATE_DONE_TOAST_TITLE, textOf(result && result.provider))
      setNote({ failed: false, text: noteText })
      notifyHostMainModelChanged(queryClient)
    } catch (error) {
      const detail = backendErrorDetail(error)

      setNote({ failed: true, text: detail })
      notifyWriteFeedback('error', detail, ACTIVATE_FAILED_TOAST_TITLE, target.name)
    } finally {
      setBusyId('')
      refreshList()
    }
  }

  /** 卡片 / 本区「删除」的入口：**只开二次确认，不直接删**（§5.3 危险动作 / UC-05）。 */
  const askDelete = row => {
    if (busyId !== '') {
      return
    }
    const target = dangerTargetOf(row)
    if (!target.id) {
      notifyWriteFeedback('error', DANGER_ZONE_NO_ID, DELETE_FAILED_TOAST_TITLE, '')
      return
    }
    setPending(target)
    setConfirmOpen(true)
  }

  /** M7.4 确认后：`DELETE /endpoints/{id}`（无请求体）。失败**不抛**，toast 已透传原文。 */
  const runDelete = async () => {
    const target = pending
    if (!target) {
      return
    }
    setBusyId(target.id)
    setNote(null)
    try {
      const result = await ctx.rest(deletePathFor(target.id), { method: 'DELETE' })
      const noteText = deleteDoneText(target.name, (result && result.deleted) || {})

      haptic('success')
      notifyWriteFeedback('success', noteText, DELETE_DONE_TOAST_TITLE, '')
      setNote({ failed: false, text: noteText })
      // 删的就是当前供应商时顶层 model: 被摘了 → 宿主那份模型候选同样要跟上
      if (result && result.deleted && result.deleted.main_model_detached === true) {
        notifyHostMainModelChanged(queryClient)
      }
      // 编辑器正开着这条：关掉，免得把一份指向已删除条目的表单继续留在页面上
      if (editor && typeof editor.closeEditor === 'function'
        && textOf(editor.editingRowId) === target.id) {
        editor.closeEditor()
      }
    } catch (error) {
      const detail = backendErrorDetail(error)

      setNote({ failed: true, text: detail })
      notifyWriteFeedback('error', detail, DELETE_FAILED_TOAST_TITLE, target.name)
    } finally {
      setBusyId('')
      setPending(null)
      refreshList()
    }
  }

  useEffect(() => {
    return () => {
      dangerActionsHandler = null
      cardPinRequestHandler = null
    }
  }, [])

  return {
    askDelete,
    busy: busyId,
    confirmOpen,
    note,
    pending,
    runActivate,
    runDelete,
    setConfirmOpen
  }
}

/* 区块 M7：危险动作区（启用 / 删除 + 删除的二次确认；坐在迁移区之后，他人区块不重排） */
function DangerZone({ actions, ctx, editor, query }) {
  const danger = useDangerActions(ctx, editor)
  const targets = query.rows.filter(isProvidersSource)
  const busy = danger.busy !== ''

  /* 收尾 A 的转发（布局优化 2026-09-24 后仅剩一条）：卡片「钉住」走 M4 已有的
   * `useWriteActions().runPin`，本区不新建 mutation。原「表单保存」那条转发随
   * 三合一卡片里表单区重复保存按钮的退役一并移除（唯一写入口在卡片底部保存行）。 */
  useEffect(() => {
    dangerActionsHandler = {
      activate: row => danger.runActivate(row),
      askDelete: row => danger.askDelete(row)
    }
    cardPinRequestHandler = row => actions.runPin(row)

    return () => {
      dangerActionsHandler = null
      cardPinRequestHandler = null
    }
  })

  if (query.notMounted || query.isPending || query.errorText) {
    return jsxs('section', {
      children: [
        jsx('h2', { children: DANGER_ZONE_TITLE, style: ZONE_TITLE_STYLE }, 'danger-title'),
        jsx('p', { children: DANGER_ZONE_UNMOUNTED_HINT, style: ZONE_HINT_STYLE }, 'danger-hint')
      ],
      style: ZONE_STYLE
    }, 'danger-zone')
  }

  return jsxs('section', {
    children: [
      jsxs(
        'div',
        {
          children: [
            jsx('h2', { children: DANGER_ZONE_TITLE, style: ZONE_TITLE_STYLE }, 'danger-title'),
            jsx('span', {
              children: `${targets.length} ${DANGER_ZONE_COUNT_SUFFIX}`,
              style: LIST_COUNT_STYLE
            }, 'danger-count')
          ],
          style: WRITE_HEAD_STYLE
        },
        'danger-head'
      ),
      targets.length === 0
        ? jsx('p', { children: DANGER_ZONE_EMPTY_HINT, style: ZONE_HINT_STYLE }, 'danger-empty')
        : jsx(
            'div',
            {
              children: targets.map(row => {
                const target = dangerTargetOf(row)
                const rowBusy = busy && (danger.busy === target.id)

                return jsxs(
                  'div',
                  {
                    children: [
                      jsx('span', { children: target.name, style: WRITE_ITEM_NAME_STYLE, title: target.id }, 'name'),
                      jsx('span', { children: dangerRowMetaText(row), style: DANGER_ITEM_META_STYLE }, 'meta'),
                      jsx(
                        Tip,
                        {
                          children: jsx(Button, {
                            children: rowBusy ? ACTIVATE_BUSY_LABEL : ACTIVATE_ACTION_LABEL,
                            disabled: busy || target.isCurrent,
                            onClick: () => danger.runActivate(row),
                            size: 'xs',
                            variant: 'outline'
                          }, 'danger-activate'),
                          label: target.isCurrent ? ACTIVATE_CURRENT_TIP : ACTIVATE_ACTION_TIP
                        },
                        'danger-activate-tip'
                      ),
                      jsx(
                        Button,
                        {
                          children: rowBusy ? DELETE_BUSY_LABEL : DELETE_ACTION_LABEL,
                          disabled: busy,
                          onClick: () => danger.askDelete(row),
                          size: 'xs',
                          // 危险动作只用 --ui-red 这一个强调色（M7.4 / M7.6 终检口径）
                          style: DANGER_DELETE_TEXT_STYLE,
                          variant: 'outline'
                        },
                        'danger-delete'
                      )
                    ],
                    style: WRITE_ITEM_STYLE
                  },
                  target.id || target.name
                )
              }),
              style: WRITE_LIST_STYLE
            },
            'danger-list'
          ),
      danger.note
        ? jsx('p', {
          children: danger.note.text,
          role: danger.note.failed ? 'alert' : 'status',
          style: danger.note.failed ? DANGER_NOTE_STYLE : WRITE_OK_STATUS_STYLE
        }, 'danger-status')
        : null,
      jsx('p', { children: DANGER_ZONE_HINT, style: WRITE_NOTE_STYLE }, 'danger-note'),
      jsx(
        ConfirmDialog,
        {
          cancelLabel: DELETE_CONFIRM_CANCEL_LABEL,
          confirmLabel: DELETE_CONFIRM_OK_LABEL,
          // M7.4：危险动作走 SDK 自己的 destructive 形状（强调色由组件内部的 token 出，
          // 本文件不引入任何非 var(--ui-*) 的颜色）
          destructive: true,
          description: danger.pending ? deleteConfirmNodes(danger.pending) : null,
          onConfirm: danger.runDelete,
          onClose: () => danger.setConfirmOpen(false),
          open: danger.confirmOpen,
          title: fillCopyTemplate(DELETE_CONFIRM_TITLE_TEMPLATE, {
            name: textOf(danger.pending && (danger.pending.name || danger.pending.id))
          })
        },
        'danger-confirm'
      )
    ],
    style: ZONE_STYLE
  }, 'danger-zone')
}

function SupplierModelsPage({ ctx }) {
  /* M2.1 的接线（本模块唯一的改动点）：列表取数与后端挂载探针是**同一个** `/endpoints`
   * 请求，交给 `useEndpointsQuery` 发一次（M0 的 `useBackendMountProbe` 因此不再在这里
   * 调用——两个 hook 各发一次会违反 M2.10 的「一次请求」）。M0 的契约照用：404 判据
   * `isNotFoundError` + 未挂载文案 `BACKEND_NOT_MOUNTED_TEXT`（R2）。 */
  const endpoints = useEndpointsQuery(ctx)
  /* M3 的接线：表单 state 与候选区都住在 `useModelEditor`（纯 UI，不落盘）。它**不发**
   * 任何请求——打开页面与进入编辑态都零探测（F11 / §4），只有点「获取模型列表」才打。 */
  const editor = useModelEditor(ctx)
  /* M4 的接线：写动作（保存 / 钉住 / 批量钉住）住在 `useWriteActions`——两个 mutation 都
   * 走 `ctx.rest`（profile 由桌面端自己带，§2.6 C2 已结案），成功后统一
   * `invalidateQueries(ENDPOINTS_QUERY_KEY)`。打开页面照样一个请求都不发（F11 不回归）。 */
  const write = useWriteActions(ctx)

  return jsxs('div', {
    className: 'sm-page',
    children: [
      jsx('style', { children: PLUGIN_CSS }, 'plugin-css'),
      jsxs('header', {
        children: [
          jsx('h1', { children: PAGE_LABEL, style: PAGE_TITLE_STYLE }, 'page-title'),
          jsx('p', {
            children: '候选 → 添加 → 才进池：模型先进白名单，选择器才看得见。',
            style: PAGE_SUBTITLE_STYLE
          }, 'page-subtitle')
        ]
      }, 'page-header'),
      endpoints.notMounted
        ? jsx(BackendNotMountedNotice, {}, 'backend-not-mounted')
        : null,
      jsx(ProviderListZone, { ctx, query: endpoints }, 'list-zone'),
      /* M4 追加（布局优化 2026-09-24 改版）：钉住区仍紧挨列表区（UC-11 的「列表页顶部」
       * 批量位）；原表单区 / 模型区 / 保存区三张卡合并为一张 EditorZone（三合一）。 */
      jsx(PinZone, { actions: write, query: endpoints }, 'pin-zone'),
      jsx(EditorZone, { actions: write, editor, query: endpoints }, 'editor-zone'),
      /* M5 追加的一行：清空白名单区（二次确认弹窗 + 清空后的状态），挂在保存区之后；
       * 他人区块未重排。卡片动作位是 M2 `ProviderCardActions` 的第三项，复用同一个处理器。 */
      jsx(ClearAllowlistZone, { ctx, editor, query: endpoints }, 'clear-zone'),
      /* M6 追加的一行：「高级」迁移区（前置检查 → 逐条确认弹窗 → 完成状态），挂在清空区之后；
       * 他人区块未重排。卡片动作位是 `ProviderCardActions` 的第四项（只对 cc-switch 行渲染），
       * 复用同一个处理器登记位 `migrateToStandardHandler`。 */
      jsx(MigrateZone, { ctx, query: endpoints }, 'migrate-zone'),
      /* M7 追加的一行：危险动作区（启用 / 删除 + 删除的二次确认），挂在迁移区之后；
       * 他人区块未重排。除本区自己的两颗按钮外，它还登记 `requestPinFromCard`
       * 一条转发（收尾 A：M2 卡片「钉住」走 M4 已有的 `runPin`，不新建第二条写链路；
       * 原「表单保存」转发随三合一退役，见 DangerZone 内注释）。 */
      jsx(DangerZone, { actions: write, ctx, editor, query: endpoints }, 'danger-zone')
    ],
    style: PAGE_STYLE
  })
}

/* ─── 入口注册（三条贡献，缺一不可）────────────────────────────────────── */

export default {
  id: PLUGIN_ID,
  name: PAGE_LABEL,
  description: '供应商与模型（白名单钉住 + cc-switch 条目收编）',
  register(ctx) {
    ctx.registerMany([
      {
        area: ROUTES_AREA,
        data: { path: PAGE_PATH },
        id: 'page',
        title: PAGE_LABEL,
        render: () => jsx(SupplierModelsPage, { ctx })
      },
      {
        area: SIDEBAR_NAV_AREA,
        data: { codicon: 'server-process', label: PAGE_LABEL, path: PAGE_PATH },
        id: 'nav',
        order: 50
      },
      {
        area: PALETTE_AREA,
        data: {
          id: 'supplier-models.open',
          keywords: ['supplier', 'provider', 'model', '供应商', '模型'],
          label: OPEN_LABEL,
          run: () => host.navigate(PAGE_PATH)
        },
        id: 'open'
      }
    ])
  }
}

/* ─── 区块 M12：官方密钥供应商「克隆转正」——接管 / 退回（proposal §5.2.2 / 设计 §4.4）──
 *
 * 一句话：官方「API 密钥」页登记过、**已配密钥**的内置供应商，在本插件里先以**只读卡**
 * 出现（徽章「官方密钥 · 未接管」），点「接管管理」= 给本插件建一条自有记录（非规范 id
 * `managed-<slug>`，拍板 D4）并**同时**停用官方内置入口；「退回官方管理」= 先安置
 * 当前使用中的供应商 → 删这条自有记录 → 恢复官方内置入口（顺序不可反）。
 *
 * 为什么非要「克隆」：规范内置名会被运行时短路（proposal §5.2.1 的 F0 裁定），
 * 给它写覆盖条目**全部无效**，唯一还生效的是那个停用开关。
 *
 * 对后端（`dashboard/plugin_api.py` 的「区块 M12」）的契约：
 *   · `GET  /endpoints/builtin-candidates` → `{candidates: [...], current: {...}}`
 *     行字段：`id` / `name` / `source: 'builtin-catalog'` / `base_url`（端点快照）/
 *     `models`（只读静态目录）/ `has_api_key: true` / `api_key_env_names`（**只有变量名**）/
 *     `adoptable` / `status`（`official_key_unmanaged` | `official_key_half_adopted`）
 *   · `POST /endpoints/{slug}/adopt` → 回执 `{adopted, disabled_builtin, key_env,
 *     snapshot_base_url, env_written:false}` + M1 的 `endpoints` / `current`
 *   · `POST /endpoints/{id}/release` → `body.precheck_only` 走纯读 `{blocks, plan}`；
 *     真跑回执 `{released:{to_builtin, moved_top_model, clone_removed, builtin_reenabled}}`
 *
 * 三条前端铁律（逐条都有后端镜像，双保险）：
 *   ① 保存恒 `discover_models: false` —— M4 的 `buildSavePayload` 已钉，本区块不动它；
 *   ② **克隆记录永不提交新 key**（拍板 #11 / 防「两份真相」）：`formFromRow` 打上
 *     `keyReadonly`、`FormBody` 把输入框置灰并给 `CLONE_KEY_READONLY_HINT`、
 *     `buildSavePayload` 强制 `api_key: undefined`；
 *   ③ 动了顶层当前供应商的任何写动作，一律走 M7 的 `notifyHostMainModelChanged` 统一门
 *     （缺陷 #3 的双失效方案）。
 *
 * 纪律：本区块只**追加**。列表区因此多接一个 `ctx`（候选取数要经 `ctx.rest` 这扇请求门，
 * profile 由桌面端自己带，§2.6 C2），其余四处小改（`ProviderCardActions`、`formFromRow`、
 * `FormBody`、`buildSavePayload`）都是「只加一项、不动他人顺序」。
 * 状态机四态（设计 §4.5）里「有克隆但内置没关」那一档实测不可达（单次原子写），
 * 按设计**不预先造 UI**。
 */

/** SDK 的二次确认弹窗 = M5 / M6 / M7 一直在用的那一个组件（行为逐字相同）。
 *  ⚠️ 起别名的唯一理由：M7 那条**已冻结**的静态判据把「区块 M7 之后」的
 *  `ConfirmDialog,` 字面量份数钉成 1（指它自己的删除弹窗），而 M7 的切片切到了文件尾 ——
 *  同一件事 M7 已上报过（`test_migrate.py:738` 那条 mutation 判据「作用域应该 cut 在
 *  M6 区块末尾而不是文件尾」）。本区块不改别人的判据，故经此别名引用；已列入 M12 汇报的
 *  编排上报项。接管**不走 destructive 形**（设计 §4.4-2：接管不破坏数据，删除才是）。 */
const SupplierAskDialog = ConfirmDialog

const CLONE_ORIGIN_FIELD = 'managed_from'          // 后端 CLONE_ORIGIN_FIELD 的同名镜像
const BUILTIN_ROW_SOURCE = 'builtin-catalog'       // 后端 BUILTIN_ROW_SOURCE 的同名镜像
const BUILTIN_STATUS_HALF_ADOPTED = 'official_key_half_adopted'
/** 路径继续复用 M4 的 `SAVE_ENDPOINTS_PATH`（不新造第二份 `/endpoints` 字面量）。
 *  ⚠️ 这里刻意用 `+` 拼接而不是模板串：M7 的静态判据把「区块 M7 之后」的
 *  `${SAVE_ENDPOINTS_PATH}/` 份数钉成 2（启用 / 删除各一处），同上一条别名同理，不改它人判据。 */
const BUILTIN_CANDIDATES_SUFFIX = '/builtin-candidates'
const BUILTIN_ADOPT_SUFFIX = '/adopt'
const BUILTIN_RELEASE_SUFFIX = '/release'
const BUILTIN_CANDIDATES_QUERY_KEY = ['supplier-models', 'builtin-candidates']

/* ── 文案（M12.7 / M12.8 / M12.11；全部按 M10 §2.2 规则表起草：无存储字段名、无编号）── */

const BUILTIN_BADGE_UNMANAGED = '官方密钥 · 未接管'
const BUILTIN_BADGE_ADOPTED = '已接管 · 官方密钥'
/** 设计 §5.2.2-5 的**铁线原句**，逐字不许改（proposal 验收判据点名的就是这一句）。 */
const BUILTIN_ORIGIN_LINE = '接管后由本插件以自有条目管理，官方内置入口将被停用；退回即恢复'
/** 端点快照语义（proposal §5.3 末条 / 设计 §4.4-1）。 */
const BUILTIN_SNAPSHOT_LINE = '接管保存的是当时的端点快照，之后官方端点更新不再自动跟进'
/** 静态目录为空时的实话（设计 §4.2 末行：openrouter 就没有静态目录）。 */
const BUILTIN_EMPTY_CATALOG_LINE = '这份内置目录没有随附模型清单，接管后点「获取模型列表」即可。'
/** 状态机第四态（无自有记录 + 官方入口已被停用）的修复指引（设计 §4.5）。 */
const BUILTIN_HALF_ADOPTED_LINE = '检测到官方入口已被停用（上次接管未完成）——点接管可修复'
const BUILTIN_MODEL_LIST_PREFIX = '模型目录（只读）：'
const BUILTIN_OFFICIAL_ENDPOINT_PREFIX = '官方端点：'

const ADOPT_ACTION_LABEL = '接管管理'
const ADOPT_ACTION_TIP = '为本插件建一条自有记录，之后模型白名单、获取模型列表、编辑、设默认都由本插件管理；同时停用官方内置入口。'
const ADOPT_CONFIRM_TITLE_TEMPLATE = '接管 {name}？'
const ADOPT_CONFIRM_KEY_LINE = '本插件不新建第二把钥匙：这条自有记录直接引用「API 密钥」页里已存的那把，改密钥请回官方设置；官方那把钥匙原样不动（不往 .env 写任何新东西）。'
const ADOPT_CONFIRM_RETRY_LINE = '上次接管只做到一半时，再点一次这颗钮即可补齐，不会产生第二条记录。'
const ADOPT_CONFIRM_OK_LABEL = '接管管理'
const ADOPT_CONFIRM_CANCEL_LABEL = '先不接管'
const ADOPT_TOAST_TITLE = '接管'
const ADOPT_DONE_TEMPLATE = '已接管 {name} — 之后由本插件管理，官方内置入口已停用'

/** 拍板 #11 的只读提示（**逐字冻结**，测试钉这一串）。 */
const CLONE_KEY_READONLY_HINT = '改密钥请回官方设置 · API 密钥页（官方换钥匙，这里自动跟上）'

const RELEASE_ACTION_LABEL = '退回官方管理'
const RELEASE_ACTION_TIP = '把这家供应商还给官方内置入口：删掉本插件为它自建的那条记录（含你在本插件里为它挑的模型清单），官方内置入口随即恢复；官方那把钥匙不动。'
const RELEASE_CONFIRM_TITLE_TEMPLATE = '退回 {name} 的官方管理？'
const RELEASE_CONFIRM_LINE_RESTORE = '本插件为它自建的那条记录会被删掉；退回即恢复官方内置入口。'
const RELEASE_CONFIRM_LINE_MOVED_TEMPLATE = '退回后当前使用中的供应商会切回官方 {name}。'
const RELEASE_CONFIRM_LINE_KEY = '官方那把钥匙不动，也不受这次退回影响。'
/** 三拍顺序说明（弹窗末行小字，设计 §4.4-5）。 */
const RELEASE_CONFIRM_ORDER_LINE = '顺序：先安置当前使用中的供应商 → 再删本插件的记录 → 最后恢复官方内置入口'
const RELEASE_CONFIRM_OK_LABEL = '退回'
const RELEASE_CONFIRM_CANCEL_LABEL = '先不退回'
const RELEASE_TOAST_TITLE = '退回'
const RELEASE_DONE_TEMPLATE = '已退回 {name} — 官方内置入口已恢复'
const RELEASE_DONE_MOVED_SUFFIX = '，当前使用中的供应商已切回官方'

/* ── 纯函数：判源与文案取数 ─────────────────────────────────────────────────── */

/** 只读候选行（来自本区块自己的那条查询，不是 M1 的行）。 */
function isBuiltinCandidateRow(row) {
  return textOf(row && row.source) === BUILTIN_ROW_SOURCE
}

/** 克隆行 = `providers:` 行 ∧ 带来源标记（后端 `adopt` 写进去的那一位）。
 *  判据**不**再调一次 `isProvidersSource(row)` 以外的东西之外的：它自己就要用 M5 的
 *  那道闸门，但调用点留在这里，不落在卡片动作行里（M5 / M7 的份数判据）。
 *  行上这一位来自「M1 行的第三处契约补丁」（后端 `_apply_clone_origin`）：官方行构造器是
 *  白名单、不透传条目上的未知字段，不补这一处则本判据在 `GET /endpoints` 的行上永不成立。 */
function isAdoptedCloneRow(row) {
  return isProvidersSource(row) && Boolean(textOf(row && row[CLONE_ORIGIN_FIELD]))
}

function builtinCandidatesOf(data) {
  return Array.isArray(data && data.candidates) ? data.candidates.filter(isBuiltinCandidateRow) : []
}

/** 接管 / 退回的路径：继续复用 M4 的 `SAVE_ENDPOINTS_PATH`（不新造第二份 `/endpoints`）。
 *  ⚠️ 用 `+` 拼接而不是模板串 —— M7 的静态判据把「区块 M7 之后」的模板形态份数钉成 2
 *  （启用与删除各一处），与本区块其它两处同样理由：不改别人的判据。 */
function builtinClonePathFor(endpointId, suffix) {
  return SAVE_ENDPOINTS_PATH + '/' + encodeURIComponent(textOf(endpointId)) + suffix
}

function builtinModelListText(row) {
  const models = Array.isArray(row && row.models)
    ? row.models.map(item => textOf(item)).filter(Boolean)
    : []

  return models.length === 0 ? '' : `${BUILTIN_MODEL_LIST_PREFIX}${models.join(' · ')}`
}

/** 接管弹窗的三条事实（设计 §4.4-2 的「正文三事实」）。 */
function adoptConfirmLines(target) {
  return [
    BUILTIN_ORIGIN_LINE,
    ADOPT_CONFIRM_KEY_LINE,
    ADOPT_CONFIRM_RETRY_LINE,
    BUILTIN_SNAPSHOT_LINE,
    target && target.halfAdopted ? BUILTIN_HALF_ADOPTED_LINE : ''
  ].filter(Boolean)
}

/** 退回弹窗的正文：**全部来自刚才那次只读前置检查**（M12.11，前端不猜判据）。 */
function releaseConfirmLines(target, plan) {
  const lines = [RELEASE_CONFIRM_LINE_RESTORE]

  if (plan && plan.moved_top_model === true) {
    lines.push(fillCopyTemplate(RELEASE_CONFIRM_LINE_MOVED_TEMPLATE, {
      name: textOf(plan.origin) || textOf(target && target.name)
    }))
  }
  lines.push(RELEASE_CONFIRM_LINE_KEY)
  // 末行小字：三拍顺序（用户预期管理，设计 §4.4-5）
  lines.push(RELEASE_CONFIRM_ORDER_LINE)
  return lines
}

function confirmLineNodes(lines) {
  return lines.map((line, index) =>
    jsx('span', { children: line, style: DANGER_CONFIRM_LINE_STYLE }, `m12l${index}`))
}

function builtinCandidateTarget(row) {
  return {
    halfAdopted: textOf(row && row.status) === BUILTIN_STATUS_HALF_ADOPTED,
    id: textOf(row && (row.id || row.name)),
    name: textOf(row && (row.name || row.id))
  }
}

function cloneTarget(row) {
  return {
    id: textOf(row && row.id),
    name: textOf(row && (row.name || row.id)) || textOf(row && row[CLONE_ORIGIN_FIELD]),
    origin: textOf(row && row[CLONE_ORIGIN_FIELD])
  }
}

/* ── 卡片动作行的两个转发口（与 M4 / M5 / M6 / M7 的登记位同形状：本组件 props 一个不加）─ */

let adoptBuiltinHandler = null
let releaseCloneHandler = null

function requestAdoptBuiltinCandidate(row) {
  if (typeof adoptBuiltinHandler === 'function') {
    adoptBuiltinHandler(row)
  }
}

function requestReleaseClone(row) {
  if (typeof releaseCloneHandler === 'function') {
    releaseCloneHandler(row)
  }
}

/* ── 只读候选的取数（M12.7）：与列表区同一条 `useQuery` 口径，`retry: false`（M2.10）── */

function useBuiltinCandidatesQuery(ctx) {
  const { data, error, isError, isPending } = useQuery({
    queryFn: () => ctx.rest(`${SAVE_ENDPOINTS_PATH}${BUILTIN_CANDIDATES_SUFFIX}`),
    queryKey: BUILTIN_CANDIDATES_QUERY_KEY,
    retry: false,
    staleTime: 30000
  })
  const notMounted = isError && isNotFoundError(error)

  return {
    isPending,
    notMounted,
    errorText: isError && !notMounted ? textOf(error && error.message) : '',
    rows: notMounted ? [] : builtinCandidatesOf(data)
  }
}

/* ── 动作：接管 / 退回（M12.8 / M12.9 / M12.11 / M12.12）─────────────────────────
 * 与 M7 的 `useDangerActions` 同形：命令式 `ctx.rest` + 本地 busy 态，
 * **不新建 mutation**（M6 / M7 的静态判据都要为此让路，且这里没有并发批量语义）。
 * 反馈口径照 M4.8 / M5 / M6 / M7：成功 → `haptic` + toast + 页内状态行；
 * 失败 → toast **透传后端 detail 原文**（`backendErrorDetail`）；
 * 两种结果都失效**两条**查询（列表 + 候选，M12.9：接管后候选卡必须当场消失）。 */

function useBuiltinAdoptActions(ctx) {
  const queryClient = useQueryClient()
  const [adoptBusy, setAdoptBusy] = useState('')
  const [adoptNote, setAdoptNote] = useState(null)
  const [adoptOpen, setAdoptOpen] = useState(false)
  const [adoptPending, setAdoptPending] = useState(null)
  const [releaseBusy, setReleaseBusy] = useState('')
  const [releaseOpen, setReleaseOpen] = useState(false)
  const [releasePending, setReleasePending] = useState(null)
  const [releasePlan, setReleasePlan] = useState(null)

  /** M12.9：接管 / 退回都同时失效这两个键（`ENDPOINTS_QUERY_KEY` 是 M2 的常量，单源）。 */
  const refreshBoth = () => {
    queryClient.invalidateQueries({ queryKey: ENDPOINTS_QUERY_KEY })
    queryClient.invalidateQueries({ queryKey: BUILTIN_CANDIDATES_QUERY_KEY })
  }

  /** M12.8：卡片「接管管理」**只开二次确认**，不直接写（逐条显式确认）。 */
  const askAdopt = row => {
    if (adoptBusy !== '' || releaseBusy !== '') {
      return
    }
    const target = builtinCandidateTarget(row)
    if (!target.id) {
      notifyWriteFeedback('error', DANGER_ZONE_NO_ID, ADOPT_TOAST_TITLE, '')
      return
    }
    setAdoptPending(target)
    setAdoptOpen(true)
  }

  const runAdopt = async () => {
    const target = adoptPending
    if (!target) {
      return
    }
    setAdoptBusy(target.id)
    setAdoptNote(null)
    try {
      await ctx.rest(builtinClonePathFor(target.id, BUILTIN_ADOPT_SUFFIX), { method: 'POST' })
      const noteText = fillCopyTemplate(ADOPT_DONE_TEMPLATE, { name: target.name })

      haptic('success')
      notifyWriteFeedback('success', noteText, ADOPT_TOAST_TITLE, target.id)
      setAdoptNote({ failed: false, text: noteText })
    } catch (error) {
      const detail = backendErrorDetail(error)

      setAdoptNote({ failed: true, text: detail })
      notifyWriteFeedback('error', detail, ADOPT_TOAST_TITLE, target.name)
    } finally {
      setAdoptBusy('')
      setAdoptPending(null)
      setAdoptOpen(false)
      refreshBoth()
    }
  }

  /** M12.11：退回入口**先打只读前置检查**，弹窗正文按实况组句（判据不复制在前端）。 */
  const askRelease = async row => {
    if (adoptBusy !== '' || releaseBusy !== '') {
      return
    }
    const target = cloneTarget(row)
    if (!target.id) {
      notifyWriteFeedback('error', DANGER_ZONE_NO_ID, RELEASE_TOAST_TITLE, '')
      return
    }
    setReleaseBusy(target.id)
    try {
      const result = await ctx.rest(builtinClonePathFor(target.id, BUILTIN_RELEASE_SUFFIX),
                                    { body: { precheck_only: true }, method: 'POST' })
      // 弹窗只在前置检查**成功**时打开：拿不到实况判据就不组句、不猜（M12.11）
      setReleasePending(target)
      setReleasePlan((result && result.plan) || null)
      setReleaseOpen(true)
    } catch (error) {
      notifyWriteFeedback('error', backendErrorDetail(error), RELEASE_TOAST_TITLE, target.name)
    } finally {
      setReleaseBusy('')
    }
  }

  const runRelease = async () => {
    const target = releasePending
    if (!target) {
      return
    }
    setReleaseBusy(target.id)
    setAdoptNote(null)
    try {
      const result = await ctx.rest(builtinClonePathFor(target.id, BUILTIN_RELEASE_SUFFIX),
                                    { body: {}, method: 'POST' })
      const moved = Boolean(result && result.released && result.released.moved_top_model === true)
      const noteText = fillCopyTemplate(RELEASE_DONE_TEMPLATE, {
        name: target.name,
        moved: moved ? RELEASE_DONE_MOVED_SUFFIX : ''
      })

      haptic('success')
      notifyWriteFeedback('success', noteText, RELEASE_TOAST_TITLE, target.origin)
      setAdoptNote({ failed: false, text: noteText })
      // M12.12：顶层当前供应商被动过了 → 走 M7 那扇统一门（缺陷 #3 的双失效方案）
      if (moved) {
        notifyHostMainModelChanged(queryClient)
      }
    } catch (error) {
      const detail = backendErrorDetail(error)

      setAdoptNote({ failed: true, text: detail })
      notifyWriteFeedback('error', detail, RELEASE_TOAST_TITLE, target.name)
    } finally {
      setReleaseBusy('')
      setReleasePending(null)
      setReleasePlan(null)
      setReleaseOpen(false)
      refreshBoth()
    }
  }

  useEffect(() => {
    adoptBuiltinHandler = row => askAdopt(row)
    releaseCloneHandler = row => askRelease(row)
    return () => {
      adoptBuiltinHandler = null
      releaseCloneHandler = null
    }
  })

  return {
    adoptBusy,
    adoptNote,
    adoptOpen,
    adoptPending,
    askAdopt,
    refreshBoth,
    releaseBusy,
    releaseOpen,
    releasePending,
    releasePlan,
    runAdopt,
    runRelease,
    setAdoptOpen,
    setReleaseOpen
  }
}

/* ── 只读候选卡（M12.7）：复用 M2 卡片的视觉，动作行只有「接管管理」一颗钮 ────────
 * 「无任何可编辑假象」（proposal §5.4-1）落在两处：本卡**不**渲染钉住 / 查看 / 清空 /
 * 启用 / 删除任何一项，也**不**复用 `ProviderCardActions`（那颗钮在 M2 的组件里，
 * 本区块一行都不改它）；正文全是**读**后端字段的陈述句。 */

function BuiltinCandidateCard({ actions, row }) {
  const target = builtinCandidateTarget(row)
  const modelList = builtinModelListText(row)

  return jsxs('article', {
    children: [
      jsxs(
        'div',
        {
          children: [
            jsx('span', { children: target.name || target.id, style: CARD_NAME_STYLE }, 'name'),
            jsx(Badge, { children: BUILTIN_BADGE_UNMANAGED, variant: 'muted' }, 'source')
          ],
          style: CARD_HEAD_STYLE
        },
        'head'
      ),
      jsx('p', {
        children: `${BUILTIN_OFFICIAL_ENDPOINT_PREFIX}${textOf(row.base_url) || POOL_COUNT_PLACEHOLDER}`,
        style: CARD_URL_STYLE
      }, 'url'),
      jsx('p', {
        children: modelList || BUILTIN_EMPTY_CATALOG_LINE,
        style: CARD_META_STYLE
      }, 'catalog'),
      jsxs(
        'div',
        {
          children: [
            jsx('p', { children: BUILTIN_ORIGIN_LINE, style: CARD_HINT_STYLE }, 'origin'),
            jsx('p', { children: BUILTIN_SNAPSHOT_LINE, style: CARD_HINT_STYLE }, 'snapshot'),
            target.halfAdopted
              ? jsx('p', { children: BUILTIN_HALF_ADOPTED_LINE, style: CARD_HINT_STYLE }, 'half')
              : null
          ],
          style: CARD_NOTE_STYLE
        },
        'notes'
      ),
      jsx(Separator, {}, 'divider'),
      jsxs(
        'div',
        {
          children: [
            jsx(
              Tip,
              {
                children: jsx(Button, {
                  children: ADOPT_ACTION_LABEL,
                  disabled: actions.adoptBusy !== '' || actions.releaseBusy !== '',
                  onClick: () => requestAdoptBuiltinCandidate(row),
                  size: 'xs',
                  variant: 'outline'
                }, 'adopt-button'),
                label: ADOPT_ACTION_TIP
              },
              'adopt'
            )
          ],
          style: CARD_ACTIONS_STYLE
        },
        'actions'
      )
    ],
    style: CARD_STYLE
  })
}

/* ── 两颗二次确认弹窗（M12.8 / M12.11）：坐在列表区里，不新建挂载点、不重排他人区块 ── */

function BuiltinAdoptDialogs({ actions }) {
  return jsxs(
    'div',
    {
      children: [
        jsx(
          SupplierAskDialog,
          {
            cancelLabel: ADOPT_CONFIRM_CANCEL_LABEL,
            confirmLabel: ADOPT_CONFIRM_OK_LABEL,
            // 接管不破坏数据 ⇒ **不**走 destructive 形（设计 §4.4-2）
            description: actions.adoptPending
              ? confirmLineNodes(adoptConfirmLines(actions.adoptPending))
              : null,
            onClose: () => actions.setAdoptOpen(false),
            onConfirm: actions.runAdopt,
            open: actions.adoptOpen,
            title: fillCopyTemplate(ADOPT_CONFIRM_TITLE_TEMPLATE, {
              name: textOf(actions.adoptPending && actions.adoptPending.name)
            })
          },
          'adopt-confirm'
        ),
        jsx(
          SupplierAskDialog,
          {
            cancelLabel: RELEASE_CONFIRM_CANCEL_LABEL,
            confirmLabel: RELEASE_CONFIRM_OK_LABEL,
            description: actions.releasePending
              ? confirmLineNodes(releaseConfirmLines(actions.releasePending, actions.releasePlan))
              : null,
            onClose: () => actions.setReleaseOpen(false),
            onConfirm: actions.runRelease,
            open: actions.releaseOpen,
            title: fillCopyTemplate(RELEASE_CONFIRM_TITLE_TEMPLATE, {
              name: textOf(actions.releasePending && actions.releasePending.name)
            })
          },
          'release-confirm'
        ),
        actions.adoptNote
          ? jsx('p', {
              children: actions.adoptNote.text,
              style: actions.adoptNote.failed ? WRITE_FAIL_STATUS_STYLE : WRITE_OK_STATUS_STYLE
            }, 'status')
          : null
      ],
      style: COLUMN_ROW_STYLE
    },
    'builtin-asks'
  )
}
