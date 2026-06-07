# PaperPilot Phase 2 交付报告

> 写给搭档（B / 应用展示层）。本文档覆盖 Phase 2 新增的全部后端能力，以及如何在 UI 中正确调用。

---

## 一、新增文件总览

| 文件 | 说明 |
|------|------|
| `paperpilot/ai_service.py` | AI 服务层 — 精读 / 打分 / 对话 / 论文问答 |
| `paperpilot/conversation.py` | 对话上下文管理 — 持久化 / 压缩 / 回滚 |
| `paperpilot/export.py` | 数据导出 — BibTeX / CSV |

---

## 二、AIService（ai_service.py）

全局单例：`_ai_service = AIService()`（app.py 第 80 行），自动从 `config.yaml` 读取 DeepSeek API Key 和模型名。

### 2.1 核心方法一览

| 方法 | 用途 | 触发场景 |
|------|------|----------|
| `is_available` | API Key 是否已配置 | UI 中控制按钮 disabled 状态 |
| `deep_read(paper, full_text)` | 单篇结构精读，返回 JSON | 文献库点 📖 按钮 |
| `score_papers(topic, papers)` | 批量打分排序（0-100） | 检索结果页 / 文献库「AI 精排」 |
| `chat(project_id, name, msg, ...)` | 课题对话（自动持久化 + 压缩） | Agent 面板发送消息 |
| `ask_question(paper, question, ...)` | 单篇论文深度问答 | 论文详情弹窗 / Agent 面板引用论文时 |
| `log_message(project_id, ...)` | 写入对话记录（不调 API） | deep_read 等非 chat 流程，确保结果持久化 |

### 2.2 chat() — 详细说明

```python
def chat(
    self,
    project_id: int,          # 课题 DB ID
    project_name: str,        # 课题名（用于磁盘路径）
    message: str,             # 用户输入文本
    topic_desc: str = "",     # 课题描述
    papers: list[dict] = None,        # 用户显式附带的论文
    project_papers: list[dict] = None, # 课题下全部论文（用于自动检测引用）
) -> dict:  # {"reply": str, "compressed": bool}
```

**自动论文检测**：当传入 `project_papers` 时，chat() 会自动从用户消息中检测论文引用：
1. `@论文标题` 显式引用 → 匹配
2. 消息中包含完整标题 → 匹配
3. 标题 ≥15 字符时，12 字符滑动窗口子串匹配

检测到的论文会自动注入摘要到对话上下文，让 AI 能基于论文内容回答。

### 2.3 ask_question() — 论文问答

```python
def ask_question(
    self,
    paper: dict,              # 论文 dict（需含 title, abstract）
    question: str,            # 用户问题
    full_text: str = None,    # 全文（可选，为 None 时按需自动获取）
    session_id: str = None,   # 多轮追问会话 ID
) -> str  # AI 回答
```

- 默认注入摘要（~1000 chars）
- 问题含「方法/实验/数据/细节」等关键词时，自动获取全文注入（≤30000 chars）
- 传入 session_id 可维持多轮追问上下文

### 2.4 deep_read() — 精读

采用 RLM 三层阅读策略：
- **< 8000 字符**：全文直接注入
- **8000 ~ 60000 字符**：滑动窗口（6000 字/窗，500 字重叠），写渐进笔记后合成
- **> 60000 字符**：切块独立分析后合成

返回结构：
```json
{
  "core_contribution": "论文核心问题（一句话）",
  "method": "关键技术路线（2-3句）",
  "key_evidence": "支撑结论的核心实验或数据",
  "highlights": "创新点（2-3句）",
  "limitations": "明显局限或改进空间",
  "scores": {"novelty": 7, "rigor": 6, "significance": 8}
}
```

结果自动保存到：
- 数据库 `project_papers.deep_read_notes` 字段
- 本地 `outputs/deep_read/{slug}.json`

### 2.5 AI 精排 score_papers()

- 评分维度：relevance(40%) + method(25%) + novelty(20%) + recency(15%)
- 每项 1-10 分，加权后 ×10 → 总分 0-100
- 分档：S(85-100) / A(70-84) / B(55-69) / C(40-54) / D(0-39)
- 返回含详细中文理由，按总分降序排列
- 安全上限：单次最多 20 篇（可配置上限 100）

---

## 三、ConversationManager（conversation.py）

### 3.1 存储结构

对话持久化到 `repository/{课题名}/conversation.json`：

```json
{
  "_meta": {
    "project_name": "...",
    "created_at": "...",
    "updated_at": "...",
    "total_rounds": 42,
    "estimated_tokens": 35000,
    "compressed_count": 2
  },
  "messages": [
    {"role": "user", "content": "...", "timestamp": "...", "attached_papers": [...]},
    {"role": "assistant", "content": "...", "timestamp": "..."}
  ],
  "compressed": [
    {"rounds_summary": "...", "original_rounds": 10, "compressed_at": "..."}
  ]
}
```

### 3.2 压缩策略

- **触发阈值**：消息总 token 数 > 80000（128K 窗口，留 48K 给回复）
- **压缩方式**：取最早 1/3 的消息，调用 AI 压缩为 ≤500 字中文摘要
- **存储位置**：压缩后的摘要存入 `compressed` 数组，原始消息从 `messages` 中移除
- **注入方式**：每次构建 API 消息时，全部压缩摘要注入 system prompt

