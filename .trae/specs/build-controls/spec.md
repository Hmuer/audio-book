# 构建暂停、继续与停止 PS

## 产品语义

构建列表行头提供暂停、继续、停止按钮，不要求展开详情。queued/running 可暂停；paused 可继续；queued/running/pausing/paused 可停止。停止必须二次确认，不删除已完成章节和段缓存，不允许继续停止的原构建。现有 cancel API 保持兼容，新页面使用 stop。

状态：queued/running → pausing → paused；paused → queued → running；queued/running/pausing/paused → stopping → cancelled。显示文案：暂停中、已暂停、停止中、已停止。

暂停和停止均在安全边界生效：当前章节与已派发 TTS 完成，或当前 ZIP 分片完成后退出。不承诺打断供应商已接受请求，不承诺避免在途计费。控制请求幂等；无效状态返回 409，不存在或项目归属不符返回 404，无写权限沿用现有权限响应。

控制中继续轮询，已暂停不自动继续。刷新页面保留状态。请求期间禁用重复按钮，显示错误；停止确认说明不能继续原构建、产物保留、当前请求可能继续计费。运行或控制中禁用删除；暂停时可查看下载已有章节。

## 后端实现计划

1. 在 Build 和兼容迁移增加 nullable resume_digest。新建/重试首次执行前保存输入恢复指纹：现有内容 digest、角色名称与性别（排序）、构建快照音色、速度、模式、厂商、旁白风格、默认音色，以及当前实际路由使用的 TTS 模型。首次暂停旧构建需要保存指纹。恢复时重新计算，变化拒绝恢复，不改写旧产物。
2. build.py 提供 pause_build、stop_build、resume_build。控制请求使用项目串行锁及条件状态更新；paused 阻止同项目新建、重试和内容编辑（暂停快照保护）。resume 复用 build_id 和持久化音色，不调用 start_build（避免成功历史复用）；拒绝存在该项目活跃 worker；共用 JobTask 心跳 runner。
3. worker 包装层统一在启动前、章节前后、LRC/归档安全边界、每个 ZIP 分片前后检查控制意图；控制退出不进入章节错误静音降级。在途章节完成并持久化后退出。runner 在用量补记和活跃登记释放后条件确认 paused/cancelled，防止恢复与旧 worker 重叠；异常不得覆盖 pausing/stopping/cancelled。
4. start、retry、孤儿恢复 runner 都使用统一控制收尾。JobTask 支持 paused 终态；看门狗不重启 paused，遇到孤儿 pausing/stopping 直接确认控制目标并收尾。不要引入跨进程租约系统，保持现有单进程架构。
5. 用量按每次执行增量累计，避免恢复覆盖旧账单；章节和终态用量必须加本次执行开始的基数，finally 使用本次 scope 增量加基数，不双记。缓存命中不计。已有 failed 章节恢复应保留失败产物供最后打包，不重新静默改变失败章语义；本地已完成文件缺失先回源，无法恢复则合成。
6. 新增 POST /projects/{project_id}/builds/{build_id}/pause、/resume、/stop，返回 BuildResp。项目状态/忙碌判定涵盖 pausing/stopping/paused，paused 显示暂停而非正在合成。现有 cancel 兼容行为不改。

## 前端实现计划

api.ts 新增 buildPause/buildResume/buildStop。构建状态 helper 中 queued/running/pausing/stopping 为轮询状态，paused 非运行。列表徽标、构建行按钮、暂停/停止说明、错误反馈、二次确认、按钮请求互斥、删除禁用和展开详情状态变化补拉。paused 时开始新建按钮禁用并提示继续/停止现有构建，避免新建返回旧暂停任务。

## 验收与验证

- 启动即暂停、合成中暂停、最后一章/打包暂停、暂停后刷新和进程重启。
- 暂停等待在途章节，但不启动下一章节；确认 paused 时 worker 已注销。重复暂停/停止不重复启动。
- 恢复原 build，成功章与段缓存复用，指纹变更拒绝，未知构建与跨项目访问拒绝。
- 暂停中改为停止，暂停态停止，停止后不可恢复，终态不被迟到 worker 覆盖。
- 原构建和 retry/recover 三条执行路径均覆盖控制与用量累计。
- 并发 resume 只产生一个 worker；paused 不被孤儿自动恢复；控制中不允许新建/编辑/删除。
- 保持现有 cancel 竞态、默认男女音色、MiniMax 和计费测试。
- 后端 pytest、前端 node:test、tsc、生产 build、Ruff/Mypy 基线对比；缺 ESLint 配置作为既有阻塞报告，不新增无关配置。

## 非目标

不实现立即硬中断供应商、不暂停识别、不删除产物、不新增任务队列、不修复无关缓存淘汰或历史重试问题、不修改 MiniMax provider 和其测试中的用户变更。
