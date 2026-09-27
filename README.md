# StudyBuddy · 知识图谱版

一个跑在本机浏览器里的错题本：把错题拍照上传 → 自动识别题干 → 对齐到**高中数学知识图谱**
（544 个知识点 / 776 条关系）→ 找到「错这道题真正暴露的薄弱点」，并用教材原文给出讲解。

**不含任何 EXE**：只有源码 + 一个启动脚本 `run.py`，Windows / macOS / Linux 都能跑。

---

## 一、怎么运行

需要 Python 3.10 以上（3.13 最省事）。

```bash
# 1. 第一次运行：自动建虚拟环境并装依赖（国内镜像，一两分钟）
python run.py --install

# 2. 之后每次启动：起服务 + 自动打开浏览器
python run.py
```

macOS / Linux 上如果没有 `python` 命令，用 `python3 run.py`。
Ubuntu/Debian 若提示 venv 不可用：`sudo apt install python3-venv`。

其它用法：

```bash
python run.py --port 9000      # 指定端口（默认 8000，被占用会自动顺延）
python run.py --no-browser     # 只起服务，不开浏览器
python run.py --server ...     # 参数透传给服务端（开发用，支持 --reload）
```

关闭终端（Windows 上是那个黑窗口）即退出程序。

## 二、第一次用要配 API

没有密钥也能先玩：把「服务商」选成 `mock`，用离线样例熟悉流程（不联网、不识别真实图片）。

要真正识别题目，启动后点网页右上角 **「⚙ 设置」**：

| 字段 | 填什么 |
| --- | --- |
| 服务商 | DeepSeek（或任何 OpenAI 兼容服务：通义 / 智谱 / 本地 Ollama…） |
| API 地址 | `https://api.deepseek.com/v1` |
| API Key | 你自己的密钥（保存后只存在本机 `app/.env`，界面不回显） |
| 识图模型 | 点「⬇ 拉取模型列表」选一个支持图片的模型，如 `deepseek-flash` |

也可以手动复制 `app/.env.example` 为 `app/.env` 再填。

## 三、六个页面

| 页面 | 做什么 |
| --- | --- |
| ① 录入错题 | 拍照/截图上传（支持整页试卷与 PDF：按页识别、多题先勾选再入库）。识别不准可在「渲染 / 原文」两栏里手工改题干 |
| ② 错题本 | 全部错题，左侧可按**错因、录入日期、排序**筛选；卡片上有「问 AI / 重新分析 / 标记已订正 / 删除」 |
| ③ 知识点热度 | 哪些知识点错得最多（冷→热热力色阶），模块薄弱概览与复习计划 |
| ④ 图谱查询 | 按名字/别名/标签检索知识点，看前置依赖、学习路径、根因下钻 |
| ⑤ 图谱视图 | 整册/模块的力导向图：颜色=你的错题热度，大小=被依赖程度 |
| ⑥ AI 对话 | 问概念或带一道错题来问。回答先用**图谱知识点与教材原文**作答，再由另一个角色**复核一遍**（超纲、算错、漏答都会被挑出来改正） |

## 四、数据在哪

| 内容 | 位置 |
| --- | --- |
| 错题库、解析 | `app/data/db.sqlite` |
| 上传的原图 | `app/data/assets/` |
| 识别失败日志 | `app/data/logs/` |
| API 密钥 | `app/.env`（只在本机，别外发） |

备份就是把 `app/data/` 整个拷走。删掉它等于恢复出厂（图谱是只读的，不受影响）。

## 五、目录结构

```
run.py                       ← 唯一入口
requirements.txt
app/
  server.py                  FastAPI 服务与接口
  desktop.py                 桌面入口：挑端口 + 开浏览器
  config.py                  路径与 .env 读取
  mathkg/                    知识图谱：加载、检索、对齐、图算法
  wrongbook/                 错题业务：识别、对齐、解析、热度、AI 对话
  static/                    前端（原生 JS，无构建步骤）
  data/kg_snapshot.json      图谱快照（544 知识点 / 776 关系）
ontology/                   本体源数据（可重新构建快照）
pipeline/data/segments/     教材原文片段（讲解的引用来源）
```

## 六、常见问题

- **提示 ModuleNotFoundError: fastapi** → 用 `python run.py --install` 建环境，或先 `pip install -r requirements.txt`
- **识别说「未配置 API Key」** → 见上面第二节；设置里保存后立即生效，不用重启
- **模型返回成功但内容是空的** → 推理型模型把 token 花在思考上了，在「设置 → 高级选项」把输出上限调到 8192 以上
- **端口被占用** → 脚本会自动顺延到 8010 / 8080…；也可以 `python run.py --port 9000`
- **整页试卷只识出一两道题** → PDF/大图是按页识别的，页面太多时注意「设置」里的页数上限
