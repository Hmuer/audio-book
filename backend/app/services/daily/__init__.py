"""科技前沿日报：采集 → 聚类 → 打分 → 脚本 → TTS → RSS。

模块划分与附件方案 §二 的流水线一一对应：

    collect   多源 RSS / 24h 窗口 / 去重
    cluster   事件聚类（跨源合并）
    score     乘法打分 + 安全一票否决 + 主题闸门
    script    LLM 可插拔，离线模板兜底
    tts       分段合成 + 拼接 + 响度标准化
    feed      RSS 2.0 + iTunes（enclosure 绝对地址、?key= 鉴权）
    store     每期一个产物目录（meta/script/shownotes/audio）
    pipeline  编排 + 单实例运行锁 + 运行状态

对外只暴露 API 层需要的少数入口；内部结构可独立测试。
"""
