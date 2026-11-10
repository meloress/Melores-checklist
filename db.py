"""Baza: Trello botning Postgres'i.

Ikki xil jadval:
- Trello botniki (`employees`, `kpi_logs`, `app_settings`) — bu yerda faqat
  KERAKLI ustunlar xaritalangan. Sxemasini Trello bot (Alembic) boshqaradi,
  bu bot ularni HECH QACHON yaratmaydi/o'zgartirmaydi.
- O'zimizniki (`checklist_*`) — ishga tushganda `create_all` bilan yaratiladi.
"""

from datetime import date, datetime

from sqlalchemy import (BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, Numeric,
                        String, Text, UniqueConstraint, func, text)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from config import settings

# pool_pre_ping: Railway proksisi bo'sh ulanishlarni jimgina uzadi (Trello botda
# shu sabab so'rov abadiy osilib qolgan).
engine = create_async_engine(settings.async_database_url, pool_pre_ping=True)
async_session: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _now_col():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# --- Trello botning jadvallari (faqat o'qish; next_payment_date yoziladi) ---

class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    full_name: Mapped[str] = mapped_column(String(150))
    role: Mapped[str] = mapped_column(String)  # admin/supervisor/observer/worker/brigadier/seller
    is_active: Mapped[bool] = mapped_column(Boolean)
    next_payment_date: Mapped[date | None] = mapped_column(Date)


class KpiLog(Base):
    __tablename__ = "kpi_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    score: Mapped[float] = mapped_column(Numeric(8, 2, asdecimal=False))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col()


class AppSetting(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    balls_per_day_shift: Mapped[int] = mapped_column(Integer)


# --- Checklist jadvallari ---

_EMP_FK = dict(ondelete="CASCADE")  # Trello bot xodimni o'chirsa, to'sib qolmasin


class ChecklistQuestion(Base):
    __tablename__ = "checklist_questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", **_EMP_FK), index=True)
    # Savol BIR MARTALIK: shu kuni 19:00 da yuboriladi (doimiy ro'yxat emas).
    day: Mapped[date] = mapped_column(Date, index=True)
    text: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0)
    # O'chirish = is_active=False: eski javoblar tarixi saqlanadi.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _now_col()


class ChecklistAnswer(Base):
    __tablename__ = "checklist_answers"
    __table_args__ = (UniqueConstraint("employee_id", "question_id", "day", name="uq_checklist_answer"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", **_EMP_FK), index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("checklist_questions.id", ondelete="CASCADE"))
    day: Mapped[date] = mapped_column(Date)
    # Savol keyin tahrirlansa ham, o'sha kuni nima so'ralgani ko'rinsin.
    question_text: Mapped[str] = mapped_column(Text)
    answer_text: Mapped[str] = mapped_column(Text)
    answered_at: Mapped[datetime] = _now_col()


class ChecklistViewer(Base):
    """Nazoratchi (viewer) qaysi xodimlarning javobini ko'radi. Admin belgilaydi."""

    __tablename__ = "checklist_viewers"
    __table_args__ = (UniqueConstraint("viewer_id", "employee_id", name="uq_checklist_viewer"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    viewer_id: Mapped[int] = mapped_column(ForeignKey("employees.id", **_EMP_FK), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", **_EMP_FK))


class ChecklistDay(Base):
    """Xodim × kun. `delivered_at` — savollar xabari birinchi marta YETIB
    BORGAN payt (yetmagan bo'lsa jarima yozilmaydi). `closed_at` — 21:00
    hisobi qilingan; qayta ishga tushganda ikki marta jarima yozilmasligi uchun."""

    __tablename__ = "checklist_days"
    __table_args__ = (UniqueConstraint("employee_id", "day", name="uq_checklist_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", **_EMP_FK), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    asked: Mapped[int] = mapped_column(Integer, default=0)
    answered: Mapped[int] = mapped_column(Integer, default=0)
    penalty: Mapped[float] = mapped_column(Numeric(6, 2, asdecimal=False), default=0)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Chatda hozir javob kutilayotgan savol — bot qayta ishga tushsa ham yo'qolmaydi.
    asking_question_id: Mapped[int | None] = mapped_column(Integer)
    # Telegram "bloklangan / chat yo'q" degan payt (yuborishga urinilgan, yetmagan).
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Yozilgan jarima (kpi_logs.id) va uni bekor qilgan admin.
    kpi_log_id: Mapped[int | None] = mapped_column(Integer)
    cancelled_by: Mapped[int | None] = mapped_column(Integer)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChecklistMember(Base):
    """Botda /start bosgan xodim. Bosgan-u keyin bloklagan = jarima yoziladi;
    hech bosmagan = jarima yo'q (savolni ko'rmagan)."""

    __tablename__ = "checklist_members"

    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", **_EMP_FK), primary_key=True)
    started_at: Mapped[datetime] = _now_col()


class ChecklistSettings(Base):
    __tablename__ = "checklist_settings"

    id: Mapped[int] = mapped_column(primary_key=True)  # doim 1
    penalty_per_question: Mapped[float] = mapped_column(Numeric(4, 2, asdecimal=False), default=0.2)


OWN_TABLES = [m.__table__ for m in (ChecklistQuestion, ChecklistAnswer, ChecklistViewer,
                                    ChecklistDay, ChecklistSettings, ChecklistMember)]

# create_all mavjud jadvalga ustun qo'shmaydi — keyin qo'shilgan ustunlar shu yerda
# (idempotent, har ishga tushishda xavfsiz).
_ADDED_COLUMNS = [
    "ALTER TABLE checklist_questions ADD COLUMN IF NOT EXISTS day date",
    "CREATE INDEX IF NOT EXISTS ix_checklist_questions_day ON checklist_questions (day)",
    "ALTER TABLE checklist_days ADD COLUMN IF NOT EXISTS asking_question_id integer",
    "ALTER TABLE checklist_days ADD COLUMN IF NOT EXISTS blocked_at timestamptz",
    "ALTER TABLE checklist_days ADD COLUMN IF NOT EXISTS kpi_log_id integer",
    "ALTER TABLE checklist_days ADD COLUMN IF NOT EXISTS cancelled_by integer",
    "ALTER TABLE checklist_days ADD COLUMN IF NOT EXISTS cancelled_at timestamptz",
]


async def init_db() -> None:
    # ponytail: ustunlar qo'lda _ADDED_COLUMNS'da; o'zgarishlar ko'paysa yoki ustun
    # turi/nomi o'zgarishi kerak bo'lsa — Alembic (alohida version_table bilan).
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=OWN_TABLES)
        for ddl in _ADDED_COLUMNS:
            await conn.execute(text(ddl))
        await conn.execute(
            pg_insert(ChecklistSettings.__table__).values(id=1, penalty_per_question=0.2)
            .on_conflict_do_nothing()
        )
