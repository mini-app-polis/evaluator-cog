"""API-006's four scoping patterns, read from the models alone."""

from __future__ import annotations

from pathlib import Path

from evaluator_cog.engine.deterministic.api import check_owner_id_column

_HEAD = """\
from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass

"""


def _flagged(tmp_path: Path, models: str) -> list[str]:
    path = tmp_path / "src" / "app" / "models.py"
    path.parent.mkdir(parents=True)
    path.write_text(_HEAD + models)
    return [f["model"] for f in check_owner_id_column(tmp_path)]


def test_pattern1_owner_id(tmp_path: Path) -> None:
    assert (
        _flagged(
            tmp_path,
            """
class Note(Base):
    __tablename__ = "notes"
    id: Mapped[str] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(Text)
""",
        )
        == []
    )


def test_pattern2_identity_table(tmp_path: Path) -> None:
    assert (
        _flagged(
            tmp_path,
            """
class Member(Base):
    __tablename__ = "members"
    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
""",
        )
        == []
    )


def test_pattern3_column_of_any_name_with_a_foreign_key_to_an_identity_table(
    tmp_path: Path,
) -> None:
    assert _flagged(
        tmp_path,
        """
class Member(Base):
    __tablename__ = "members"
    user_id: Mapped[str] = mapped_column(Text, primary_key=True)


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[str] = mapped_column(primary_key=True)
    created_by: Mapped[str] = mapped_column(Text, ForeignKey("members.user_id"))


class Pair(Base):
    __tablename__ = "pairs"
    id: Mapped[str] = mapped_column(primary_key=True)
    partner: Mapped[str] = mapped_column(Text, ForeignKey("users.id"))


class Tag(Base):
    __tablename__ = "tags"
    id: Mapped[str] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, ForeignKey("labels.id"))
""",
    ) == ["Tag"]


def test_pattern4_child_of_a_scoped_table_resolves_transitively(
    tmp_path: Path,
) -> None:
    assert _flagged(
        tmp_path,
        """
class Event(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(Text)


class Round(Base):
    __tablename__ = "rounds"
    id: Mapped[str] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(Text, ForeignKey("events.id"))


class Heat(Base):
    __tablename__ = "heats"
    id: Mapped[str] = mapped_column(primary_key=True)
    round_id: Mapped[str] = mapped_column(Text, ForeignKey(Round.id))


class Orphan(Base):
    __tablename__ = "orphans"
    id: Mapped[str] = mapped_column(primary_key=True)
    thing_id: Mapped[str] = mapped_column(Text, ForeignKey("things.id"))
""",
    ) == ["Orphan"]


def test_pattern4_a_cycle_that_never_reaches_a_scoped_table_does_not_pass(
    tmp_path: Path,
) -> None:
    assert _flagged(
        tmp_path,
        """
class Alpha(Base):
    __tablename__ = "alphas"
    id: Mapped[str] = mapped_column(primary_key=True)
    beta_id: Mapped[str] = mapped_column(Text, ForeignKey("betas.id"))


class Beta(Base):
    __tablename__ = "betas"
    id: Mapped[str] = mapped_column(primary_key=True)
    alpha_id: Mapped[str] = mapped_column(Text, ForeignKey("alphas.id"))
""",
    ) == ["Alpha", "Beta"]


def test_pattern4_a_cycle_with_a_way_out_passes(tmp_path: Path) -> None:
    assert (
        _flagged(
            tmp_path,
            """
class Alpha(Base):
    __tablename__ = "alphas"
    id: Mapped[str] = mapped_column(primary_key=True)
    beta_id: Mapped[str] = mapped_column(Text, ForeignKey("betas.id"))


class Beta(Base):
    __tablename__ = "betas"
    id: Mapped[str] = mapped_column(primary_key=True)
    alpha_id: Mapped[str] = mapped_column(Text, ForeignKey("alphas.id"))
    owner: Mapped[str] = mapped_column(Text, ForeignKey("users.id"))
""",
        )
        == []
    )


def test_a_foreign_key_only_in_migration_sql_does_not_count(tmp_path: Path) -> None:
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "001.sql").write_text(
        "alter table rounds add foreign key (event_id) references users (id);\n"
    )
    assert _flagged(
        tmp_path,
        """
class Round(Base):
    __tablename__ = "rounds"
    id: Mapped[str] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(Text)
""",
    ) == ["Round"]
