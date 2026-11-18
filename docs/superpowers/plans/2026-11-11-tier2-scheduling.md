# Tier 2 提炼调度优化（逐条）

日期：2026-11-11
背景：18.5 万篇新接入文档把 index 队列打到 6.2 万，`pipeline.run_once()`
的 Tier 1 内层 while 要抽干队列才轮到 Tier 2 → 82,082 个 extract 任务
`attempts=0` 卡了两天，本地/远端大模型全程空转（两台 Ollama `/api/ps`
都没有生成模型）。636,108 块按 30s/块串行 ≈ 221 天。

## 1. 提炼不再被索引队列饿死（交错/解耦）
- [x] RED：`test_extract_runner.py::test_runner_processes_extract_jobs_while_index_backlog_remains`
- [x] GREEN：`ExtractRunner` 后台线程独立消费 extract 任务；`run_forever` 启动它
- [x] `run_once` 保留同步 Tier 2，仅在没有后台线程时执行（`--once` / 单测）

## 2. 并发化：按成员限流，不让同一台 Ollama 被多线程打爆
- [x] RED：`test_job_queue.py::test_concurrent_claims_never_duplicate`（旧实现 200 个任务被领 205 次）
- [x] RED：`test_parse_pool.py::test_pool_limit_reads_extra_concurrency`
- [x] RED：`test_parse_pool.py::test_pool_capacity_is_sum_of_member_limits`
- [x] RED：`test_extract_runner.py::test_runner_runs_two_members_in_parallel`
- [x] RED：`test_extract_runner.py::test_runner_never_exceeds_member_concurrency`
- [x] GREEN：`JobQueue.claim` 单语句 `UPDATE ... RETURNING`；新增 `release()`
- [x] GREEN：`ParsePool` 增加锁、`extra.concurrency`、`capacity()`
- [x] 额外：`idx_jobs_catalog(kind,catalog_id,state)`——启动补队耗时 390s → 0.14s

## 3. 用量可见：model_usage 真正写入并接到算力池状态
- [x] RED：`test_extract.py::test_extract_records_model_usage`
- [x] RED：`test_extract.py::test_extract_records_usage_for_single_model`
- [x] RED：`test_parse_pool.py::test_record_usage_accumulates_for_pipeline_bucket`
- [x] RED：`test_parse_pool.py::test_status_includes_today_usage`
- [x] GREEN：`models.record_usage()`；`extract_file` 每块记一次；`ParsePool.status()` 带今日用量

## 4. 降本（用户拍板：跳过表格）
- [x] 统计：待提炼 82,445 篇 / 639,158 块；`.xlsx` 占 515,271 块（81%），
      最大单篇 `日发运计划表.xlsx` 15.5 万块；党建目录 71,622 篇
- [x] RED：`test_extract_runner.py::test_runner_skips_spreadsheets_without_calling_llm`
- [x] RED：`test_pipeline_scheduling.py::test_pipeline_does_not_enqueue_extract_for_skipped_ext`
- [x] GREEN：`Config.extract_skip_exts` + `extract.is_skipped_ext / mark_extract_skipped`；
      入队时跳过 + 领取时兜底 + `status_detail` 收口
- [x] 线上一次性收口：11,727 篇 / 515,271 块标记跳过，关闭 11,727 个任务
      → 剩余 70,696 篇 / 123,862 块

## 6. 调参实验（2026-11-11 14:00 左右，结论：已到硬件上限，全部回滚）
- [x] `num_ctx` 65536 → 16384（两端）：显存 7.06/7.15 → 5.63/5.73GB，
      **吞吐无变化**（4.1 vs 3.9 块/分钟）→ 回滚。原因是 Ollama 把 `num_ctx`
      按并发槽位均分，16k 在 3 槽下每槽只剩 5.4k token，长块会被截断。
- [x] 远端并发 3 → 6：Ollama 把 context 自动压到 16384（每槽 2.7k token），
      **5 分钟 0 次调用**，整台卡死 → 回滚，并 `keep_alive=0` 强制按 65k 重载。
- [x] 远端并发 3 → 4：context 仍被压到 16384，5 分钟 0 次远端调用 → 回滚。
      结论：192.0.2.77 只支持 **3 路 × 64k**。
- [x] 本地并发 1 → 2：吞吐无提升（3.5 vs 3.9），且 llama-server 私有内存涨到
      **17GB**（本机物理内存只有 15.8GB，可用掉到 1.1GB）→ 回滚。
- [x] 本地 `num_ctx` 最终定在 **32768**（不是 65536）：65k 时 KV cache 约 9.6GB
      塞不进 12GB 显存，溢出到主机内存把可用内存吃到 0.9GB；16k 又装不下真实的
      问答提示（实测约 1.7 万 token），32k 两头都够，且吞吐与 65k 相同。
- [x] 最终配置：本地 #1 `concurrency=1, num_ctx=32768`；
      远端 #10 `concurrency=3, num_ctx=65536` → 4 条线程，约 **4 块/分钟**。
- 结论：再快只能加机器（再挂一台带 GPU 的 Ollama 进池），或换更小的模型。

## 6.1 问答 500「提问失败」修复（2026-11-11 下午）
- [x] `vectors.py`：`_matrix()` 把 68.8 万行 fp16 整表 `astype(float32)`
      （一次 2.63GB），本机内存紧张时直接 `MemoryError` → `/api/v1/query` 500。
      改成 `SCAN_BLOCK=16384` 分块扫描 + `argpartition` 取 top-k，峰值 64MB。
- [x] `retrieval.py::_graph()`：宽泛问题（「人力有哪些流程」）扩出上万邻居，
      `src IN (?) OR dst IN (?)` 超过 SQLite 32766 变量上限 →
      `OperationalError: too many SQL variables` → 500。改成每批 400 个 id。
- [x] 回归：两条提问修复前分别 500/500，修复后 200/200；新增 3 个回归用例。

## 5. 上线与验证
- [x] 全量 pytest：559 个用例，1 个失败（`test_web_assets.py::test_graph_layout_*`，他人图谱 WIP，与本轮无关）
- [x] 线上池配置：本地只留 #1（concurrency=1），远端 #10（concurrency=3），#2/#11 移出池
- [x] 重启 ingest（supervisor 自动拉起）
- [x] 验证：两端 `/api/ps` 都有生成模型；`model_usage` 本地 3 次 / 远端 4 次；
      extract 队列 `running=4`、`tier2_done` 555 → 561
