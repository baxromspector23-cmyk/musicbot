"""Asynchronous SQLite / PostgreSQL database service using SQLAlchemy 2.x."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

import sqlalchemy.types as types
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    JSON,
    String,
    Text,
    delete,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import config

log = logging.getLogger(__name__)


class UTCDateTime(types.TypeDecorator):
    """DateTime type that stores naive UTC in DB and returns timezone-aware UTC across SQLite and Postgres."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is not None:
            if not isinstance(value, datetime):
                return value
            if value.tzinfo is not None:
                value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is not None and isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        return value


class Base(DeclarativeBase):
    pass


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    storage_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    storage_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tg_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_unique_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    hashtags: Mapped[list[str]] = mapped_column(JSON, default=list)
    scheduled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    ig_container_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ig_media_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    permalink: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(timezone.utc)
    )
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    stats_sent: Mapped[bool] = mapped_column(Boolean, default=False)


class KV(Base):
    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, default=dict)


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        db_url = config.DATABASE_URL
        connect_args: dict[str, Any] = {}
        if "sqlite" in db_url:
            connect_args["check_same_thread"] = False
        _engine = create_async_engine(
            db_url,
            echo=False,
            future=True,
            connect_args=connect_args,
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional scope around a series of operations."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db(engine: AsyncEngine | None = None) -> None:
    """Initialize database tables asynchronously."""
    eng = engine or get_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("database tables initialized successfully")


# ----------------------------------------------------------------------
# KV Store Helpers
# ----------------------------------------------------------------------

async def get_kv(key: str, default: Any = None) -> Any:
    async with session_scope() as session:
        stmt = select(KV).where(KV.key == key)
        res = await session.execute(stmt)
        item = res.scalar_one_or_none()
        if item is None:
            return default
        return item.value


async def set_kv(key: str, value: Any) -> None:
    """Atomically upsert key-value pair using INSERT ... ON CONFLICT DO UPDATE with IntegrityError retry."""
    for attempt in range(3):
        try:
            async with session_scope() as session:
                dialect_name = session.bind.dialect.name if session.bind else "sqlite"
                if dialect_name == "postgresql":
                    from sqlalchemy.dialects.postgresql import insert as pg_insert
                    stmt = (
                        pg_insert(KV)
                        .values(key=key, value=value)
                        .on_conflict_do_update(index_elements=[KV.key], set_={"value": value})
                    )
                    await session.execute(stmt)
                elif dialect_name == "sqlite":
                    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
                    stmt = (
                        sqlite_insert(KV)
                        .values(key=key, value=value)
                        .on_conflict_do_update(index_elements=[KV.key], set_={"value": value})
                    )
                    await session.execute(stmt)
                else:
                    item = (await session.execute(select(KV).where(KV.key == key))).scalar_one_or_none()
                    if item is None:
                        session.add(KV(key=key, value=value))
                    else:
                        item.value = value
            return
        except IntegrityError:
            if attempt == 2:
                raise


async def delete_kv(key: str) -> None:
    async with session_scope() as session:
        await session.execute(delete(KV).where(KV.key == key))


# ----------------------------------------------------------------------
# Post CRUD Helpers
# ----------------------------------------------------------------------

async def create_post(
    owner_id: int,
    storage_chat_id: int | None = None,
    storage_message_id: int | None = None,
    tg_file_id: str | None = None,
    file_unique_id: str | None = None,
    file_size: int | None = None,
    duration: float | None = None,
    width: int | None = None,
    height: int | None = None,
    caption: str | None = None,
    hashtags: list[str] | None = None,
    scheduled_at: datetime | None = None,
    status: str = "draft",
) -> Post:
    async with session_scope() as session:
        post = Post(
            owner_id=owner_id,
            storage_chat_id=storage_chat_id,
            storage_message_id=storage_message_id,
            tg_file_id=tg_file_id,
            file_unique_id=file_unique_id,
            file_size=file_size,
            duration=duration,
            width=width,
            height=height,
            caption=caption,
            hashtags=hashtags or [],
            scheduled_at=scheduled_at,
            status=status,
        )
        session.add(post)
        await session.flush()
        post_id = post.id
    return await get_post(post_id)  # type: ignore[return-value]


async def get_post(post_id: int) -> Post | None:
    async with session_scope() as session:
        stmt = select(Post).where(Post.id == post_id)
        res = await session.execute(stmt)
        return res.scalar_one_or_none()


async def update_post(post_id: int, **kwargs: Any) -> Post | None:
    async with session_scope() as session:
        stmt = update(Post).where(Post.id == post_id).values(**kwargs)
        await session.execute(stmt)
    return await get_post(post_id)


async def delete_post(post_id: int) -> bool:
    async with session_scope() as session:
        stmt = delete(Post).where(Post.id == post_id)
        res = await session.execute(stmt)
        return (res.rowcount or 0) > 0


async def list_scheduled_posts(limit: int = 50) -> list[Post]:
    async with session_scope() as session:
        stmt = (
            select(Post)
            .where(Post.status.in_(["scheduled", "overdue"]))
            .order_by(Post.scheduled_at.asc().nulls_last(), Post.id.asc())
            .limit(limit)
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())


async def list_published_posts(limit: int = 10) -> list[Post]:
    async with session_scope() as session:
        stmt = (
            select(Post)
            .where(Post.status == "published")
            .order_by(Post.published_at.desc().nulls_last(), Post.id.desc())
            .limit(limit)
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())


async def list_failed_posts(limit: int = 10) -> list[Post]:
    """Return recently failed posts ordered by id desc."""
    async with session_scope() as session:
        stmt = (
            select(Post)
            .where(Post.status == "failed")
            .order_by(Post.id.desc())
            .limit(limit)
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())


async def get_stuck_publishing_posts(now_utc: datetime | None = None) -> list[Post]:
    """Return posts in 'publishing' status whose lock has expired or all publishing posts on startup."""
    async with session_scope() as session:
        if now_utc is None:
            stmt = select(Post).where(Post.status == "publishing").order_by(Post.id.asc())
        else:
            stmt = (
                select(Post)
                .where(
                    (Post.status == "publishing")
                    & ((Post.locked_until == None) | (Post.locked_until <= now_utc))  # noqa: E711
                )
                .order_by(Post.id.asc())
            )
        res = await session.execute(stmt)
        return list(res.scalars().all())


async def get_overdue_scheduled_posts(cutoff_time: datetime) -> list[Post]:
    """Return scheduled posts whose scheduled_at is older than cutoff_time (e.g. 6 hours ago)."""
    async with session_scope() as session:
        stmt = (
            select(Post)
            .where(
                (Post.status == "scheduled")
                & (Post.scheduled_at != None)  # noqa: E711
                & (Post.scheduled_at < cutoff_time)
            )
            .order_by(Post.scheduled_at.asc(), Post.id.asc())
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())


async def claim_due_posts(now_utc: datetime, lock_duration_sec: int = 900) -> list[Post]:
    """Atomically claim posts that are due for publishing (idempotency guard)."""
    claimed_posts: list[Post] = []
    lock_until = datetime.fromtimestamp(now_utc.timestamp() + lock_duration_sec, tz=timezone.utc)
    six_hours_ago = datetime.fromtimestamp(now_utc.timestamp() - 6 * 3600, tz=timezone.utc)

    async with session_scope() as session:
        # Select candidates that are scheduled, due, within the 6-hour window, and not locked
        stmt = (
            select(Post)
            .where(
                (Post.status == "scheduled")
                & (Post.scheduled_at <= now_utc)
                & (Post.scheduled_at >= six_hours_ago)
                & ((Post.locked_until == None) | (Post.locked_until <= now_utc))  # noqa: E711
            )
            .order_by(Post.scheduled_at.asc(), Post.id.asc())
        )
        res = await session.execute(stmt)
        candidates = list(res.scalars().all())

        for p in candidates:
            # Atomic update to 'publishing' status and set lock_until
            upd = (
                update(Post)
                .where(Post.id == p.id, Post.status == "scheduled")
                .values(status="publishing", locked_until=lock_until)
            )
            upd_res = await session.execute(upd)
            if (upd_res.rowcount or 0) > 0:
                p.status = "publishing"
                p.locked_until = lock_until
                claimed_posts.append(p)

    return claimed_posts
