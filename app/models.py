"""Workout templates and performed sets; all weights are external load in kg."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    and_,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Exercise(Base):
    __tablename__ = "exercises"
    __table_args__ = (CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)


class WorkoutPlan(Base):
    __tablename__ = "workout_plans"
    __table_args__ = (
        UniqueConstraint("id", "user_id"),
        UniqueConstraint("user_id", "name"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[UUID]
    name: Mapped[str] = mapped_column(String(120))

    exercises: Mapped[list["WorkoutPlanExercise"]] = relationship(
        back_populates="plan",
        order_by="WorkoutPlanExercise.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    workouts: Mapped[list["Workout"]] = relationship(
        back_populates="plan",
        primaryjoin=lambda: and_(
            WorkoutPlan.id == Workout.plan_id, WorkoutPlan.user_id == Workout.user_id
        ),
        foreign_keys="Workout.plan_id",
        passive_deletes="all",
    )


class WorkoutPlanExercise(Base):
    __tablename__ = "workout_plan_exercises"
    __table_args__ = (
        UniqueConstraint("plan_id", "position"),
        CheckConstraint("position > 0", name="positive_position"),
        CheckConstraint("target_sets > 0", name="positive_target_sets"),
        CheckConstraint("target_reps > 0", name="positive_target_reps"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("workout_plans.id", ondelete="CASCADE")
    )
    exercise_id: Mapped[int] = mapped_column(ForeignKey("exercises.id", ondelete="RESTRICT"))
    position: Mapped[int]
    target_sets: Mapped[int]
    target_reps: Mapped[int]

    plan: Mapped[WorkoutPlan] = relationship(back_populates="exercises")
    exercise: Mapped[Exercise] = relationship()


class Workout(Base):
    __tablename__ = "workouts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "user_id"],
            ["workout_plans.id", "workout_plans.user_id"],
            ondelete="RESTRICT",
        ),
        Index("ix_workouts_user_id_started_at", "user_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[UUID]
    plan_id: Mapped[int | None]
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    plan: Mapped[WorkoutPlan | None] = relationship(
        back_populates="workouts",
        primaryjoin=lambda: and_(
            Workout.plan_id == WorkoutPlan.id, Workout.user_id == WorkoutPlan.user_id
        ),
        foreign_keys="Workout.plan_id",
    )
    sets: Mapped[list["WorkoutSet"]] = relationship(
        back_populates="workout",
        order_by="(WorkoutSet.exercise_id, WorkoutSet.set_number)",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class WorkoutSet(Base):
    __tablename__ = "workout_sets"
    __table_args__ = (
        UniqueConstraint("workout_id", "exercise_id", "set_number"),
        CheckConstraint("set_number > 0", name="positive_set_number"),
        CheckConstraint("reps > 0", name="positive_reps"),
        CheckConstraint(
            "weight_kg >= 0 AND weight_kg <= 99999.99", name="valid_weight_kg"
        ),
        Index("ix_workout_sets_exercise_id", "exercise_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workout_id: Mapped[int] = mapped_column(ForeignKey("workouts.id", ondelete="CASCADE"))
    exercise_id: Mapped[int] = mapped_column(ForeignKey("exercises.id", ondelete="RESTRICT"))
    set_number: Mapped[int]
    reps: Mapped[int]
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(7, 2))

    workout: Mapped[Workout] = relationship(back_populates="sets")
    exercise: Mapped[Exercise] = relationship()
