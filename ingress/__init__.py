# -*- coding: utf-8 -*-
"""弹幕源与互动流水线。

数据流：Source(异步任务) → EventCenter.ingest() → 归一化/去重/连击聚合
      → Pipeline.publish() → EventBus(topics live.*) → 各订阅模块
"""
