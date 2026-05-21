# 第一阶段：中期报告 + 演示原型（2周）

## 目标
跑通"课题 → 抓取 → 排序 → 展示"核心闭环，可现场演示。

## 技术任务

### 1. 项目骨架
- Flet 主窗口 + 页面路由（课题页 / 推荐页 / 设置页）
- SQLAlchemy 模型建表：projects, papers, project_papers

### 2. 关键词提取
- 接入 KeyBERT，实现课题描述 → 技术术语自动提取
- 支持手动输入关键词覆盖

### 3. 数据源接入
- arXiv API 抓取（arxiv 包）
- OpenAlex API 抓取（免Key，requests 直接调）
- 本地 PDF 批量导入：FilePicker 选文件夹 → PyMuPDF 提取标题/摘要
- 数据统一存入 SQLite

### 4. 向量流水线
- FAISS IndexFlatIP 构建索引
- **本地多语言模型**：paraphrase-multilingual-MiniLM-L12-v2，离线运行，支持中英文跨语言匹配（默认方案）
- **在线可选**：Google Gemini text-embedding-004，免费 1500 req/min，需 GEMINI_API_KEY（备选方案）
- 注意：DeepSeek 无 Embedding API（仅 chat 模型），Embedding 不使用 DeepSeek
- 相同摘要 Embedding 本地缓存（numpy .npy）

### 5. 排序展示
- 向量相似度排序，终端输出 Top10（标题 + 得分 + 摘要首句）
- Flet DataTable 展示推荐列表，支持按得分排序

### 6. 测试数据
- 准备 3 个真实课题（如钙钛矿电池、大模型微调、联邦学习）
- 每个课题收集 50 篇混合数据（arXiv + 本地PDF）
- 预计算 Embedding 和基础分析，制作离线演示包

## 演示方式
- 手动点击"刷新推荐"按钮模拟推送
- 中期报告明确画出第二、三阶段扩展架构图

## 交付物
- 可运行的终端版最小闭环
- Flet 基础 UI（课题输入 + 推荐列表 + 设置页）
- 系统架构图 + 算法流程图
- 3 分钟演示录屏
- 50 篇/课题的测试数据集 + 离线演示包
