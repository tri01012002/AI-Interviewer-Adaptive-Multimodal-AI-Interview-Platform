"""Add organization isolation and invite-first candidate membership."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d3f7_tenant_isolation"
down_revision: str | Sequence[str] | None = "b6c9_candidate_invitations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _remove_candidate_global_uniques() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        metadata = sa.MetaData()
        candidates = sa.Table("candidates", metadata, autoload_with=bind)
        for constraint in list(candidates.constraints):
            if not isinstance(constraint, sa.UniqueConstraint):
                continue
            columns = tuple(column.name for column in constraint.columns)
            if columns in {("email",), ("user_id",)}:
                candidates.constraints.remove(constraint)
        for index in list(candidates.indexes):
            if index.name in {"ix_candidates_email", "ix_candidates_user_id"}:
                candidates.indexes.remove(index)

        with op.batch_alter_table(
            "candidates", recreate="always", copy_from=candidates
        ) as batch_op:
            batch_op.add_column(sa.Column("organization_id", sa.String(), nullable=True))
            batch_op.create_foreign_key(
                "fk_candidates_organization_id_organizations",
                "organizations",
                ["organization_id"],
                ["id"],
            )
            batch_op.create_foreign_key(
                "fk_candidates_org_owner_membership",
                "organization_memberships",
                ["organization_id", "owner_user_id"],
                ["organization_id", "user_id"],
            )
            batch_op.create_foreign_key(
                "fk_candidates_org_user_membership",
                "organization_memberships",
                ["organization_id", "user_id"],
                ["organization_id", "user_id"],
            )
            batch_op.create_unique_constraint(
                "uq_candidates_org_email", ["organization_id", "email"]
            )
            batch_op.create_unique_constraint(
                "uq_candidates_org_id", ["organization_id", "id"]
            )
            batch_op.create_unique_constraint(
                "uq_candidates_org_user", ["organization_id", "user_id"]
            )
        return

    inspector = sa.inspect(bind)
    for constraint in inspector.get_unique_constraints("candidates"):
        columns = tuple(constraint.get("column_names") or ())
        if columns in {("email",), ("user_id",)}:
            op.drop_constraint(
                constraint["name"], "candidates", type_="unique"
            )
    for index in inspector.get_indexes("candidates"):
        if index["name"] in {"ix_candidates_email", "ix_candidates_user_id"}:
            op.drop_index(index["name"], table_name="candidates")

    op.add_column(
        "candidates", sa.Column("organization_id", sa.String(), nullable=True)
    )
    op.create_foreign_key(
        "fk_candidates_organization_id_organizations",
        "candidates",
        "organizations",
        ["organization_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_candidates_org_owner_membership",
        "candidates",
        "organization_memberships",
        ["organization_id", "owner_user_id"],
        ["organization_id", "user_id"],
    )
    op.create_foreign_key(
        "fk_candidates_org_user_membership",
        "candidates",
        "organization_memberships",
        ["organization_id", "user_id"],
        ["organization_id", "user_id"],
    )
    op.create_unique_constraint(
        "uq_candidates_org_email", "candidates", ["organization_id", "email"]
    )
    op.create_unique_constraint(
        "uq_candidates_org_id", "candidates", ["organization_id", "id"]
    )
    op.create_unique_constraint(
        "uq_candidates_org_user", "candidates", ["organization_id", "user_id"]
    )


def _create_audit_immutability_trigger() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER access_audit_no_update
            BEFORE UPDATE ON access_audit
            BEGIN
                SELECT RAISE(ABORT, 'access_audit is append-only');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER access_audit_no_delete
            BEFORE DELETE ON access_audit
            BEGIN
                SELECT RAISE(ABORT, 'access_audit is append-only');
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            """
            CREATE FUNCTION deny_access_audit_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'access_audit is append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER access_audit_no_mutation
            BEFORE UPDATE OR DELETE ON access_audit
            FOR EACH ROW EXECUTE FUNCTION deny_access_audit_mutation()
            """
        )


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_by_user_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'suspended')", name="ck_organizations_status"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_organizations_status_created", "organizations", ["status", "created_at"]
    )
    op.create_table(
        "organization_memberships",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("granted_by_user_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "role IN ('org_admin', 'recruiter', 'candidate')",
            name="ck_organization_memberships_role",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_organization_memberships_status",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["granted_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "user_id", name="uq_organization_memberships_org_user"
        ),
    )
    op.create_index(
        "ix_organization_memberships_organization_id",
        "organization_memberships",
        ["organization_id"],
    )
    op.create_index(
        "ix_organization_memberships_user_id",
        "organization_memberships",
        ["user_id"],
    )
    op.create_index(
        "ix_organization_memberships_org_status_role",
        "organization_memberships",
        ["organization_id", "status", "role"],
    )

    _remove_candidate_global_uniques()
    op.create_index("ix_candidates_email", "candidates", ["email"])
    op.create_index("ix_candidates_user_id", "candidates", ["user_id"])
    op.create_index(
        "ix_candidates_organization_id", "candidates", ["organization_id"]
    )

    with op.batch_alter_table("interviews") as batch_op:
        batch_op.add_column(sa.Column("organization_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_interviews_organization_id_organizations",
            "organizations",
            ["organization_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_interviews_org_candidate",
            "candidates",
            ["organization_id", "candidate_id"],
            ["organization_id", "id"],
        )
        batch_op.create_foreign_key(
            "fk_interviews_org_owner_membership",
            "organization_memberships",
            ["organization_id", "owner_user_id"],
            ["organization_id", "user_id"],
        )
        batch_op.create_unique_constraint(
            "uq_interviews_org_id", ["organization_id", "id"]
        )
    op.create_index(
        "ix_interviews_organization_id", "interviews", ["organization_id"]
    )

    with op.batch_alter_table("candidate_invitations") as batch_op:
        batch_op.alter_column(
            "candidate_id",
            existing_type=sa.String(),
            existing_nullable=False,
            nullable=True,
        )
        batch_op.add_column(sa.Column("organization_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("candidate_name", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_candidate_invitations_organization_id_organizations",
            "organizations",
            ["organization_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_candidate_invitations_org_recruiter_membership",
            "organization_memberships",
            ["organization_id", "recruiter_user_id"],
            ["organization_id", "user_id"],
        )
        batch_op.create_foreign_key(
            "fk_candidate_invitations_org_candidate",
            "candidates",
            ["organization_id", "candidate_id"],
            ["organization_id", "id"],
        )
    op.execute(
        sa.text(
            """
            UPDATE candidate_invitations
            SET candidate_name = (
                SELECT candidates.name
                FROM candidates
                WHERE candidates.id = candidate_invitations.candidate_id
            )
            WHERE candidate_id IS NOT NULL
            """
        )
    )
    op.create_index(
        "ix_candidate_invitations_organization_id",
        "candidate_invitations",
        ["organization_id"],
    )
    op.create_index(
        "uq_candidate_invitations_one_pending_per_org_email",
        "candidate_invitations",
        ["organization_id", "email"],
        unique=True,
        sqlite_where=sa.text(
            "organization_id IS NOT NULL AND consumed_at IS NULL AND revoked_at IS NULL"
        ),
        postgresql_where=sa.text(
            "organization_id IS NOT NULL AND consumed_at IS NULL AND revoked_at IS NULL"
        ),
    )

    with op.batch_alter_table("access_audit") as batch_op:
        batch_op.alter_column(
            "actor_user_id",
            existing_type=sa.String(),
            existing_nullable=False,
            nullable=True,
        )
        batch_op.add_column(sa.Column("organization_id", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("requested_organization_id", sa.String(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "outcome",
                sa.String(),
                nullable=False,
                server_default="allowed",
            )
        )
        batch_op.add_column(sa.Column("support_reason", sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            "fk_access_audit_organization_id_organizations",
            "organizations",
            ["organization_id"],
            ["id"],
        )
    op.create_index(
        "ix_access_audit_organization_created",
        "access_audit",
        ["organization_id", "created_at"],
    )
    _create_audit_immutability_trigger()


def downgrade() -> None:
    bind = op.get_bind()
    tenant_data = bind.execute(
        sa.text(
            """
            SELECT
                (SELECT COUNT(*) FROM organizations)
                + (SELECT COUNT(*) FROM organization_memberships)
                + (SELECT COUNT(*) FROM candidates WHERE organization_id IS NOT NULL)
                + (SELECT COUNT(*) FROM interviews WHERE organization_id IS NOT NULL)
                + (SELECT COUNT(*) FROM candidate_invitations
                   WHERE organization_id IS NOT NULL)
                + (SELECT COUNT(*) FROM access_audit
                   WHERE organization_id IS NOT NULL
                      OR requested_organization_id IS NOT NULL
                      OR support_reason IS NOT NULL
                      OR outcome <> 'allowed')
            """
        )
    ).scalar_one()
    null_candidate_invitations = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM candidate_invitations WHERE candidate_id IS NULL"
        )
    ).scalar_one()
    duplicate_candidate_emails = bind.execute(
        sa.text(
            """
            SELECT COUNT(*) FROM (
                SELECT email FROM candidates GROUP BY email HAVING COUNT(*) > 1
            ) AS duplicate_candidate_emails
            """
        )
    ).scalar_one()
    duplicate_candidate_users = bind.execute(
        sa.text(
            """
            SELECT COUNT(*) FROM (
                SELECT user_id FROM candidates
                WHERE user_id IS NOT NULL
                GROUP BY user_id HAVING COUNT(*) > 1
            ) AS duplicate_candidate_users
            """
        )
    ).scalar_one()
    if (
        tenant_data
        or null_candidate_invitations
        or duplicate_candidate_emails
        or duplicate_candidate_users
    ):
        raise RuntimeError(
            "Cannot downgrade tenant data safely: tenant ownership or support-audit "
            "metadata exists, invitations without profiles exist, or organization-scoped "
            "candidate duplicates exist. Back up and reconcile tenant data before downgrading."
        )

    dialect = bind.dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER access_audit_no_update")
        op.execute("DROP TRIGGER access_audit_no_delete")
    elif dialect == "postgresql":
        op.execute("DROP TRIGGER access_audit_no_mutation ON access_audit")
        op.execute("DROP FUNCTION deny_access_audit_mutation()")
    op.drop_index("ix_access_audit_organization_created", table_name="access_audit")
    with op.batch_alter_table("access_audit") as batch_op:
        batch_op.drop_constraint(
            "fk_access_audit_organization_id_organizations", type_="foreignkey"
        )
        batch_op.drop_column("support_reason")
        batch_op.drop_column("outcome")
        batch_op.drop_column("requested_organization_id")
        batch_op.drop_column("organization_id")
        batch_op.alter_column(
            "actor_user_id",
            existing_type=sa.String(),
            existing_nullable=True,
            nullable=False,
        )

    op.drop_index(
        "uq_candidate_invitations_one_pending_per_org_email",
        table_name="candidate_invitations",
    )
    op.drop_index(
        "ix_candidate_invitations_organization_id",
        table_name="candidate_invitations",
    )
    with op.batch_alter_table("candidate_invitations") as batch_op:
        batch_op.drop_constraint(
            "fk_candidate_invitations_org_candidate", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_candidate_invitations_org_recruiter_membership",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_candidate_invitations_organization_id_organizations",
            type_="foreignkey",
        )
        batch_op.drop_column("candidate_name")
        batch_op.drop_column("organization_id")
        batch_op.alter_column(
            "candidate_id",
            existing_type=sa.String(),
            existing_nullable=True,
            nullable=False,
        )

    op.drop_index("ix_interviews_organization_id", table_name="interviews")
    with op.batch_alter_table("interviews") as batch_op:
        batch_op.drop_constraint(
            "fk_interviews_org_owner_membership", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_interviews_org_candidate", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_interviews_organization_id_organizations", type_="foreignkey"
        )
        batch_op.drop_constraint("uq_interviews_org_id", type_="unique")
        batch_op.drop_column("organization_id")

    op.drop_index("ix_candidates_organization_id", table_name="candidates")
    op.drop_index("ix_candidates_user_id", table_name="candidates")
    op.drop_index("ix_candidates_email", table_name="candidates")
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        metadata = sa.MetaData()
        candidates = sa.Table("candidates", metadata, autoload_with=bind)
        with op.batch_alter_table(
            "candidates", recreate="always", copy_from=candidates
        ) as batch_op:
            batch_op.drop_constraint(
                "fk_candidates_org_user_membership", type_="foreignkey"
            )
            batch_op.drop_constraint(
                "fk_candidates_org_owner_membership", type_="foreignkey"
            )
            batch_op.drop_constraint(
                "fk_candidates_organization_id_organizations",
                type_="foreignkey",
            )
            batch_op.drop_constraint("uq_candidates_org_user", type_="unique")
            batch_op.drop_constraint("uq_candidates_org_id", type_="unique")
            batch_op.drop_constraint("uq_candidates_org_email", type_="unique")
            batch_op.drop_column("organization_id")
            batch_op.create_unique_constraint("uq_candidates_user_id", ["user_id"])
    else:
        op.drop_constraint(
            "fk_candidates_org_user_membership",
            "candidates",
            type_="foreignkey",
        )
        op.drop_constraint(
            "fk_candidates_org_owner_membership",
            "candidates",
            type_="foreignkey",
        )
        op.drop_constraint(
            "fk_candidates_organization_id_organizations",
            "candidates",
            type_="foreignkey",
        )
        op.drop_constraint("uq_candidates_org_user", "candidates", type_="unique")
        op.drop_constraint("uq_candidates_org_id", "candidates", type_="unique")
        op.drop_constraint("uq_candidates_org_email", "candidates", type_="unique")
        op.drop_column("candidates", "organization_id")
        op.create_unique_constraint("uq_candidates_user_id", "candidates", ["user_id"])
        op.create_unique_constraint(
            "candidates_email_key", "candidates", ["email"]
        )

    op.create_index("ix_candidates_email", "candidates", ["email"], unique=True)
    op.create_index("ix_candidates_user_id", "candidates", ["user_id"])

    op.drop_index(
        "ix_organization_memberships_org_status_role",
        table_name="organization_memberships",
    )
    op.drop_index(
        "ix_organization_memberships_user_id", table_name="organization_memberships"
    )
    op.drop_index(
        "ix_organization_memberships_organization_id",
        table_name="organization_memberships",
    )
    op.drop_table("organization_memberships")
    op.drop_index("ix_organizations_status_created", table_name="organizations")
    op.drop_table("organizations")
