"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-11-12 11:32:21.139091
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from investigator.db.models import UTCDateTime

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('services',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('team', sa.String(length=120), nullable=True),
    sa.Column('tier', sa.String(length=20), nullable=True),
    sa.Column('created_at', UTCDateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('deploys',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('service_id', sa.Integer(), nullable=False),
    sa.Column('version', sa.String(length=40), nullable=False),
    sa.Column('previous_version', sa.String(length=40), nullable=True),
    sa.Column('commit_sha', sa.String(length=40), nullable=True),
    sa.Column('deployed_by', sa.String(length=120), nullable=True),
    sa.Column('change_summary', sa.Text(), nullable=True),
    sa.Column('deployed_at', UTCDateTime(), nullable=False),
    sa.ForeignKeyConstraint(['service_id'], ['services.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('deploys', schema=None) as batch_op:
        batch_op.create_index('ix_deploys_service_deployed', ['service_id', 'deployed_at'], unique=False)

    op.create_table('incidents',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=240), nullable=False),
    sa.Column('alert_text', sa.Text(), nullable=False),
    sa.Column('service_id', sa.Integer(), nullable=True),
    sa.Column('endpoint', sa.String(length=200), nullable=True),
    sa.Column('severity', sa.String(length=10), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('detected_at', UTCDateTime(), nullable=False),
    sa.Column('created_at', UTCDateTime(), nullable=False),
    sa.ForeignKeyConstraint(['service_id'], ['services.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('incidents', schema=None) as batch_op:
        batch_op.create_index('ix_incidents_service_detected', ['service_id', 'detected_at'], unique=False)
        batch_op.create_index('ix_incidents_status', ['status'], unique=False)

    op.create_table('logs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('service_id', sa.Integer(), nullable=False),
    sa.Column('ts', UTCDateTime(), nullable=False),
    sa.Column('level', sa.String(length=10), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('trace_id', sa.String(length=32), nullable=True),
    sa.Column('span_id', sa.String(length=16), nullable=True),
    sa.Column('attributes', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.ForeignKeyConstraint(['service_id'], ['services.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('logs', schema=None) as batch_op:
        batch_op.create_index('ix_logs_service_ts', ['service_id', 'ts'], unique=False)
        batch_op.create_index('ix_logs_trace', ['trace_id'], unique=False)

    op.create_table('spans',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('trace_id', sa.String(length=32), nullable=False),
    sa.Column('span_id', sa.String(length=16), nullable=False),
    sa.Column('parent_span_id', sa.String(length=16), nullable=True),
    sa.Column('service_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('start_ts', UTCDateTime(), nullable=False),
    sa.Column('duration_ms', sa.Float(), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('status_message', sa.String(length=500), nullable=True),
    sa.Column('attributes', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.ForeignKeyConstraint(['service_id'], ['services.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('trace_id', 'span_id', name='uq_spans_trace_span')
    )
    with op.batch_alter_table('spans', schema=None) as batch_op:
        batch_op.create_index('ix_spans_errors_service_start', ['service_id', 'start_ts'], unique=False, sqlite_where=sa.text("status = 'ERROR'"), postgresql_where=sa.text("status = 'ERROR'"))
        batch_op.create_index('ix_spans_service_name_start', ['service_id', 'name', 'start_ts'], unique=False)
        batch_op.create_index('ix_spans_service_start', ['service_id', 'start_ts'], unique=False)

    op.create_table('incident_labels',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('incident_id', sa.Integer(), nullable=False),
    sa.Column('scenario', sa.String(length=60), nullable=False),
    sa.Column('root_cause_category', sa.String(length=40), nullable=False),
    sa.Column('root_cause_component', sa.String(length=120), nullable=False),
    sa.Column('explanation', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('incident_id'),
    sa.UniqueConstraint('scenario')
    )
    op.create_table('investigations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('incident_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('llm_provider', sa.String(length=40), nullable=False),
    sa.Column('llm_model', sa.String(length=120), nullable=False),
    sa.Column('max_steps', sa.Integer(), nullable=False),
    sa.Column('steps', sa.Integer(), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Float(), nullable=False),
    sa.Column('latency_ms', sa.Float(), nullable=True),
    sa.Column('root_cause_category', sa.String(length=40), nullable=True),
    sa.Column('root_cause_component', sa.String(length=120), nullable=True),
    sa.Column('confidence', sa.String(length=10), nullable=True),
    sa.Column('report', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('report_markdown', sa.Text(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', UTCDateTime(), nullable=False),
    sa.Column('started_at', UTCDateTime(), nullable=True),
    sa.Column('finished_at', UTCDateTime(), nullable=True),
    sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('investigations', schema=None) as batch_op:
        batch_op.create_index('ix_investigations_incident_created', ['incident_id', 'created_at'], unique=False)
        batch_op.create_index('ix_investigations_status', ['status'], unique=False)

    op.create_table('investigation_steps',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('investigation_id', sa.Integer(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('node', sa.String(length=40), nullable=False),
    sa.Column('tool_name', sa.String(length=80), nullable=True),
    sa.Column('evidence_id', sa.String(length=10), nullable=True),
    sa.Column('args', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('result_summary', sa.Text(), nullable=False),
    sa.Column('latency_ms', sa.Float(), nullable=True),
    sa.Column('created_at', UTCDateTime(), nullable=False),
    sa.ForeignKeyConstraint(['investigation_id'], ['investigations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('investigation_id', 'seq', name='uq_steps_investigation_seq')
    )


def downgrade() -> None:
    op.drop_table('investigation_steps')
    with op.batch_alter_table('investigations', schema=None) as batch_op:
        batch_op.drop_index('ix_investigations_status')
        batch_op.drop_index('ix_investigations_incident_created')

    op.drop_table('investigations')
    op.drop_table('incident_labels')
    with op.batch_alter_table('spans', schema=None) as batch_op:
        batch_op.drop_index('ix_spans_service_start')
        batch_op.drop_index('ix_spans_service_name_start')
        batch_op.drop_index('ix_spans_errors_service_start', sqlite_where=sa.text("status = 'ERROR'"), postgresql_where=sa.text("status = 'ERROR'"))

    op.drop_table('spans')
    with op.batch_alter_table('logs', schema=None) as batch_op:
        batch_op.drop_index('ix_logs_trace')
        batch_op.drop_index('ix_logs_service_ts')

    op.drop_table('logs')
    with op.batch_alter_table('incidents', schema=None) as batch_op:
        batch_op.drop_index('ix_incidents_status')
        batch_op.drop_index('ix_incidents_service_detected')

    op.drop_table('incidents')
    with op.batch_alter_table('deploys', schema=None) as batch_op:
        batch_op.drop_index('ix_deploys_service_deployed')

    op.drop_table('deploys')
    op.drop_table('services')
