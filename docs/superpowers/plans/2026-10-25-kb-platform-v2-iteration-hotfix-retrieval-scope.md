# Task 0.4+ 热修：统一检索层的授权过滤（grants ∪ 老范围）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让"用 2.0 目录授权（grants）建号的用户"在**检索/问答**里也能看到自己的文档——现在他们搜索一律 0 结果，而看板上却正常。

**Architecture:** 不新增机制。`kbplatform/scopes.py` 已有两个谓词：`visible_predicate`（老范围 `user_scopes`）与 `grants_predicate`（2.0 目录授权 `grants`）。文档看板的 `scope_predicate`（`kbplatform/service.py:645-650`）已经把两者 OR 起来，而**检索层** `Retriever._allowed_chunk_ids`（`kbplatform/retrieval.py:72-81`）只用了老的那一个 → grants-only 用户被全部过滤掉。本任务把"两者合并"的逻辑收进 `scopes.py` 的**唯一入口**，看板与检索都调它。

**Tech Stack:** Python 3.12 / SQLite / pytest（沿用仓库既有风格）

**Spec:** `docs/superpowers/specs/2026-10-25-kb-platform-ux-review-and-roadmap.md`（§2.4/§5 的"统一授权"约束；迭代计划 Task 0.4 的后端已按授权过滤这一前提）

---

## 背景（为什么这是必须的）

生产实测（2026-10-25）：用管理台"目录授权"给普通账号授权（只写 `grants` 表，没有老的 `user_scopes` 行）后——

- 文档看板 `/api/v1/documents` **能看到** 564 篇（走 `scope_predicate` = grants ∪ 老范围）；
- 而 `/api/v1/search`、`/api/v1/query` 的四路召回 **keyword 0 / vector 0 / graph 0**，检索层把用户的文档全过滤掉了。

根因：`retrieval.py:76-77` 用的是 `visible_predicate(user_id, "c", load_scopes(...))`，而 `load_scopes` 只读老的 `user_scopes` 表。启动时的迁移是**老范围 → grants** 单向的，所以 grants-only 用户永远没有老范围行 → 检索层认为他什么都看不到。

这违反迭代计划的全局约束："不改授权模型：`grants` 是唯一授权源；文件页、看板、检索、问答共用同一套过滤"。

## Global Constraints

- **不新增授权模型**：`grants` 仍是唯一授权源；本任务只是让检索层与看板**用同一个谓词**。
- **过滤必须仍然在 SQL 层生效**（不能"先召回再过滤"——那会泄露范围外文档的存在性）。
- **管理员不受限**：`user_id is None` 时不过滤。
- 不引入新的 Python 依赖。
- 不得放宽到"任何人可见"：没有任何授权的用户仍然必须什么都搜不到。

## Review Focus

1. **grants-only 用户**能搜到自己目录下的正文 → 必须有测试钉住。
2. **完全没授权的用户**仍然搜不到任何东西 → 必须有测试钉住（这是安全红线，测试要断言 `total == 0`）。
3. **范围外**的目录不能因为合并谓词而漏进来（谓词是 OR，但两个来源各自都必须是"精确前缀 + 目录边界"，不是裸 `LIKE '%x%'`）。
4. 看板与检索对**同一个用户**的可见范围必须一致（这是本任务的核心不变量）。

---

### Task 1: 抽出统一的可见范围谓词并让检索层用它

**Files:**
- Modify: `kbplatform/scopes.py`（新增 `unified_predicate(conn, user_id, alias="c") -> tuple[str, list]`）
- Modify: `kbplatform/retrieval.py:72-81`（`_allowed_chunk_ids` 改用 `unified_predicate`）
- Modify: `kbplatform/service.py:645-650`（`scope_predicate` 改为调用 `unified_predicate`，消掉重复的合并逻辑）
- Test: `kbplatform/tests/test_unified_authz.py`（已有该文件，追加用例）

**Interfaces:**
- Produces:
  - `kbplatform/scopes.py` → `unified_predicate(conn: sqlite3.Connection, user_id: int, alias: str = "c") -> tuple[str, list]`
    返回 (SQL 谓词, 参数列表)。语义 = `visible_predicate`（老范围） OR `grants_predicate`（2.0 目录授权）；任一为空时退化为另一个；两者都空时返回 `("1=0", [])`。
  - `kbplatform/service.py` 的 `scope_predicate(user)` 变成 `unified_predicate(conn, user["id"], "c")` 的薄封装（管理员仍返回 `("1=1", [])`）。
