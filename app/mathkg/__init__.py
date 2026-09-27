# -*- coding: utf-8 -*-
"""图谱查询引擎：纯 Python、确定性、可单测。

模块划分：
  store.py   快照加载与图结构索引
  search.py  检索打分（中文 n-gram + 别名 + 标签）
  graph.py   前置子图、拓扑排序、根因下钻
  link.py    知识点对齐（LLM 候选名 → 图谱真实 ID）
"""

from .store import KG, get_kg  # noqa: F401
from .search import search  # noqa: F401
from .link import link_candidates  # noqa: F401