### 3.3 UI 展示

- 初始加载最近 30 轮（60 条消息）
- `has_more_history` 判断是否有更早消息可加载
- 压缩摘要在消息列表中显示为特殊气泡

### 3.4 持久化规则

**自动持久化**：
- `chat()` → 自动写入 user + assistant 消息
- `deep_read` → 通过 `log_message()` 写入精读请求 + 结果

**需手动调用 log_message()**：
- 任何通过 `send_agent_message(role="agent")` 显示在 Agent 面板的结果，必须调用 `log_message()` 写入

---

## 四、导出模块（export.py）

| 函数 | 说明 |
|------|------|
| `to_bibtex(papers)` | 论文列表 → BibTeX 字符串，entry key 为 `第一作者姓+年份+标题首词` |
| `to_csv(papers)` | 论文列表 → CSV 字符串（中文表头） |
| `save_file(content, path)` | UTF-8 写入文件，自动创建父目录 |

### UI 调用方式

```python
# 导出按钮点击 → tkinter 文件保存对话框
def _do_export(ext: str, label: str, convert):
    papers = _get_export_papers()  # 多选取选中，否则取全部
    content = convert(papers)
    # 弹出保存对话框，默认文件名含课题名+时间戳
    # ...
```

---

## 五、UI 接入快速上手指南

### 5.1 全局状态（app.py 中已就绪）

```python
_ai_service = AIService()           # AI 服务实例
_agent_project_id: int | None       # 当前 Agent 关联的课题 ID
_agent_project_name: str            # 课题名
_agent_topic_desc: str              # 课题描述
```

### 5.2 切换课题

```python
set_agent_project(project_id, project_name, topic_desc)
# → 自动加载该课题的对话历史到 Agent 面板
# → 传入 None 清空面板
```

### 5.3 发送消息（Agent 面板）

```python
def _on_agent_send(e):
    text = _agent_input.value.strip()
    send_agent_message(text, role="user")   # 先显示用户消息
    # 加载课题论文
    _proj_papers = library.get_project_papers(_agent_project_id)
    # 调用 AI
    result = _ai_service.chat(
        project_id=_agent_project_id or 0,
        project_name=_agent_project_name or "通用",
        message=text,
        topic_desc=_agent_topic_desc,
        project_papers=_proj_papers,  # 自动检测论文引用
    )
    send_agent_message(result["reply"], role="agent")
```

### 5.4 精读（文献库 📖 按钮）

```python
_on_deep_read(e, paper)
# → 自动获取全文 → RLM 分析 → 结果发送到 Agent 面板 + 持久化
# → 结果也保存到数据库和 outputs/deep_read/
```

### 5.5 AI 精排

检索结果页：
```python
# 取当前 CE 排序后的 top-K 篇
limit = int(ai_limit_dd.value)
candidates = [p for p, s in state.scores[:limit]]
results = _ai_service.score_papers(state.topic_desc, candidates, max_papers=limit)
# → results 含 ai_score + ai_reason，合并回 state.scores 渲染表格
```

文献库：
```python
papers = library.get_project_papers(project_id)
results = _ai_service.score_papers(topic_desc, papers)
library.update_paper_ai_scores(project_id, results, papers)
```

### 5.6 论文问答

```python
# 直接在 Agent 面板发送消息，chat() 自动检测论文引用
# 或直接调用：
reply = _ai_service.ask_question(paper, "这篇论文的实验样本量够吗？")
```

### 5.7 按钮禁用逻辑

```python
# 所有 AI 功能按钮
btn.disabled = not _ai_service.is_available
btn.tooltip = "请先配置 DeepSeek API Key" if btn.disabled else ""
```

---

## 六、关键注意事项

1. **API Key 安全**：`config.yaml` 已在 `.gitignore` 中，绝不会被提交或推送
2. **所有 AI 调用都在后台线程**：`threading.Thread(target=..., daemon=True).start()`，UI 不阻塞
3. **结果轮询用 `page.run_task()`**：在 async 函数中 `await asyncio.sleep(0.3)` 轮询 `_done` 事件
4. **对话持久化**：Agent 面板展示的 AI 结果必须调用 `log_message()` 写入，否则关闭软件后丢失
5. **论文引用检测**：`chat()` 的 `project_papers` 参数传入课题全部论文后，AI 自动识别用户引用的论文
6. **全文获取链路**：PDF 提取 → HTML 缓存 → HTML 下载，自动降级，失败返回 None
7. **压缩透明**：对话 token 超 80K 自动触发，对用户无感知（仅 `compressed: true` 标记）

---

## 七、TODO / 待接入

| 功能 | 状态 | 备注 |
|------|------|------|
| @mention 自动补全 UI | 🔲 未开始 | 输入框监控 @ 字符，弹出论文列表 |
| 批量对比 UI | 🔲 未开始 | `batch_compare` 方法未实现，需选定多篇后触发 |
| 课题讨论专用入口 | 🔲 未开始 | `discuss_topic` 方法未实现 |
| 对话管理按钮 | 🔲 未开始 | 清空对话 / 导出对话记录 |
| dialog z-order 修复 | 🔲 Issue #30 | 弹窗有时出现在主窗口后面 |
| app.py 与 app_copy.py 同步 | ✅ 已同步 | 本次提交已合并 |

---

*报告生成于 2026-06-07，对应 commit `4f329cd`，已合并到 develop*
