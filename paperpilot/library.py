"""文献库 CRUD 接口。

提供课题管理、论文收藏、阅读状态追踪功能，复用 models.py 现有五张表。
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from paperpilot.models import Base, Project, Paper, ProjectPaper, Feedback

_DB_PATH = "paperpilot.db"
_engine = None
_SessionLocal = None


def _get_session() -> Session:
    """获取数据库会话（单引擎，复用 session factory）。"""
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(f"sqlite:///{_DB_PATH}", echo=False)
        Base.metadata.create_all(_engine)
        _SessionLocal = sessionmaker(bind=_engine)
    return _SessionLocal()


# ── 课题管理 ──

def create_project(name: str, description: str, push_interval_days: int = 7) -> Project:
    """创建新课题。"""
    session = _get_session()
    try:
        project = Project(
            name=name,
            description=description,
            push_interval_days=push_interval_days,
        )
        session.add(project)
        session.commit()
        session.refresh(project)
        return project
    finally:
        session.close()


def get_all_projects() -> list[Project]:
    """获取所有课题列表，按创建时间倒序。"""
    session = _get_session()
    try:
        return session.query(Project).order_by(Project.created_at.desc()).all()
    finally:
        session.close()


def get_project(project_id: int) -> Project | None:
    """按 ID 获取单个课题。"""
    session = _get_session()
    try:
        return session.query(Project).filter(Project.id == project_id).first()
    finally:
        session.close()


def delete_project(project_id: int) -> bool:
    """删除课题及其关联数据（级联删除 papers/keywords/feedback）。"""
    session = _get_session()
    try:
        project = session.query(Project).filter(Project.id == project_id).first()
        if not project:
            return False
        session.delete(project)
        session.commit()
        return True
    finally:
        session.close()


# ── 论文管理 ──

def _find_or_create_paper(session: Session, paper_dict: dict) -> Paper:
    """在 session 内查找或创建 Paper 记录。

    去重策略：DOI 优先 → 标题 + 年份。
    """
    # 1. DOI 匹配
    doi = paper_dict.get("doi")
    if doi:
        existing = session.query(Paper).filter(Paper.doi == doi).first()
        if existing:
            return existing

    # 2. 标题 + 年份匹配
    title = (paper_dict.get("title") or "").strip()
    year = paper_dict.get("year")
    if title:
        q = session.query(Paper).filter(Paper.title == title)
        if year is not None:
            q = q.filter(Paper.year == year)
        existing = q.first()
        if existing:
            return existing

    # 3. 新建
    paper = Paper(
        title=title,
        authors=(paper_dict.get("authors") or "")[:500],
        abstract=(paper_dict.get("abstract") or ""),
        year=year,
        source=paper_dict.get("source", "unknown"),
        url=paper_dict.get("url"),
        doi=doi,
    )
    session.add(paper)
    session.flush()
    return paper


def save_papers_to_project(
    project_id: int,
    papers: list[dict],
    scores: list[tuple[dict, float]] | None = None,
) -> int:
    """将检索结果保存到课题。

    已存在于课题中的论文不重复添加（通过 ProjectPaper 去重）。

    Args:
        project_id: 目标课题 ID
        papers: paper dict 列表
        scores: rank_papers 返回的 [(paper, score), ...]，可选

    Returns:
        本次新增的论文数量
    """
    # 构建 paper → score 映射
    score_map: dict[str, float] = {}
    if scores:
        for paper_dict, score in scores:
            key = (paper_dict.get("doi") or paper_dict.get("title", "")).strip().lower()
            if key:
                score_map[key] = float(score)

    session = _get_session()
    added = 0
    try:
        project = session.query(Project).filter(Project.id == project_id).first()
        if not project:
            return 0

        for paper_dict in papers:
            paper = _find_or_create_paper(session, paper_dict)
            paper_id = paper.id

            # 检查是否已关联到该课题
            existing = (
                session.query(ProjectPaper)
                .filter(
                    ProjectPaper.project_id == project_id,
                    ProjectPaper.paper_id == paper_id,
                )
                .first()
            )
            if existing:
                continue

            # 获取分数
            key = (paper_dict.get("doi") or paper_dict.get("title", "")).strip().lower()
            total_score = score_map.get(key, 0.0)

            pp = ProjectPaper(
                project_id=project_id,
                paper_id=paper_id,
                total_score=total_score,
                score_similarity=total_score,
                status="unread",
            )
            session.add(pp)
            added += 1

        session.commit()
    finally:
        session.close()

    return added


def get_project_papers(
    project_id: int,
    status_filter: str | None = None,
) -> list[dict]:
    """获取课题下的论文列表。

    Args:
        project_id: 课题 ID
        status_filter: 可选的状态筛选 ("unread" / "skimmed" / "deep_read")

    Returns:
        dict 列表，包含论文字段 + project_paper 关联字段
    """
    session = _get_session()
    try:
        q = (
            session.query(ProjectPaper, Paper)
            .join(Paper, ProjectPaper.paper_id == Paper.id)
            .filter(ProjectPaper.project_id == project_id)
        )
        if status_filter:
            q = q.filter(ProjectPaper.status == status_filter)
        q = q.order_by(ProjectPaper.total_score.desc())

        results = []
        for pp, paper in q.all():
            results.append({
                "id": paper.id,
                "project_paper_id": pp.id,
                "title": paper.title,
                "authors": paper.authors,
                "abstract": paper.abstract,
                "year": paper.year,
                "source": paper.source,
                "url": paper.url,
                "doi": paper.doi,
                "total_score": pp.total_score,
                "status": pp.status,
                "ai_notes": pp.ai_notes,
                "user_notes": pp.user_notes,
                "added_at": str(pp.added_at) if pp.added_at else None,
            })
        return results
    finally:
        session.close()


def update_paper_status(project_paper_id: int, status: str) -> bool:
    """更新论文阅读状态。

    Args:
        project_paper_id: ProjectPaper 的 ID（非 Paper ID）
        status: "unread" / "skimmed" / "deep_read"

    Returns:
        True 表示更新成功，False 表示记录不存在
    """
    valid_statuses = {"unread", "skimmed", "deep_read"}
    if status not in valid_statuses:
        raise ValueError(f"无效状态: {status}，有效值: {valid_statuses}")

    session = _get_session()
    try:
        pp = session.query(ProjectPaper).filter(ProjectPaper.id == project_paper_id).first()
        if not pp:
            return False
        pp.status = status
        session.commit()
        return True
    finally:
        session.close()


# ── 用户笔记 ──

def update_user_notes(project_paper_id: int, notes: str) -> bool:
    """更新论文的用户批注。"""
    session = _get_session()
    try:
        pp = session.query(ProjectPaper).filter(ProjectPaper.id == project_paper_id).first()
        if not pp:
            return False
        pp.user_notes = notes
        session.commit()
        return True
    finally:
        session.close()


# ── 反馈记录 ──

def record_feedback(project_paper_id: int, action_type: str) -> bool:
    """记录用户行为反馈（star / skip / deep_read）。"""
    valid_actions = {"star", "skip", "deep_read"}
    if action_type not in valid_actions:
        raise ValueError(f"无效操作: {action_type}，有效值: {valid_actions}")

    session = _get_session()
    try:
        fb = Feedback(
            project_paper_id=project_paper_id,
            action_type=action_type,
        )
        session.add(fb)
        session.commit()
        return True
    finally:
        session.close()
