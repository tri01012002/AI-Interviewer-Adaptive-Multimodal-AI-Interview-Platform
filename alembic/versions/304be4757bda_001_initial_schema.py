"""Initial schema with normalized interview tables

Revision ID: 304be4757bda
Revises:
Create Date: 2026-09-02 20:41:13.733300

This migration creates the initial normalized schema for the interview platform.

Tables:
  - users: user accounts
  - candidates: interview candidates
  - interviews: interview records (keeps state_json for backward compatibility)
  - interview_turns: individual turns within an interview
  - interview_questions: questions asked during interviews
  - interview_evidence: evidence extracted from answers
  - interview_competency_state: competency tracking per interview
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '304be4757bda'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - create all tables."""
    
    # Create users table
    op.create_table(
        'users',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('email', sa.String(), nullable=False),
        sa.Column('password_hash', sa.String(), nullable=False),
        sa.Column('role', sa.String(), nullable=False, server_default='admin'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email'),
    )
    op.create_index('ix_users_email', 'users', ['email'], unique=True)
    
    # Create candidates table
    op.create_table(
        'candidates',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('email', sa.String(), nullable=False),
        sa.Column('phone', sa.String(), nullable=True),
        sa.Column('resume_url', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email'),
    )
    op.create_index('ix_candidates_email', 'candidates', ['email'], unique=True)
    
    # Create interviews table (keeping state_json for backward compatibility)
    op.create_table(
        'interviews',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('candidate_id', sa.String(), nullable=False),
        sa.Column('position', sa.String(), nullable=False),
        sa.Column('mode', sa.String(), nullable=False, server_default='text'),
        sa.Column('current_question', sa.String(), nullable=False),
        sa.Column('state_json', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_interviews_candidate_id', 'interviews', ['candidate_id'])
    
    # Create interview_questions table FIRST (will be referenced by interview_turns)
    op.create_table(
        'interview_questions',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('interview_id', sa.String(), nullable=False),
        sa.Column('turn_id', sa.String(), nullable=True),
        sa.Column('sequence_number', sa.Integer(), nullable=False),
        sa.Column('competency', sa.String(), nullable=True),
        sa.Column('difficulty', sa.String(), nullable=True),
        sa.Column('question_text', sa.Text(), nullable=False),
        sa.Column('question_type', sa.String(), nullable=False, server_default='free_form'),
        sa.Column('status', sa.String(), nullable=False, server_default='pending'),
        sa.Column('generated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['interview_id'], ['interviews.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_interview_questions_interview_id_sequence', 'interview_questions', ['interview_id', 'sequence_number'])
    op.create_index('ix_interview_questions_interview_id_status', 'interview_questions', ['interview_id', 'status'])
    
    # Create interview_turns table
    op.create_table(
        'interview_turns',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('interview_id', sa.String(), nullable=False),
        sa.Column('turn_id', sa.String(), nullable=False),
        sa.Column('sequence_number', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='received'),
        sa.Column('question_id', sa.String(), nullable=True),
        sa.Column('candidate_answer', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['interview_id'], ['interviews.id']),
        sa.ForeignKeyConstraint(['question_id'], ['interview_questions.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_interview_turns_interview_id_turn_id', 'interview_turns', ['interview_id', 'turn_id'], unique=True)
    op.create_index('ix_interview_turns_interview_id_sequence', 'interview_turns', ['interview_id', 'sequence_number'], unique=True)
    op.create_index('ix_interview_turns_interview_id_status', 'interview_turns', ['interview_id', 'status'])
    
    # Create interview_evidence table
    op.create_table(
        'interview_evidence',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('interview_id', sa.String(), nullable=False),
        sa.Column('turn_id', sa.String(), nullable=False),
        sa.Column('competency', sa.String(), nullable=False),
        sa.Column('evidence_text', sa.Text(), nullable=False),
        sa.Column('strength', sa.String(), nullable=True),
        sa.Column('specificity', sa.String(), nullable=True),
        sa.Column('ownership', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['interview_id'], ['interviews.id']),
        sa.ForeignKeyConstraint(['turn_id'], ['interview_turns.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_interview_evidence_interview_id_competency', 'interview_evidence', ['interview_id', 'competency'])
    
    # Create interview_competency_state table
    op.create_table(
        'interview_competency_state',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('interview_id', sa.String(), nullable=False),
        sa.Column('competency', sa.String(), nullable=False),
        sa.Column('evidence_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('confidence', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('strength', sa.String(), nullable=False, server_default='unknown'),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['interview_id'], ['interviews.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_interview_competency_state_interview_id_competency', 'interview_competency_state', ['interview_id', 'competency'], unique=True)
    op.create_index('ix_interview_competency_state_interview_id_confidence', 'interview_competency_state', ['interview_id', 'confidence'])


def downgrade() -> None:
    """Downgrade schema - drop all tables."""
    op.drop_index('ix_interview_competency_state_interview_id_confidence', table_name='interview_competency_state')
    op.drop_index('ix_interview_competency_state_interview_id_competency', table_name='interview_competency_state')
    op.drop_table('interview_competency_state')
    
    op.drop_index('ix_interview_evidence_interview_id_competency', table_name='interview_evidence')
    op.drop_table('interview_evidence')
    
    op.drop_index('ix_interview_turns_interview_id_status', table_name='interview_turns')
    op.drop_index('ix_interview_turns_interview_id_sequence', table_name='interview_turns')
    op.drop_index('ix_interview_turns_interview_id_turn_id', table_name='interview_turns')
    op.drop_table('interview_turns')
    
    op.drop_index('ix_interview_questions_interview_id_status', table_name='interview_questions')
    op.drop_index('ix_interview_questions_interview_id_sequence', table_name='interview_questions')
    op.drop_table('interview_questions')
    
    op.drop_index('ix_interviews_candidate_id', table_name='interviews')
    op.drop_table('interviews')
    
    op.drop_index('ix_candidates_email', table_name='candidates')
    op.drop_table('candidates')
    
    op.drop_index('ix_users_email', table_name='users')
    op.drop_table('users')