- Consumes: `visible_predicate`、`grants_predicate`（都已存在于 `kbplatform/scopes.py`，不要重写它们的语义）。

- [ ] **Step 1: 写失败测试**（在 `kbplatform/tests/test_unified_authz.py` 追加）

```python
def test_retrieval_sees_documents_for_grants_only_user(app, tmp_path):
    # 建一个只写 grants（没有 user_scopes 行）的普通用户，
    # 造一篇在他授权目录下的 catalog+chunk，
    # 断言 /api/v1/search 能命中这篇
    ...

def test_retrieval_blocks_user_without_any_grant(app, tmp_path):
    # 一个既没有 grants 也没有 user_scopes 的用户，
    # 断言 /api/v1/search 返回 total == 0
    ...

def test_board_and_search_agree_on_visibility(app, tmp_path):
    # 同一个 grants-only 用户：
    # 断言 /api/v1/documents 里能看到 N 篇，且 /api/v1/search 也能命中同一篇
    ...

def test_unified_predicate_is_or_of_legacy_and_grants(app):
    # 直接测 unified_predicate：给一个既有老范围又有 grants 的用户，
    # 断言返回的谓词同时含两边的子句，且两边参数都在 params 里
    ...
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest kbplatform/tests/test_unified_authz.py -q` → Expected: FAIL（grants-only 用户搜不到）

- [ ] **Step 3: 实现 `unified_predicate`**

放在 `kbplatform/scopes.py`（`grants_predicate` 之后）。语义与 `service.py` 现有的 `scope_predicate` 闭包**完全一致**，但搬到共享模块：

```python
def unified_predicate(conn: sqlite3.Connection, user_id: int,
                      alias: str = "c") -> tuple[str, list]:
    """可见范围谓词 = 老范围（user_scopes） ∪ 2.0 目录授权（grants）。

    文件页、看板、检索、问答共用它：**grants 是唯一授权源**，
    老范围只是还没迁移完的历史数据，两者都要认。
    """
```
组合规则照搬 `service.py:645-650` 的现成写法（legacy 为 "1=0" 时只留 grants；grants 为 "1=0" 时只留 legacy；否则 OR，参数拼接）。

- [ ] **Step 4: 检索层改用它**

`kbplatform/retrieval.py::_allowed_chunk_ids`：
```python
pred, params = unified_predicate(self.conn, user_id, "c")
```
（`user_id is None` 的管理员分支保持不变：返回 None = 不过滤。）

- [ ] **Step 5: 看板改用它**

`kbplatform/service.py::scope_predicate` 改成调用 `unified_predicate`，管理员仍返回 `("1=1", [])`。
注意 `scope_predicate` 里对 legacy/g_pred 的特殊分支（"1=0" 处理）搬进 `unified_predicate` 后要删掉，别留两份。

- [ ] **Step 6: 跑测试确认通过 + 全量 + 提交**

Run: `pytest kbplatform/tests/test_unified_authz.py -q` → PASS
Run: `pytest kbplatform/tests --ignore=kbplatform/tests/test_feishu_kbq.py -q` → 全绿

```bash
git add kbplatform/scopes.py kbplatform/retrieval.py kbplatform/service.py kbplatform/tests/test_unified_authz.py
git commit -F <utf8-no-BOM 文件>
```

- [ ] **Step 7: 生产验证（由控制器执行，实现者只写脚本）**

写一个只读脚本 `tools/check-retrieval-scope.py`：用 HTTP 登录一个 grants-only 临时账号，打 `/api/v1/search`，打印 `total` 与 `stats`。
实现者**只跑 dry-run 打印数字**，真实的临时账号与生产验证由控制器做。

---

## 出口验收

- [ ] grants-only 用户 `/api/v1/search` 命中 > 0（生产实测）
- [ ] 无授权用户 `/api/v1/search` 仍 `total == 0`
- [ ] 看板与检索对同一用户的可见范围一致
- [ ] 全量测试绿
- [ ] 文件页「搜正文」在 grants-only 账号下真的能出结果（浏览器实测）
