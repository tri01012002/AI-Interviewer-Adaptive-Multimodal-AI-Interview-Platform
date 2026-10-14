"""Add explicit fail-closed RAG corpus scope."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e84_rag_tenant_scope"
down_revision: str | Sequence[str] | None = "d3f7_tenant_isolation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _scope_constraints() -> tuple[sa.CheckConstraint, ...]:
    return (
        sa.CheckConstraint(
            "scope IN ('organization', 'global', 'unclassified')",
            name="ck_rag_documents_scope",
        ),
        sa.CheckConstraint(
            "(scope = 'organization' AND organization_id IS NOT NULL "
            "AND trim(organization_id) <> '') OR "
            "(scope = 'global' AND organization_id IS NULL) OR "
            "scope = 'unclassified'",
            name="ck_rag_documents_scope_organization",
        ),
        sa.CheckConstraint(
            "(scope = 'global' AND scope_approval_reference IS NOT NULL "
            "AND trim(scope_approval_reference) <> '') OR "
            "(scope IN ('organization', 'unclassified') "
            "AND scope_approval_reference IS NULL)",
            name="ck_rag_documents_scope_approval",
        ),
    )


def _assert_scope_metadata_is_discardable() -> None:
    bind = op.get_bind()
    result = bind.execute(
        sa.text(
            "SELECT 1 FROM rag_documents "
            "WHERE scope <> 'unclassified' "
            "OR scope_approval_reference IS NOT NULL "
            "LIMIT 1"
        )
    ).first()
    if result is not None:
        raise RuntimeError(
            "cannot downgrade RAG scope metadata while classified corpus rows exist"
        )

    duplicate = bind.execute(
        sa.text(
            "SELECT 1 FROM rag_documents "
            "GROUP BY document_id, chunk_id, version "
            "HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "cannot downgrade RAG identity indexes while duplicate chunk keys exist"
        )


def upgrade() -> None:
    bind = op.get_bind()
    scope_column = sa.Column(
        "scope",
        sa.String(),
        nullable=False,
        server_default="unclassified",
    )
    approval_column = sa.Column(
        "scope_approval_reference",
        sa.String(),
        nullable=True,
    )

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(
            "rag_documents",
            recreate="always",
        ) as batch_op:
            batch_op.drop_constraint(
                "uq_rag_document_chunk_version",
                type_="unique",
            )
            batch_op.add_column(scope_column)
            batch_op.add_column(approval_column)
            for constraint in _scope_constraints():
                batch_op.create_check_constraint(
                    constraint.name,
                    constraint.sqltext,
                )
    else:
        op.drop_constraint(
            "uq_rag_document_chunk_version",
            "rag_documents",
            type_="unique",
        )
        op.add_column("rag_documents", scope_column)
        op.add_column("rag_documents", approval_column)
        for constraint in _scope_constraints():
            op.create_check_constraint(
                constraint.name,
                "rag_documents",
                constraint.sqltext,
            )

    op.create_index(
        "uq_rag_document_chunk_org_scope_version",
        "rag_documents",
        ["organization_id", "document_id", "chunk_id", "version"],
        unique=True,
        postgresql_where=sa.text("scope = 'organization'"),
        sqlite_where=sa.text("scope = 'organization'"),
    )
    op.create_index(
        "uq_rag_document_chunk_nontenant_scope_version",
        "rag_documents",
        ["scope", "document_id", "chunk_id", "version"],
        unique=True,
        postgresql_where=sa.text("scope IN ('global', 'unclassified')"),
        sqlite_where=sa.text("scope IN ('global', 'unclassified')"),
    )
    op.create_index(
        "ix_rag_document_scope_filters",
        "rag_documents",
        [
            "active",
            "scope",
            "organization_id",
            "job_id",
            "competency_id",
            "difficulty",
        ],
    )


def downgrade() -> None:
    _assert_scope_metadata_is_discardable()

    bind = op.get_bind()
    op.drop_index(
        "ix_rag_document_scope_filters",
        table_name="rag_documents",
    )
    op.drop_index(
        "uq_rag_document_chunk_nontenant_scope_version",
        table_name="rag_documents",
    )
    op.drop_index(
        "uq_rag_document_chunk_org_scope_version",
        table_name="rag_documents",
    )

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(
            "rag_documents",
            recreate="always",
        ) as batch_op:
            batch_op.drop_constraint(
                "ck_rag_documents_scope_approval",
                type_="check",
            )
            batch_op.drop_constraint(
                "ck_rag_documents_scope_organization",
                type_="check",
            )
            batch_op.drop_constraint(
                "ck_rag_documents_scope",
                type_="check",
            )
            batch_op.drop_column("scope_approval_reference")
            batch_op.drop_column("scope")
            batch_op.create_unique_constraint(
                "uq_rag_document_chunk_version",
                ["document_id", "chunk_id", "version"],
            )
    else:
        op.drop_constraint(
            "ck_rag_documents_scope_approval",
            "rag_documents",
            type_="check",
        )
        op.drop_constraint(
            "ck_rag_documents_scope_organization",
            "rag_documents",
            type_="check",
        )
        op.drop_constraint(
            "ck_rag_documents_scope",
            "rag_documents",
            type_="check",
        )
        op.drop_column("rag_documents", "scope_approval_reference")
        op.drop_column("rag_documents", "scope")
        op.create_unique_constraint(
            "uq_rag_document_chunk_version",
            "rag_documents",
            ["document_id", "chunk_id", "version"],
        )
